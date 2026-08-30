"""SQL-хелперы модуля loophole: имена таблиц и загрузка миграций.

Весь SQL — через sqlalchemy.text(), без ORM. Миграции 012_loophole.sql,
013_loophole_agent.sql, 024_loophole_manual_mark.sql,
025_loophole_parser_shared.sql, 026_loophole_content.sql и
044_loophole_agent_audit.sql идемпотентны
(CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / ADD COLUMN IF NOT EXISTS),
диалект Greenplum 6 (без PRIMARY KEY / UNIQUE).
"""
from __future__ import annotations

import os
import uuid

from sqlalchemy import text

from ..config import ROOT

MIGRATION_PATH = ROOT / "migrations" / "012_loophole.sql"
MIGRATION_011_PATH = ROOT / "migrations" / "013_loophole_agent.sql"
MIGRATION_024_PATH = ROOT / "migrations" / "024_loophole_manual_mark.sql"
MIGRATION_025_PATH = ROOT / "migrations" / "025_loophole_parser_shared.sql"
MIGRATION_026_PATH = ROOT / "migrations" / "026_loophole_content.sql"
MIGRATION_044_PATH = ROOT / "migrations" / "044_loophole_agent_audit.sql"

T_KEYWORD = "loophole_keyword"
T_RECORD = "loophole_record"
T_WORKSPACE = "loophole_workspace"
T_RESULT = "loophole_result"
T_CHAT_MESSAGE = "loophole_chat_message"
T_ACTION_LOG = "loophole_action_log"
T_AGENT_AUDIT_LOG = "agent_audit_log"

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


def migration_044_sql() -> str:
    """Возвращает текст миграции 044_loophole_agent_audit.sql."""
    return MIGRATION_044_PATH.read_text(encoding="utf-8")


def apply_migration(session) -> None:
    """Применяет миграции loophole, включая журнал аудита агента."""
    session.execute(text(migration_sql()))
    session.execute(text(migration_011_sql()))
    session.execute(text(migration_024_sql()))
    session.execute(text(migration_025_sql()))
    session.execute(text(migration_026_sql()))
    session.execute(text(migration_044_sql()))


def verify_migration_044_postgres(staging_url: str | None = None) -> dict[str, str]:
    """Проверяет миграцию 044 только на явно выделенном PostgreSQL staging.

    Без staging возвращается ``UNVERIFIED``: SQLite и обычный ``DATABASE_URL``
    намеренно не считаются доказательством PostgreSQL-совместимости.
    """
    url = staging_url or os.getenv("AUDITLENS_POSTGRES_STAGING_URL")
    if not url:
        return {
            "status": "UNVERIFIED",
            "reason": "Не задан PostgreSQL staging для проверки миграции 044",
        }
    scheme = url.partition("://")[0].lower()
    if scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
        return {
            "status": "UNVERIFIED",
            "reason": "Для verification нужен PostgreSQL staging, SQLite не подходит",
        }

    try:
        import psycopg
    except ImportError:
        return {
            "status": "UNVERIFIED",
            "reason": "Драйвер psycopg недоступен для PostgreSQL staging",
        }

    connect_url = (
        url.replace("postgresql+psycopg://", "postgresql://", 1)
        if scheme == "postgresql+psycopg"
        else url
    )
    try:
        connection = psycopg.connect(connect_url)
    except Exception as exc:  # noqa: BLE001 — недоступный staging честно UNVERIFIED
        return {
            "status": "UNVERIFIED",
            "reason": f"PostgreSQL staging недоступен: {type(exc).__name__}",
        }

    try:
        for _ in range(2):
            with connection.cursor() as cursor:
                cursor.execute(migration_044_sql())
            connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'agent_audit_log'
                """
            )
            columns = {row[0]: row[1] for row in cursor.fetchall()}
            expected_columns = {
                "audit_id": "bigint",
                "run_id": "text",
                "user_id": "text",
                "workspace_id": "bigint",
                "query_redacted": "text",
                "tools_used": "jsonb",
                "duration_ms": "integer",
                "result_redacted": "text",
                "status": "text",
            }
            if any(columns.get(name) != kind for name, kind in expected_columns.items()):
                raise RuntimeError("структура agent_audit_log не совпала с PostgreSQL-контрактом")

            cursor.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'agent_audit_log'
                """
            )
            indexes = {row[0] for row in cursor.fetchall()}
            if not {"idx_agent_audit_run", "idx_agent_audit_user"}.issubset(indexes):
                raise RuntimeError("индексы agent_audit_log не созданы")

            cursor.execute(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgrelid = 'agent_audit_log'::regclass
                  AND NOT tgisinternal
                """
            )
            triggers = {row[0] for row in cursor.fetchall()}
            if "trg_agent_audit_append_only" not in triggers:
                raise RuntimeError("append-only trigger agent_audit_log не создан")

        insert_sql = (
            "INSERT INTO agent_audit_log "
            "(run_id, user_id, query_redacted, tools_used, duration_ms, result_redacted, status) "
            "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)"
        )

        class _MutationAllowed(Exception):
            pass

        def mutation_is_denied(statement: str) -> bool:
            run_id = f"migration-044-verification-{uuid.uuid4().hex}"
            try:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(
                        insert_sql,
                        (run_id, "verification", "проверка", "[]", 0, "проверка", "completed"),
                    )
                    cursor.execute(statement, (run_id,))
                    raise _MutationAllowed
            except _MutationAllowed:
                return False
            except psycopg.errors.RaiseException:
                return True

        if not mutation_is_denied(
            "UPDATE agent_audit_log SET status = 'changed' WHERE run_id = %s"
        ):
            raise RuntimeError("UPDATE agent_audit_log не заблокирован")
        if not mutation_is_denied("DELETE FROM agent_audit_log WHERE run_id = %s"):
            raise RuntimeError("DELETE agent_audit_log не заблокирован")
        return {
            "status": "VERIFIED",
            "reason": "PostgreSQL staging подтвердил apply/re-run и append-only ограничения 044",
        }
    except Exception as exc:  # noqa: BLE001 — доступный staging не маскируем под PASS
        connection.rollback()
        return {
            "status": "FAILED",
            "reason": f"Проверка PostgreSQL staging завершилась ошибкой: {type(exc).__name__}",
        }
    finally:
        connection.close()
