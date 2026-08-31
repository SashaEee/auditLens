"""Тест миграции 026_loophole_content.sql: полный контент в loophole_record.

Без реальной БД: проверяем текст миграции, константы db_schema и состав
apply_migration (012 + 013 + 014 + 015 + 016). Идемпотентность — IF NOT EXISTS.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bank_audit.loophole import db_schema


def test_migration_016_file_exists():
    assert db_schema.MIGRATION_026_PATH.exists()
    sql = db_schema.migration_026_sql()
    assert sql.strip(), "миграция 016 пустая"


def test_migration_016_adds_content_columns():
    sql = db_schema.migration_026_sql()
    assert "ALTER TABLE loophole_record" in sql
    assert "ADD COLUMN IF NOT EXISTS content_status TEXT DEFAULT 'legacy'" in sql
    assert "ADD COLUMN IF NOT EXISTS raw_text_len INTEGER" in sql
    assert "ADD COLUMN IF NOT EXISTS raw_text_truncated BOOLEAN DEFAULT FALSE" in sql


def test_migration_016_has_status_index():
    sql = db_schema.migration_026_sql()
    assert (
        "CREATE INDEX IF NOT EXISTS idx_lr_content_status "
        "ON loophole_record(content_status)" in sql
    )


def test_migration_016_no_primary_key_or_unique():
    """Greenplum 6 — запрещены PRIMARY KEY / UNIQUE-конструкции."""
    sql = db_schema.migration_026_sql()
    lines = [line.split("--")[0] for line in sql.splitlines()]
    body = "\n".join(lines).upper()
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body


def test_migration_016_path_constant_defined():
    assert db_schema.MIGRATION_026_PATH.name == "026_loophole_content.sql"


def test_apply_migration_executes_six_migrations():
    """apply_migration выполняет 012 + 013 + 024 + 025 + 026 + 044."""
    session = MagicMock()
    db_schema.apply_migration(session)
    assert session.execute.call_count == 6
    texts = [str(call.args[0].text) for call in session.execute.call_args_list]
    assert any("idx_lr_content_status" in t for t in texts), "миграция 016 не выполнена"
