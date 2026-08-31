"""Именованные внутренние аналитические задачи и их fail-closed расписание."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from ..clock import MSK
from . import authorization
from . import repository as repo
from .analytics import execute_analytics_query
from .parsers.scheduler import next_run


@dataclass(frozen=True)
class NamedAnalyticsQuery:
    """Версионированная задача; SQL остаётся только в серверном реестре."""

    query_id: str
    version: int
    title: str
    sql: str


_NAMED_QUERIES = {
    "published_cases_by_bank": NamedAnalyticsQuery(
        query_id="published_cases_by_bank",
        version=1,
        title="Опубликованные кейсы по банкам",
        sql="SELECT bank_slug, status FROM loophole_published_catalog_v1",
    ),
}


def available_named_queries() -> list[dict[str, Any]]:
    """Публичная проекция реестра: идентификатор/версия, но никогда raw SQL."""
    return [
        {"query_id": query.query_id, "version": query.version, "title": query.title}
        for query in _NAMED_QUERIES.values()
    ]


def _as_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=MSK)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MSK)


class ScheduledAnalyticsService:
    """Хранит только контракт именованной задачи и выполняет его по cron."""

    def __init__(self, session):
        self.session = session

    def enable(
        self,
        *,
        query_id: str,
        workspace_id: int,
        owner_username: str,
        recipient_username: str,
        cron_expr: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Создаёт ScheduledQueryContract v1, не принимая и не сохраняя SQL."""
        query = _NAMED_QUERIES.get(query_id)
        if query is None:
            raise ValueError("Недоступная именованная аналитическая задача")
        now = _as_datetime(now or datetime.now(MSK))
        expires_at = _as_datetime(expires_at)
        if expires_at <= now:
            raise ValueError("Срок действия расписания должен быть в будущем")
        planned_at = next_run(cron_expr, now)
        row = self.session.execute(
            text(
                "INSERT INTO loophole_scheduled_query "
                "(query_id, query_version, workspace_id, owner_username, recipient_username, "
                "cron_expr, expires_at, enabled, next_run_at) "
                "VALUES (:qid, :ver, :workspace, :owner, :recipient, :cron, :expires, TRUE, :next) "
                "RETURNING scheduled_query_id"
            ),
            {
                "qid": query.query_id,
                "ver": query.version,
                "workspace": workspace_id,
                "owner": owner_username,
                "recipient": recipient_username,
                "cron": cron_expr,
                "expires": expires_at.isoformat(),
                "next": planned_at.isoformat(),
            },
        ).scalar_one()
        return {
            "contract_version": "ScheduledQueryContract v1",
            "scheduled_query_id": row,
            "query_id": query.query_id,
            "query_version": query.version,
            "workspace_id": workspace_id,
            "owner_username": owner_username,
            "recipient_username": recipient_username,
            "expires_at": expires_at,
            "next_run_at": planned_at,
        }

    def run_due(self, *, now: datetime | str | None = None) -> list[int]:
        """Выполняет due-контракты после повторной server-side проверки ACL."""
        effective_now = _as_datetime(now or datetime.now(MSK))
        rows = self.session.execute(
            text(
                "SELECT * FROM loophole_scheduled_query "
                "WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= :now"
            ),
            {"now": effective_now.isoformat()},
        ).mappings().all()
        completed: list[int] = []
        for raw in rows:
            contract = dict(raw)
            reason = self._skip_reason(contract, effective_now)
            if reason:
                self._record_skip(contract, reason, effective_now)
                continue
            query = _NAMED_QUERIES.get(contract["query_id"])
            if query is None or query.version != contract["query_version"]:
                self._record_skip(contract, "query_capability", effective_now)
                continue
            result = execute_analytics_query(query.sql, {}, session=self.session)
            result_expires_at = min(
                effective_now + timedelta(hours=24), _as_datetime(contract["expires_at"])
            )
            self.session.execute(
                text(
                    "INSERT INTO loophole_scheduled_result "
                    "(scheduled_query_id, workspace_id, owner_username, recipient_username, "
                    "result_json, expires_at) "
                    "VALUES (:schedule, :workspace, :owner, :recipient, :result, :expires)"
                ),
                {
                    "schedule": contract["scheduled_query_id"],
                    "workspace": contract["workspace_id"],
                    "owner": contract["owner_username"],
                    "recipient": contract["recipient_username"],
                    "result": json.dumps(result, ensure_ascii=False),
                    "expires": result_expires_at.isoformat(),
                },
            )
            self._advance(contract, effective_now)
            completed.append(contract["scheduled_query_id"])
        return completed

    def list_results(self, scheduled_query_id: int, *, username: str) -> list[dict[str, Any]]:
        """Возвращает только неистёкшие результаты явному ACL контракта."""
        contract = self.session.execute(
            text("SELECT * FROM loophole_scheduled_query WHERE scheduled_query_id = :id"),
            {"id": scheduled_query_id},
        ).mappings().first()
        if contract is None:
            return []
        contract = dict(contract)
        if username not in {contract["owner_username"], contract["recipient_username"]}:
            raise PermissionError("Нет доступа к результату расписания")
        if not authorization.is_active_member(username, session=self.session):
            raise PermissionError("Членство получателя результата неактивно")
        rows = self.session.execute(
            text(
                "SELECT scheduled_result_id, workspace_id, owner_username, recipient_username, "
                "result_json, expires_at, created_at FROM loophole_scheduled_result "
                "WHERE scheduled_query_id = :id AND expires_at > :now ORDER BY scheduled_result_id DESC"
            ),
            {"id": scheduled_query_id, "now": datetime.now(MSK).isoformat()},
        ).mappings().all()
        return [
            {
                **dict(row),
                "result": json.loads(row["result_json"]),
            }
            for row in rows
        ]

    def _skip_reason(self, contract: dict[str, Any], now: datetime) -> str | None:
        if _as_datetime(contract["expires_at"]) <= now:
            return "expired"
        workspace = repo.get_workspace(contract["workspace_id"], session=self.session)
        if workspace is None or workspace["user_id"] != contract["owner_username"]:
            return "owner_capability"
        if not authorization.is_active_member(contract["owner_username"], session=self.session):
            return "owner_membership"
        if not authorization.is_active_member(contract["recipient_username"], session=self.session):
            return "recipient_membership"
        return None

    def _record_skip(self, contract: dict[str, Any], reason: str, now: datetime) -> None:
        """Один audit event на due-запуск, затем переносит/гасит контракт."""
        repo.log_action(
            contract["owner_username"],
            f"schedule_skipped_{reason}",
            workspace_id=contract["workspace_id"],
            detail={"scheduled_query_id": contract["scheduled_query_id"]},
            session=self.session,
        )
        if reason == "expired":
            self.session.execute(
                text("UPDATE loophole_scheduled_query SET enabled = FALSE WHERE scheduled_query_id = :id"),
                {"id": contract["scheduled_query_id"]},
            )
            return
        self._advance(contract, now)

    def _advance(self, contract: dict[str, Any], now: datetime) -> None:
        try:
            planned_at = next_run(contract["cron_expr"], now)
        except ValueError:
            planned_at = None
        self.session.execute(
            text(
                "UPDATE loophole_scheduled_query SET next_run_at = :next, "
                "enabled = CASE WHEN :next IS NULL THEN FALSE ELSE enabled END "
                "WHERE scheduled_query_id = :id"
            ),
            {"id": contract["scheduled_query_id"], "next": planned_at.isoformat() if planned_at else None},
        )
