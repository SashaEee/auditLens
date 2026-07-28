"""Тест миграции 014_loophole_manual_mark.sql: record_id в loophole_kb_example.

Без реальной БД: проверяем текст миграции, константы db_schema и состав
apply_migration (010 + 011 + 014). Идемпотентность — IF NOT EXISTS.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bank_audit.loophole import db_schema


def test_migration_014_file_exists():
    assert db_schema.MIGRATION_014_PATH.exists()
    sql = db_schema.migration_014_sql()
    assert sql.strip(), "миграция 014 пустая"


def test_migration_014_adds_record_id_column():
    sql = db_schema.migration_014_sql()
    assert "ALTER TABLE loophole_kb_example" in sql
    assert "ADD COLUMN IF NOT EXISTS record_id BIGINT" in sql


def test_migration_014_has_record_index():
    sql = db_schema.migration_014_sql()
    assert (
        "CREATE INDEX IF NOT EXISTS idx_lkbe_record "
        "ON loophole_kb_example(record_id)" in sql
    )


def test_migration_014_no_primary_key_or_unique():
    """Greenplum 6 — запрещены PRIMARY KEY / UNIQUE-конструкции."""
    sql = db_schema.migration_014_sql()
    lines = [line.split("--")[0] for line in sql.splitlines()]
    body = "\n".join(lines).upper()
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body


def test_migration_014_path_constant_defined():
    assert db_schema.MIGRATION_014_PATH.name == "014_loophole_manual_mark.sql"


def test_apply_migration_executes_five_migrations():
    """apply_migration выполняет 012 + 013 + 014 + 015 + 016."""
    session = MagicMock()
    db_schema.apply_migration(session)
    assert session.execute.call_count == 5
    texts = [str(call.args[0].text) for call in session.execute.call_args_list]
    assert any("loophole_record" in t for t in texts), "миграция 012 не выполнена"
    assert any("loophole_agent_task" in t for t in texts), "миграция 013 не выполнена"
    assert any("idx_lkbe_record" in t for t in texts), "миграция 014 не выполнена"
    assert any("loophole_parser_run" in t for t in texts), "миграция 015 не выполнена"
    assert any("idx_lr_content_status" in t for t in texts), "миграция 016 не выполнена"
