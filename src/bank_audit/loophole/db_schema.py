"""SQL-хелперы модуля loophole: имена таблиц и загрузка миграций.

Весь SQL — через sqlalchemy.text(), без ORM. Миграции 012_loophole.sql,
013_loophole_agent.sql, 024_loophole_manual_mark.sql и
025_loophole_parser_shared.sql и 026_loophole_content.sql идемпотентны
(CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / ADD COLUMN IF NOT EXISTS),
диалект Greenplum 6 (без PRIMARY KEY / UNIQUE).
"""
from __future__ import annotations

from sqlalchemy import text

from ..config import ROOT

MIGRATION_PATH = ROOT / "migrations" / "012_loophole.sql"
MIGRATION_011_PATH = ROOT / "migrations" / "013_loophole_agent.sql"
MIGRATION_024_PATH = ROOT / "migrations" / "024_loophole_manual_mark.sql"
MIGRATION_025_PATH = ROOT / "migrations" / "025_loophole_parser_shared.sql"
MIGRATION_026_PATH = ROOT / "migrations" / "026_loophole_content.sql"

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
T_PARSER_RUN = "loophole_parser_run"

# Авторизация модуля (миграция 042_loophole_authorization.sql).
T_PRINCIPAL = "loophole_principal"
T_MEMBERSHIP = "loophole_workspace_membership"
T_ROLE_ASSIGNMENT = "loophole_role_assignment"
T_AUTH_AUDIT = "loophole_auth_audit"


def migration_sql() -> str:
    """Возвращает текст миграции 012_loophole.sql."""
    return MIGRATION_PATH.read_text(encoding="utf-8")


def migration_011_sql() -> str:
    """Возвращает текст миграции 013_loophole_agent.sql."""
    return MIGRATION_011_PATH.read_text(encoding="utf-8")


def migration_024_sql() -> str:
    """Возвращает текст миграции 024_loophole_manual_mark.sql."""
    return MIGRATION_024_PATH.read_text(encoding="utf-8")


def migration_025_sql() -> str:
    """Возвращает текст миграции 025_loophole_parser_shared.sql."""
    return MIGRATION_025_PATH.read_text(encoding="utf-8")


def migration_026_sql() -> str:
    """Возвращает текст миграции 026_loophole_content.sql."""
    return MIGRATION_026_PATH.read_text(encoding="utf-8")


def apply_migration(session) -> None:
    """Применяет миграции 012 + 013 + 014 + 015 + 016 (идемпотентно)."""
    session.execute(text(migration_sql()))
    session.execute(text(migration_011_sql()))
    session.execute(text(migration_024_sql()))
    session.execute(text(migration_025_sql()))
    session.execute(text(migration_026_sql()))
