"""SQL-хелперы модуля loophole: имена таблиц и загрузка миграций.

Весь SQL — через sqlalchemy.text(), без ORM. Миграции 010_loophole.sql и
011_loophole_agent.sql идемпотентны (CREATE TABLE IF NOT EXISTS /
CREATE INDEX IF NOT EXISTS), диалект Greenplum 6 (без PRIMARY KEY / UNIQUE).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from ..config import ROOT

MIGRATION_PATH = ROOT / "migrations" / "012_loophole.sql"
MIGRATION_011_PATH = ROOT / "migrations" / "013_loophole_agent.sql"

T_KEYWORD = "loophole_keyword"
T_RECORD = "loophole_record"
T_WORKSPACE = "loophole_workspace"
T_RESULT = "loophole_result"
T_CHAT_MESSAGE = "loophole_chat_message"
T_ACTION_LOG = "loophole_action_log"

T_AGENT_TASK = "loophole_agent_task"
T_KB_EXAMPLE = "loophole_kb_example"
T_KB_DOC = "loophole_kb_doc"
T_PARSER = "loophole_parser"


def migration_sql() -> str:
    """Возвращает текст миграции 010_loophole.sql."""
    return MIGRATION_PATH.read_text(encoding="utf-8")


def migration_011_sql() -> str:
    """Возвращает текст миграции 011_loophole_agent.sql."""
    return MIGRATION_011_PATH.read_text(encoding="utf-8")


def apply_migration(session) -> None:
    """Применяет миграции 010 + 011 к переданной SQLAlchemy-сессии (идемпотентно)."""
    session.execute(text(migration_sql()))
    session.execute(text(migration_011_sql()))
