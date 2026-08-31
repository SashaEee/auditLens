"""Структурный тест миграции 015 (без выполнения: GP6-специфика)."""
from __future__ import annotations

from bank_audit.config import ROOT
from bank_audit.loophole import db_schema as schema


def test_migration_015_structure():
    sql = (ROOT / "migrations" / "025_loophole_parser_shared.sql").read_text(encoding="utf-8")
    for needle in (
        "CREATE TABLE IF NOT EXISTS loophole_parser_run",
        "run_trigger",
        "heal_report",
        "log_tail",
        "ADD COLUMN IF NOT EXISTS created_by",
        "ADD COLUMN IF NOT EXISTS cron_expr",
        "ADD COLUMN IF NOT EXISTS auto_enabled",
        "ADD COLUMN IF NOT EXISTS next_run_at",
        "ADD COLUMN IF NOT EXISTS source_keys",
        "ADD COLUMN IF NOT EXISTS heal_attempts",
        "ADD COLUMN IF NOT EXISTS parser_id",
        "ADD COLUMN IF NOT EXISTS text_sha256",
        "idx_lpr_parser",
        "idx_lr_url",
        "idx_lr_text_sha",
    ):
        assert needle in sql, needle
    # GP6: без PK/UNIQUE.
    assert "PRIMARY KEY" not in sql
    assert "UNIQUE" not in sql


def test_db_schema_exposes_015():
    assert schema.T_PARSER_RUN == "loophole_parser_run"
    text = schema.migration_025_sql()
    assert "loophole_parser_run" in text


def test_apply_migration_includes_015():
    calls: list[str] = []

    class _FakeSession:
        def execute(self, clause):
            calls.append(str(clause))

    schema.apply_migration(_FakeSession())
    assert len(calls) == 6
    assert "loophole_parser_run" in calls[3]
