"""Тест миграции 010_loophole.sql: структура, идемпотентность, отсутствие PK/UNIQUE.

Без реальной БД Greenplum: проверяем текст миграции и вызов apply_migration
через мок-сессию. Идемпотентность гарантируется конструкцией IF NOT EXISTS
во всех CREATE TABLE / CREATE INDEX.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from bank_audit.loophole import db_schema


def test_migration_file_exists():
    assert db_schema.MIGRATION_PATH.exists()
    sql = db_schema.migration_sql()
    assert sql.strip(), "миграция пустая"


def test_migration_contains_all_tables():
    sql = db_schema.migration_sql()
    for table in (
        "loophole_keyword",
        "loophole_record",
        "loophole_workspace",
        "loophole_result",
        "loophole_chat_message",
        "loophole_action_log",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"нет таблицы {table}"


def test_migration_has_indexes():
    sql = db_schema.migration_sql()
    for idx in (
        "idx_lk_keyword",
        "idx_lr_sha",
        "idx_lr_loophole",
        "idx_lr_bank",
        "idx_lw_user",
        "idx_lcm_ws",
        "idx_lal_user",
    ):
        assert f"CREATE INDEX IF NOT EXISTS {idx}" in sql, f"нет индекса {idx}"


def test_migration_no_primary_key_or_unique():
    """Greenplum 6 — запрещены PRIMARY KEY / UNIQUE-конструкции."""
    sql = db_schema.migration_sql()
    # Вырезаем комментарии, чтобы не поймать слово в комментарии.
    lines = []
    for line in sql.splitlines():
        stripped = line.split("--")[0]
        lines.append(stripped)
    body = "\n".join(lines).upper()
    assert "PRIMARY KEY" not in body, "миграция содержит PRIMARY KEY"
    assert "UNIQUE" not in body or "UNIQUE" not in body.replace("UNIQUE(", ""), (
        "миграция содержит UNIQUE-конструкцию"
    )
    # Точнее: не должно быть UNIQUE-ограничения в DDL.
    assert "UNIQUE (" not in body and "UNIQUE(" not in body, "UNIQUE-ограничение в DDL"


def test_migration_is_idempotent_if_not_exists():
    """Все CREATE — с IF NOT EXISTS (повторное применение не падает)."""
    sql = db_schema.migration_sql()
    # Каждый CREATE TABLE / CREATE INDEX должен сопровождаться IF NOT EXISTS.
    import re

    creates = re.findall(r"CREATE\s+(TABLE|INDEX)\s+(IF\s+NOT\s+EXISTS\s+)?", sql, re.I)
    assert creates, "нет CREATE-инструкций"
    for kind, ifne in creates:
        assert ifne.strip().upper() == "IF NOT EXISTS", (
            f"CREATE {kind} без IF NOT EXISTS — миграция не идемпотентна"
        )


def test_apply_migration_calls_session_execute():
    session = MagicMock()
    db_schema.apply_migration(session)
    # 010 + 011 — две миграции выполняются идемпотентно.
    assert session.execute.call_count == 2
    # Переданы объекты text() с SQL обеих миграций.
    texts = [str(call.args[0].text) for call in session.execute.call_args_list]
    assert any("loophole_record" in t for t in texts), "миграция 010 не выполнена"
    assert any("loophole_agent_task" in t for t in texts), "миграция 011 не выполнена"
