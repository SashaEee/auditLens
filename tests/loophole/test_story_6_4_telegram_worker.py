"""TDD-контракты устойчивого Telegram worker-а (Story 6.4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from bank_audit.loophole.telegram_ingestion import TelegramIngressItem
from bank_audit.loophole.telegram_worker import StaleTelegramWorkerError, TelegramWorkerService


@pytest.fixture
def worker_schema(session):
    """SQLite-проекция lifecycle, ingress и durable-состояния worker-а."""
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
            CREATE TABLE loophole_telegram_ingestion_run (
                ingestion_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                sync_mode TEXT NOT NULL,
                checkpoint_before_json TEXT,
                checkpoint_after_json TEXT,
                accepted_count INTEGER NOT NULL DEFAULT 0,
                quarantined_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_ingress (
                ingress_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                source_identity TEXT NOT NULL,
                source_version TEXT NOT NULL,
                object_kind TEXT NOT NULL,
                sequence_no INTEGER,
                sanitized_text TEXT,
                metadata_json TEXT NOT NULL,
                ingestion_run_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (target_id, source_identity, source_version)
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_ingress_quarantine (
                quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                source_identity TEXT NOT NULL,
                source_version TEXT NOT NULL,
                object_kind TEXT NOT NULL,
                sequence_no INTEGER,
                metadata_json TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                ingestion_run_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (target_id, source_identity, source_version)
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_worker_global_lease (
                lease_name TEXT PRIMARY KEY,
                owner_id TEXT,
                fence_token INTEGER NOT NULL DEFAULT 0,
                lease_until TEXT NOT NULL
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_worker_target_lease (
                target_id INTEGER PRIMARY KEY,
                owner_id TEXT,
                global_fence_token INTEGER NOT NULL DEFAULT 0,
                target_fence_token INTEGER NOT NULL DEFAULT 0,
                lifecycle_fence_token INTEGER NOT NULL DEFAULT 0,
                lease_until TEXT NOT NULL
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_worker_attempt (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                owner_id TEXT NOT NULL,
                global_fence_token INTEGER NOT NULL,
                target_fence_token INTEGER NOT NULL,
                lifecycle_fence_token INTEGER NOT NULL,
                sync_mode TEXT NOT NULL,
                checkpoint_before_json TEXT,
                checkpoint_after_json TEXT,
                accepted_count INTEGER NOT NULL DEFAULT 0,
                quarantined_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                lease_until TEXT NOT NULL,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_worker_outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL UNIQUE,
                target_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    session.execute(
        text(
            """
            CREATE TABLE loophole_telegram_worker_journal (
                journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER,
                target_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                sync_mode TEXT,
                checkpoint_before_json TEXT,
                checkpoint_after_json TEXT,
                accepted_count INTEGER NOT NULL DEFAULT 0,
                quarantined_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER,
                error_code TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    session.execute(
        text(
            """
            INSERT INTO loophole_telegram_target
                (target_id, normalized_address, target_kind, lifecycle_status)
            VALUES (1, 't.me/bank_news', 'public', 'active')
            """
        )
    )
    session.execute(
        text(
            """
            INSERT INTO loophole_telegram_worker_global_lease
                (lease_name, owner_id, fence_token, lease_until)
            VALUES ('telegram-worker', NULL, 0, '2000-01-01 00:00:00')
            """
        )
    )
    return session


def _item(identity: str, sequence: int, text_value: str = "Безопасное сообщение") -> TelegramIngressItem:
    return TelegramIngressItem(
        identity=identity,
        version="1",
        object_kind="post",
        sequence=sequence,
        text=text_value,
        metadata={"author_id": "channel", "published_at": "2026-08-30T10:00:00+03:00"},
    )


def _lease(service: TelegramWorkerService):
    global_lease = service.acquire_global_lease()
    assert global_lease is not None
    target_lease = service.acquire_target_lease(target_id=1, global_lease=global_lease)
    assert target_lease is not None
    return target_lease


def test_stale_worker_cannot_write_batch_or_checkpoint_after_lease_is_replaced(worker_schema):
    stale_service = TelegramWorkerService(worker_schema, owner_id="worker-old", lease_seconds=60)
    stale_lease = _lease(stale_service)
    stale_attempt = stale_service.start_attempt(stale_lease)
    worker_schema.execute(
        text(
            "UPDATE loophole_telegram_worker_global_lease "
            "SET lease_until = '2000-01-01 00:00:00'"
        )
    )
    worker_schema.execute(
        text(
            "UPDATE loophole_telegram_worker_target_lease "
            "SET lease_until = '2000-01-01 00:00:00'"
        )
    )
    fresh_service = TelegramWorkerService(worker_schema, owner_id="worker-new", lease_seconds=60)
    fresh_lease = _lease(fresh_service)

    with pytest.raises(StaleTelegramWorkerError):
        stale_service.ingest_batch(stale_attempt, stale_lease, [_item("post:stale", 1)])

    assert worker_schema.execute(text("SELECT count(*) FROM loophole_telegram_ingress")).scalar_one() == 0
    assert worker_schema.execute(
        text("SELECT checkpoint_json FROM loophole_telegram_target WHERE target_id = 1")
    ).scalar_one() is None

    fresh_attempt = fresh_service.start_attempt(fresh_lease)
    result = fresh_service.ingest_batch(fresh_attempt, fresh_lease, [_item("post:fresh", 1)])
    assert result.accepted_count == 1
    assert result.checkpoint_after == {"sequence": 1}


def test_reaper_terminalizes_expired_attempt_once_and_new_owner_resumes_checkpoint(worker_schema):
    old_service = TelegramWorkerService(worker_schema, owner_id="worker-old", lease_seconds=60)
    old_lease = _lease(old_service)
    old_attempt = old_service.start_attempt(old_lease)
    old_service.ingest_batch(old_attempt, old_lease, [_item("post:stable", 5)])
    worker_schema.execute(
        text(
            "UPDATE loophole_telegram_worker_attempt SET lease_until = '2000-01-01 00:00:00' "
            "WHERE attempt_id = :attempt_id"
        ),
        {"attempt_id": old_attempt.attempt_id},
    )
    worker_schema.execute(
        text("UPDATE loophole_telegram_worker_global_lease SET lease_until = '2000-01-01 00:00:00'")
    )
    worker_schema.execute(
        text("UPDATE loophole_telegram_worker_target_lease SET lease_until = '2000-01-01 00:00:00'")
    )

    assert old_service.reap_expired_attempts() == 1
    assert old_service.reap_expired_attempts() == 0
    assert worker_schema.execute(
        text("SELECT count(*) FROM loophole_telegram_worker_outbox WHERE attempt_id = :attempt_id"),
        {"attempt_id": old_attempt.attempt_id},
    ).scalar_one() == 1

    new_service = TelegramWorkerService(worker_schema, owner_id="worker-new", lease_seconds=60)
    new_lease = _lease(new_service)
    new_attempt = new_service.start_attempt(new_lease)
    result = new_service.ingest_batch(
        new_attempt,
        new_lease,
        [_item("post:stable", 5), _item("post:next", 6)],
    )

    assert new_attempt.checkpoint_before == {"sequence": 5}
    assert result.accepted_count == 1
    assert result.duplicate_count == 1
    assert result.checkpoint_after == {"sequence": 6}
    assert worker_schema.execute(text("SELECT count(*) FROM loophole_telegram_ingress")).scalar_one() == 2


def test_slo_uses_24h_attempt_journal_and_never_serializes_message_body(worker_schema):
    service = TelegramWorkerService(worker_schema, owner_id="worker-1", lease_seconds=60)
    lease = _lease(service)
    attempt = service.start_attempt(lease)
    service.ingest_batch(attempt, lease, [_item("post:secret", 1, "Секретное тело сообщения")])
    service.complete_attempt(attempt, lease)

    assert service.slo_violations() == []
    journal = worker_schema.execute(
        text("SELECT event_type, checkpoint_before_json, checkpoint_after_json, error_code "
             "FROM loophole_telegram_worker_journal ORDER BY journal_id")
    ).mappings().all()
    serialized = json.dumps([dict(row) for row in journal], ensure_ascii=False)
    assert [row["event_type"] for row in journal] == ["attempt_started", "batch_finished", "attempt_finished"]
    assert "Секретное тело сообщения" not in serialized
    assert "post:secret" not in serialized
    assert "raw_body" not in serialized


def test_migration_056_defines_fenced_leases_reaper_outbox_and_safe_journal_contract():
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "056_loophole_telegram_worker.sql").read_text(
        encoding="utf-8"
    )

    assert "loophole_telegram_worker_global_lease" in sql
    assert "loophole_telegram_worker_target_lease" in sql
    assert "fence_token" in sql
    assert "loophole_telegram_worker_attempt" in sql
    assert "loophole_telegram_worker_outbox" in sql
    assert "loophole_telegram_worker_journal" in sql
    assert "sanitized_text" not in sql.lower()
    assert "raw_body" not in sql.lower()
