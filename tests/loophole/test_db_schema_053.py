"""Статическая проверка DDL внутреннего расписания аналитики Story 4.4."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_scheduled_analytics_migration_has_contract_and_private_result_without_raw_sql():
    sql = (ROOT / "migrations" / "053_loophole_scheduled_analytics.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS loophole_scheduled_query" in sql
    assert "query_id TEXT NOT NULL" in sql
    assert "query_version INTEGER NOT NULL" in sql
    assert "workspace_id BIGINT NOT NULL" in sql
    assert "owner_username TEXT NOT NULL" in sql
    assert "recipient_username TEXT NOT NULL" in sql
    assert "CREATE TABLE IF NOT EXISTS loophole_scheduled_result" in sql
    assert "result_json JSONB NOT NULL" in sql
    assert "sql TEXT" not in sql.lower()
