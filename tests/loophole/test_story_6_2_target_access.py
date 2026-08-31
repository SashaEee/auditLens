"""Контракт управления доступом и lifecycle Telegram-цели (Story 6.2)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from bank_audit.loophole.target_access import (
    TargetAccessDenied,
    TargetAccessService,
    TargetInactiveError,
    TargetRedirectError,
)


@pytest.fixture
def target_access_schema(session):
    """Минимальная SQLite-проекция таблиц миграции 054."""
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_target (
                target_id INTEGER PRIMARY KEY,
                normalized_address TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                canonical_target_id INTEGER,
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                generation INTEGER NOT NULL DEFAULT 1,
                fence_token INTEGER NOT NULL DEFAULT 1,
                checkpoint_json TEXT
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_workspace_subscription (
                subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                grant_version INTEGER NOT NULL,
                intent_sequence INTEGER NOT NULL,
                granted_by TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                revoked_at TEXT
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_target_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                workspace_id INTEGER,
                actor_username TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_terminal_signal (
                terminal_signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                fence_token INTEGER NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    session.execute(
        text(
            """
            INSERT INTO loophole_telegram_target
                (target_id, normalized_address, target_kind, lifecycle_status, checkpoint_json)
            VALUES (1, 't.me/bank_news', 'public', 'active', :checkpoint)
            """
        ),
        {"checkpoint": json.dumps({"sequence": 42, "cursor": "latest"})},
    )
    session.execute(
        text(
            """
            INSERT INTO loophole_telegram_target
                (target_id, normalized_address, target_kind, canonical_target_id, lifecycle_status)
            VALUES (2, 't.me/old_bank_news', 'public', 1, 'active')
            """
        )
    )
    return session


def _allowed(_actor: str, _workspace_id: int, _capability: str) -> bool:
    return True


def test_grant_subscription_changes_only_canonical_target_and_audits(target_access_schema):
    service = TargetAccessService(target_access_schema, capability_checker=_allowed)

    result = service.grant_workspace_subscription(
        actor="module-admin",
        actor_workspace_id=10,
        workspace_id=10,
        target_id=1,
    )

    assert result.target_id == 1
    assert result.status == "active"
    assert result.grant_version == 1
    subscription = target_access_schema.execute(
        text(
            "SELECT workspace_id, target_id, status, granted_by "
            "FROM loophole_telegram_workspace_subscription"
        )
    ).mappings().one()
    assert dict(subscription) == {
        "workspace_id": 10,
        "target_id": 1,
        "status": "active",
        "granted_by": "module-admin",
    }
    audit = target_access_schema.execute(
        text(
            "SELECT actor_username, action, result, created_at "
            "FROM loophole_telegram_target_audit"
        )
    ).mappings().one()
    assert audit["actor_username"] == "module-admin"
    assert audit["action"] == "subscription_grant"
    assert audit["result"] == "allow"
    assert audit["created_at"] is not None


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"target_id": 2, "workspace_id": 10, "actor_workspace_id": 10}, TargetRedirectError),
        ({"target_id": 1, "workspace_id": 11, "actor_workspace_id": 10}, TargetAccessDenied),
    ],
)
def test_grant_rejects_redirect_or_foreign_workspace_fail_closed(
    target_access_schema, kwargs, error
):
    service = TargetAccessService(target_access_schema, capability_checker=_allowed)

    with pytest.raises(error):
        service.grant_workspace_subscription(actor="module-admin", **kwargs)

    assert target_access_schema.execute(
        text("SELECT count(*) FROM loophole_telegram_workspace_subscription")
    ).scalar_one() == 0


def test_grant_without_capability_is_denied_fail_closed(target_access_schema):
    service = TargetAccessService(target_access_schema, capability_checker=lambda *_: False)

    with pytest.raises(TargetAccessDenied):
        service.grant_workspace_subscription(
            actor="module-admin",
            actor_workspace_id=10,
            workspace_id=10,
            target_id=1,
        )

    assert target_access_schema.execute(
        text("SELECT count(*) FROM loophole_telegram_workspace_subscription")
    ).scalar_one() == 0


def test_revoke_increments_grant_version_and_keeps_subscription_history(target_access_schema):
    service = TargetAccessService(target_access_schema, capability_checker=_allowed)
    service.grant_workspace_subscription(
        actor="module-admin", actor_workspace_id=10, workspace_id=10, target_id=1
    )

    result = service.revoke_workspace_subscription(
        actor="module-admin", actor_workspace_id=10, workspace_id=10, target_id=1
    )

    assert result.status == "revoked"
    assert result.grant_version == 2
    subscription = target_access_schema.execute(
        text(
            "SELECT status, grant_version, revoked_at "
            "FROM loophole_telegram_workspace_subscription"
        )
    ).mappings().one()
    assert subscription["status"] == "revoked"
    assert subscription["grant_version"] == 2
    assert subscription["revoked_at"] is not None


def test_deactivation_fences_active_lease_without_deleting_checkpoint(target_access_schema):
    service = TargetAccessService(target_access_schema, capability_checker=_allowed)
    lease = service.start_collection(target_id=1)

    signal = service.deactivate_target(actor="module-admin", target_id=1)

    assert signal.target_id == 1
    assert signal.generation == lease.generation
    assert signal.fence_token == lease.fence_token
    assert signal.code == "target_deactivated"
    assert service.terminal_signal_for(lease) == signal
    target = target_access_schema.execute(
        text(
            "SELECT lifecycle_status, generation, fence_token, checkpoint_json "
            "FROM loophole_telegram_target WHERE target_id = 1"
        )
    ).mappings().one()
    assert target["lifecycle_status"] == "inactive"
    assert target["generation"] == lease.generation + 1
    assert target["fence_token"] == lease.fence_token + 1
    assert json.loads(target["checkpoint_json"])["sequence"] == 42
    with pytest.raises(TargetInactiveError):
        service.start_collection(target_id=1)


def test_reactivation_starts_new_fenced_lease_and_preserves_checkpoint(target_access_schema):
    service = TargetAccessService(target_access_schema, capability_checker=_allowed)
    service.deactivate_target(actor="module-admin", target_id=1)

    lifecycle = service.activate_target(actor="module-admin", target_id=1)
    lease = service.start_collection(target_id=1)

    assert lifecycle.status == "active"
    assert lifecycle.collection_mode == "incremental"
    assert lease.generation == 2
    assert lease.fence_token == 2


def test_migration_054_defines_subscription_audit_and_fenced_lifecycle_contract():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "migrations" / "054_loophole_target_access.sql").read_text(
        encoding="utf-8"
    )

    assert "canonical_target_id BIGINT" in sql
    assert "loophole_telegram_workspace_subscription" in sql
    assert "loophole_telegram_target_audit" in sql
    assert "loophole_telegram_terminal_signal" in sql
    assert "uq_lttws_workspace_target" in sql
    assert "uq_ltts_target_generation" in sql
