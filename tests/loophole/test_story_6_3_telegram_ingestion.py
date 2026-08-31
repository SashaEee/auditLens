"""Безопасный независимый Telegram ingress (Story 6.3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from bank_audit.loophole.telegram_ingestion import TelegramIngestionService, TelegramIngressItem


@pytest.fixture
def telegram_ingestion_schema(session):
    """Минимальная SQLite-проекция таблиц target access и ingress Story 6.3."""
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
                accepted_count INTEGER NOT NULL,
                quarantined_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
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
                ingestion_run_id INTEGER,
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
                ingestion_run_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (target_id, source_identity, source_version)
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
    return session


def _item(
    identity: str,
    version: str,
    sequence: int,
    *,
    kind: str = "post",
    text_value: object = "Новости банка",
    metadata: dict[str, object] | None = None,
    attachments: object | None = None,
) -> TelegramIngressItem:
    return TelegramIngressItem(
        identity=identity,
        version=version,
        object_kind=kind,
        sequence=sequence,
        text=text_value,
        metadata=metadata or {"author_id": "channel", "published_at": "2026-08-30T10:00:00+03:00"},
        attachments=attachments,
    )


def test_first_sync_keeps_available_history_as_sanitized_ingress_and_checkpoint(
    telegram_ingestion_schema,
):
    service = TelegramIngestionService(telegram_ingestion_schema)

    result = service.ingest(
        target_id=1,
        items=[
            _item("post:1", "1", 1, text_value="Позвоните +7 999 111-22-33"),
            _item("comment:1", "1", 2, kind="comment"),
        ],
    )

    assert result.sync_mode == "initial"
    assert result.accepted_count == 2
    assert result.quarantined_count == 0
    assert result.duplicate_count == 0
    assert result.checkpoint_after == {"sequence": 2}
    rows = telegram_ingestion_schema.execute(
        text(
            "SELECT source_identity, object_kind, sanitized_text, metadata_json "
            "FROM loophole_telegram_ingress ORDER BY sequence_no"
        )
    ).mappings().all()
    assert [row["source_identity"] for row in rows] == ["post:1", "comment:1"]
    assert rows[0]["sanitized_text"] == "Позвоните [PHONE_1]"
    assert json.loads(rows[0]["metadata_json"]) == {
        "author_id": "channel",
        "published_at": "2026-08-30T10:00:00+03:00",
    }
    checkpoint = telegram_ingestion_schema.execute(
        text("SELECT checkpoint_json FROM loophole_telegram_target WHERE target_id = 1")
    ).scalar_one()
    assert json.loads(checkpoint) == {"sequence": 2}
    run = telegram_ingestion_schema.execute(
        text(
            "SELECT sync_mode, checkpoint_before_json, checkpoint_after_json, accepted_count "
            "FROM loophole_telegram_ingestion_run"
        )
    ).mappings().one()
    assert run["sync_mode"] == "initial"
    assert run["checkpoint_before_json"] is None
    assert json.loads(run["checkpoint_after_json"]) == {"sequence": 2}
    assert run["accepted_count"] == 2


def test_incremental_sync_accepts_late_comment_and_deduplicates_identity_version(
    telegram_ingestion_schema,
):
    service = TelegramIngestionService(telegram_ingestion_schema)
    service.ingest(target_id=1, items=[_item("post:1", "1", 1)])

    result = service.ingest(
        target_id=1,
        items=[
            _item("post:1", "1", 1),
            _item("post:2", "1", 2),
            _item("comment:late", "1", 1, kind="comment"),
            _item("post:1", "2", 1, text_value="Исправленный пост"),
        ],
    )

    assert result.sync_mode == "incremental"
    assert result.accepted_count == 3
    assert result.duplicate_count == 1
    assert result.checkpoint_after == {"sequence": 2}
    assert telegram_ingestion_schema.execute(
        text("SELECT count(*) FROM loophole_telegram_ingress")
    ).scalar_one() == 4
    stored = telegram_ingestion_schema.execute(
        text(
            "SELECT source_identity, source_version FROM loophole_telegram_ingress "
            "ORDER BY source_identity, source_version"
        )
    ).all()
    assert stored == [("comment:late", "1"), ("post:1", "1"), ("post:1", "2"), ("post:2", "1")]


def test_uncertain_content_is_metadata_only_quarantine_without_raw_body_or_attachments(
    telegram_ingestion_schema,
):
    raw_body = "Секретный текст Иванов Иван Иванович +7 999 111-22-33"
    service = TelegramIngestionService(telegram_ingestion_schema)

    result = service.ingest(
        target_id=1,
        items=[
            _item(
                "post:unsafe",
                "1",
                3,
                text_value=raw_body,
                metadata={
                    "author_id": "channel",
                    "raw_body": raw_body,
                    "replacement_map": {"[PHONE_1]": "+7 999 111-22-33"},
                },
                attachments=[{"filename": "passport.jpg", "content": b"secret"}],
            )
        ],
    )

    assert result.accepted_count == 0
    assert result.quarantined_count == 1
    assert telegram_ingestion_schema.execute(
        text("SELECT count(*) FROM loophole_telegram_ingress")
    ).scalar_one() == 0
    quarantined = telegram_ingestion_schema.execute(
        text(
            "SELECT metadata_json, reason_code FROM loophole_telegram_ingress_quarantine "
            "WHERE source_identity = 'post:unsafe'"
        )
    ).mappings().one()
    payload = quarantined["metadata_json"]
    assert quarantined["reason_code"] == "attachments_not_approved"
    assert json.loads(payload) == {"author_id": "channel"}
    assert raw_body not in payload
    assert "replacement_map" not in payload
    assert "passport.jpg" not in payload
    assert "secret" not in payload


def test_untrusted_value_in_safe_metadata_key_is_quarantined_without_raw_text(
    telegram_ingestion_schema,
):
    raw_body = "Иванов Иван Иванович +7 999 111-22-33"
    service = TelegramIngestionService(telegram_ingestion_schema)

    result = service.ingest(
        target_id=1,
        items=[_item("post:unsafe-metadata", "1", 4, metadata={"author_id": raw_body})],
    )

    assert result.accepted_count == 0
    assert result.quarantined_count == 1
    metadata = telegram_ingestion_schema.execute(
        text(
            "SELECT metadata_json FROM loophole_telegram_ingress_quarantine "
            "WHERE source_identity = 'post:unsafe-metadata'"
        )
    ).scalar_one()
    assert json.loads(metadata) == {}
    assert raw_body not in metadata


def test_migration_055_defines_history_dedup_and_metadata_only_quarantine_contract():
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "055_loophole_telegram_ingestion.sql").read_text(
        encoding="utf-8"
    )

    assert "loophole_telegram_ingestion_run" in sql
    assert "loophole_telegram_ingress" in sql
    assert "loophole_telegram_ingress_quarantine" in sql
    assert "uq_ltti_target_identity_version" in sql
    assert "uq_lttq_target_identity_version" in sql
    assert "sanitized_text TEXT" in sql
    assert "attachments" not in sql.lower()
