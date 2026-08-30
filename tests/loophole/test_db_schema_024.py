"""Тест миграции 024_loophole_manual_mark.sql: record_id в loophole_kb_example.

Без реальной БД: проверяем текст миграции, константы db_schema и состав
apply_migration (012 + 013 + 024 + 025 + 026 + 044). Идемпотентность — IF NOT EXISTS.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bank_audit.loophole import db_schema


def test_migration_014_file_exists():
    assert db_schema.MIGRATION_024_PATH.exists()
    sql = db_schema.migration_024_sql()
    assert sql.strip(), "миграция 014 пустая"


def test_migration_014_adds_record_id_column():
    sql = db_schema.migration_024_sql()
    assert "ALTER TABLE loophole_kb_example" in sql
    assert "ADD COLUMN IF NOT EXISTS record_id BIGINT" in sql


def test_migration_014_has_record_index():
    sql = db_schema.migration_024_sql()
    assert (
        "CREATE INDEX IF NOT EXISTS idx_lkbe_record "
        "ON loophole_kb_example(record_id)" in sql
    )


def test_migration_014_no_primary_key_or_unique():
    """Greenplum 6 — запрещены PRIMARY KEY / UNIQUE-конструкции."""
    sql = db_schema.migration_024_sql()
    lines = [line.split("--")[0] for line in sql.splitlines()]
    body = "\n".join(lines).upper()
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body


def test_migration_014_path_constant_defined():
    assert db_schema.MIGRATION_024_PATH.name == "024_loophole_manual_mark.sql"


def test_apply_migration_executes_six_migrations():
    """apply_migration выполняет 012 + 013 + 024 + 025 + 026 + 044."""
    session = MagicMock()
    db_schema.apply_migration(session)
    assert session.execute.call_count == 6
    texts = [str(call.args[0].text) for call in session.execute.call_args_list]
    assert any("loophole_record" in t for t in texts), "миграция 012 не выполнена"
    assert any("loophole_agent_task" in t for t in texts), "миграция 013 не выполнена"
    assert any("idx_lkbe_record" in t for t in texts), "миграция 014 не выполнена"
    assert any("loophole_parser_run" in t for t in texts), "миграция 015 не выполнена"
    assert any("idx_lr_content_status" in t for t in texts), "миграция 016 не выполнена"


def test_apply_migration_includes_agent_audit_log_migration():
    """Локальный helper обязан применять DDL agent_audit_log из миграции 044."""
    session = MagicMock()
    db_schema.apply_migration(session)
    texts = [str(call.args[0].text) for call in session.execute.call_args_list]

    assert db_schema.MIGRATION_044_PATH.name == "044_loophole_agent_audit.sql"
    assert any(
        "CREATE TABLE IF NOT EXISTS agent_audit_log" in sql
        for sql in texts
    )


def test_migration_044_skips_postgres_only_trigger_on_greenplum():
    """Greenplum 6 применяет audit table, но не выполняет неподдерживаемый trigger DDL."""
    sql = db_schema.migration_044_sql()
    guard = "IF version() NOT ILIKE '%Greenplum%' THEN"

    assert guard in sql
    before_guard = sql[:sql.index(guard)].upper()
    assert "CREATE TRIGGER" not in before_guard
    assert sql.index(guard) < sql.index("CREATE OR REPLACE FUNCTION")
    assert sql.index(guard) < sql.index("CREATE TRIGGER")


def test_migration_044_verification_harness_reports_unverified_without_staging(monkeypatch):
    """Без явного PostgreSQL staging результат не считается проверенным."""
    monkeypatch.delenv("AUDITLENS_POSTGRES_STAGING_URL", raising=False)

    verifier = getattr(db_schema, "verify_migration_044_postgres", None)
    assert callable(verifier), "нет PostgreSQL verification harness для миграции 044"
    result = verifier()

    assert result["status"] == "UNVERIFIED"
    assert "staging" in result["reason"].lower()


def test_migration_044_verification_harness_rejects_sqlite_as_staging(monkeypatch):
    """SQLite не может маскироваться под PostgreSQL verification staging."""
    monkeypatch.setenv("AUDITLENS_POSTGRES_STAGING_URL", "sqlite:///:memory:")

    result = db_schema.verify_migration_044_postgres()

    assert result["status"] == "UNVERIFIED"
    assert "postgres" in result["reason"].lower()


def test_migration_044_verifies_explicit_postgres_staging_or_marks_unverified():
    """На явном staging harness проверяет apply, повторный запуск и ограничения."""
    result = db_schema.verify_migration_044_postgres()

    if result["status"] == "UNVERIFIED":
        import pytest

        pytest.skip(f"UNVERIFIED: {result['reason']}")
    assert result["status"] == "VERIFIED"
