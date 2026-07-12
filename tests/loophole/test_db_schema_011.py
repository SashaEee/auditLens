"""Тест миграции 011_loophole_agent.sql: структура, константы, идемпотентность.

Без реальной БД Greenplum: проверяем текст миграции и наличие констант/функций.
Идемпотентность гарантируется IF NOT EXISTS во всех CREATE TABLE / CREATE INDEX.
"""
from __future__ import annotations

import re

from bank_audit.loophole import db_schema


def test_migration_011_file_exists():
    assert db_schema.MIGRATION_011_PATH.exists()
    sql = db_schema.migration_011_sql()
    assert sql.strip(), "миграция 011 пустая"


def test_migration_011_contains_all_tables():
    sql = db_schema.migration_011_sql()
    for table in (
        "loophole_agent_task",
        "loophole_kb_example",
        "loophole_kb_doc",
        "loophole_parser",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"нет таблицы {table}"


def test_migration_011_has_indexes():
    sql = db_schema.migration_011_sql()
    for idx in (
        "idx_lat_workspace",
        "idx_lat_status",
        "idx_lat_phase",
        "idx_lkbe_category",
        "idx_lkbd_source",
        "idx_lp_workspace",
        "idx_lp_status",
    ):
        assert f"CREATE INDEX IF NOT EXISTS {idx}" in sql, f"нет индекса {idx}"


def test_migration_011_no_primary_key_or_unique():
    """Greenplum 6 — запрещены PRIMARY KEY / UNIQUE-конструкции."""
    sql = db_schema.migration_011_sql()
    lines = [line.split("--")[0] for line in sql.splitlines()]
    body = "\n".join(lines).upper()
    assert "PRIMARY KEY" not in body, "миграция 011 содержит PRIMARY KEY"
    assert "UNIQUE (" not in body and "UNIQUE(" not in body, "UNIQUE-ограничение в DDL 011"


def test_migration_011_is_idempotent_if_not_exists():
    """Все CREATE — с IF NOT EXISTS (повторное применение не падает)."""
    sql = db_schema.migration_011_sql()
    creates = re.findall(r"CREATE\s+(TABLE|INDEX)\s+(IF\s+NOT\s+EXISTS\s+)?", sql, re.I)
    assert creates, "нет CREATE-инструкций в 011"
    for kind, ifne in creates:
        assert ifne.strip().upper() == "IF NOT EXISTS", (
            f"CREATE {kind} без IF NOT EXISTS — миграция 011 не идемпотентна"
        )


def test_table_constants_defined():
    """Константы имён таблиц 011 определены в db_schema."""
    assert db_schema.T_AGENT_TASK == "loophole_agent_task"
    assert db_schema.T_KB_EXAMPLE == "loophole_kb_example"
    assert db_schema.T_KB_DOC == "loophole_kb_doc"
    assert db_schema.T_PARSER == "loophole_parser"


def test_migration_011_path_constant_defined():
    assert db_schema.MIGRATION_011_PATH.name == "011_loophole_agent.sql"


def test_apply_migration_executes_both_migrations():
    """apply_migration должна выполнять обе миграции (010 + 011)."""
    from unittest.mock import MagicMock

    session = MagicMock()
    db_schema.apply_migration(session)
    assert session.execute.call_count == 2
    texts = [str(call.args[0].text) for call in session.execute.call_args_list]
    assert any("loophole_record" in t for t in texts), "миграция 010 не выполнена"
    assert any("loophole_agent_task" in t for t in texts), "миграция 011 не выполнена"
