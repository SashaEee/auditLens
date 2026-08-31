"""Контракт и fail-closed выполнение расписаний аналитики Story 4.4."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from bank_audit.clock import MSK
from bank_audit.loophole.scheduled_analytics import ScheduledAnalyticsService
from bank_audit.loophole.web import ScheduledAnalyticsRequest


def _create_schedule_schema(session) -> None:
    session.execute(text("""
        CREATE TABLE loophole_scheduled_query (
            scheduled_query_id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_id TEXT NOT NULL,
            query_version INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            owner_username TEXT NOT NULL,
            recipient_username TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            next_run_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE TABLE loophole_scheduled_result (
            scheduled_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_query_id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            owner_username TEXT NOT NULL,
            recipient_username TEXT NOT NULL,
            result_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))
    session.execute(text("""
        CREATE VIEW loophole_published_catalog_v1 AS
        SELECT bank_slug, status FROM loophole_record
        WHERE status = 'published' AND is_loophole = TRUE
    """))


def _activate_member(session, username: str) -> None:
    session.execute(
        text("INSERT INTO loophole_workspace_membership (username, status) VALUES (:u, 'active')"),
        {"u": username},
    )


def test_schedule_contract_stores_named_query_not_raw_sql_and_keeps_result_private(session):
    _create_schedule_schema(session)
    _activate_member(session, "owner")
    _activate_member(session, "recipient")
    session.execute(text("INSERT INTO loophole_workspace (user_id, name) VALUES ('owner', 'private')"))
    session.execute(text(
        "INSERT INTO loophole_record (sha256, bank_slug, status, is_loophole) "
        "VALUES ('published', 'bank', 'published', 1)"
    ))
    now = datetime(2026, 8, 30, 10, 0, 0, tzinfo=MSK)
    service = ScheduledAnalyticsService(session)

    contract = service.enable(
        query_id="published_cases_by_bank",
        workspace_id=1,
        owner_username="owner",
        recipient_username="recipient",
        cron_expr="0 * * * *",
        expires_at=now + timedelta(days=2),
        now=now,
    )
    row = session.execute(text("SELECT * FROM loophole_scheduled_query")).mappings().one()

    assert contract["contract_version"] == "ScheduledQueryContract v1"
    assert "sql" not in contract
    assert row["query_id"] == "published_cases_by_bank"
    assert row["query_version"] == 1

    service.run_due(now=contract["next_run_at"])
    result = session.execute(text("SELECT * FROM loophole_scheduled_result")).mappings().one()
    assert result["workspace_id"] == 1
    assert result["owner_username"] == "owner"
    assert result["recipient_username"] == "recipient"
    assert datetime.fromisoformat(str(result["expires_at"])) <= contract["next_run_at"] + timedelta(hours=24)


def test_schedule_skips_once_without_query_when_recipient_membership_revoked(session):
    _create_schedule_schema(session)
    _activate_member(session, "owner")
    _activate_member(session, "recipient")
    session.execute(text("INSERT INTO loophole_workspace (user_id) VALUES ('owner')"))
    now = datetime(2026, 8, 30, 10, 0, 0, tzinfo=MSK)
    service = ScheduledAnalyticsService(session)
    contract = service.enable(
        query_id="published_cases_by_bank",
        workspace_id=1,
        owner_username="owner",
        recipient_username="recipient",
        cron_expr="0 * * * *",
        expires_at=now + timedelta(days=2),
        now=now,
    )
    session.execute(text(
        "UPDATE loophole_workspace_membership SET status = 'revoked' WHERE username = 'recipient'"
    ))

    assert service.run_due(now=contract["next_run_at"]) == []
    assert session.execute(text("SELECT COUNT(*) FROM loophole_scheduled_result")).scalar_one() == 0
    actions = session.execute(text(
        "SELECT action FROM loophole_action_log WHERE action = 'schedule_skipped_recipient_membership'"
    )).scalars().all()
    assert actions == ["schedule_skipped_recipient_membership"]


def test_schedule_result_acl_rejects_another_member(session):
    _create_schedule_schema(session)
    _activate_member(session, "owner")
    _activate_member(session, "recipient")
    _activate_member(session, "outsider")
    session.execute(text("INSERT INTO loophole_workspace (user_id) VALUES ('owner')"))
    now = datetime(2026, 8, 30, 10, 0, 0, tzinfo=MSK)
    service = ScheduledAnalyticsService(session)
    contract = service.enable(
        query_id="published_cases_by_bank",
        workspace_id=1,
        owner_username="owner",
        recipient_username="recipient",
        cron_expr="0 * * * *",
        expires_at=now + timedelta(days=2),
        now=now,
    )

    import pytest

    with pytest.raises(PermissionError, match="Нет доступа"):
        service.list_results(contract["scheduled_query_id"], username="outsider")


def test_schedule_api_contract_rejects_raw_sql_field():
    with pytest.raises(ValidationError):
        ScheduledAnalyticsRequest(
            query_id="published_cases_by_bank",
            workspace_id=1,
            recipient_username="recipient",
            cron_expr="0 * * * *",
            expires_at="2026-08-31T10:00:00+03:00",
            sql="SELECT * FROM loophole_record",
        )
