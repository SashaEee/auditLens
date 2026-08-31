"""Production-adapter Telegram worker-а без прямого DML.

PostgreSQL runtime вызывает только узкие SECURITY DEFINER functions migration
057. SQLite допускается лишь в unit-тестах через отдельный test adapter.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .telegram_ingestion import TelegramIngestionService, TelegramIngressItem
from .telegram_worker_sqlite import (
    GlobalWorkerLease,
    StaleTelegramWorkerError,
    TelegramWorkerAttempt,
    TelegramWorkerBatchResult,
    TelegramWorkerError,
    TelegramWorkerLease,
)
from .telegram_worker_sqlite import (
    TelegramWorkerService as _SqliteTelegramWorkerService,
)


class TelegramWorkerService:
    """Выбирает production PostgreSQL-adapter либо изолированный SQLite test adapter."""

    def __new__(
        cls, session: Session, *, owner_id: str, lease_seconds: int = 60
    ) -> _PostgresTelegramWorkerService | _SqliteTelegramWorkerService:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            return _SqliteTelegramWorkerService(
                session, owner_id=owner_id, lease_seconds=lease_seconds
            )
        return _PostgresTelegramWorkerService(session, owner_id=owner_id, lease_seconds=lease_seconds)


class _PostgresTelegramWorkerService:
    """Узкий клиент controlled DB functions для runtime principal telegram_worker."""

    def __init__(self, session: Session, *, owner_id: str, lease_seconds: int = 60) -> None:
        if not owner_id or len(owner_id) > 128:
            raise ValueError("owner_id Telegram worker-а обязателен и ограничен 128 символами")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds должен быть положительным")
        self._session = session
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds

    def acquire_global_lease(self) -> GlobalWorkerLease | None:
        row = self._call("loophole_worker_acquire_global_lease", owner_id=self._owner_id,
                         lease_seconds=self._lease_seconds)
        if row is None:
            return None
        return GlobalWorkerLease(owner_id=self._owner_id, fence_token=int(row["fence_token"]))

    def acquire_target_lease(
        self, *, target_id: int, global_lease: GlobalWorkerLease
    ) -> TelegramWorkerLease | None:
        row = self._call("loophole_worker_acquire_target_lease", target_id=target_id,
                         owner_id=self._owner_id, global_fence_token=global_lease.fence_token,
                         lease_seconds=self._lease_seconds)
        if row is None:
            return None
        return TelegramWorkerLease(
            target_id=target_id, owner_id=self._owner_id,
            global_fence_token=global_lease.fence_token,
            target_fence_token=int(row["target_fence_token"]),
            lifecycle_fence_token=int(row["lifecycle_fence_token"]),
        )

    def start_attempt(self, lease: TelegramWorkerLease) -> TelegramWorkerAttempt:
        row = self._require("loophole_worker_start_attempt", **self._lease_params(lease))
        return TelegramWorkerAttempt(
            attempt_id=int(row["attempt_id"]), target_id=lease.target_id,
            sync_mode=str(row["sync_mode"]), checkpoint_before=_checkpoint(row.get("checkpoint_before")),
            started_monotonic=0.0,
        )

    def ingest_batch(
        self, attempt: TelegramWorkerAttempt, lease: TelegramWorkerLease,
        items: Iterable[TelegramIngressItem],
    ) -> TelegramWorkerBatchResult:
        sanitized = [TelegramIngestionService._sanitize(item) for item in items]
        payload = [
            {
                "identity": item.identity, "version": item.version, "object_kind": item.object_kind,
                "sequence": item.sequence, "sanitized_text": item.sanitized_text,
                "metadata": item.metadata, "quarantine_reason": item.quarantine_reason,
            }
            for item in sanitized
        ]
        row = self._require("loophole_worker_ingest_batch", **self._lease_params(
            lease, attempt_id=attempt.attempt_id, items=json.dumps(payload, ensure_ascii=False)
        ))
        return TelegramWorkerBatchResult(
            checkpoint_before=_checkpoint(row.get("checkpoint_before")),
            checkpoint_after=_checkpoint(row.get("checkpoint_after")),
            accepted_count=int(row["accepted_count"]), quarantined_count=int(row["quarantined_count"]),
            duplicate_count=int(row["duplicate_count"]),
        )

    def complete_attempt(self, attempt: TelegramWorkerAttempt, lease: TelegramWorkerLease) -> None:
        self._require("loophole_worker_complete_attempt", **self._lease_params(
            lease, attempt_id=attempt.attempt_id
        ))

    def reap_expired_attempts(self) -> int:
        row = self._session.execute(
            text("SELECT loophole_terminalize_expired_attempt(:limit) AS result"), {"limit": 100}
        ).mappings().one()
        return int(row["result"])

    def slo_violations(self) -> list[int]:
        rows = self._session.execute(text("SELECT target_id FROM loophole_telegram_worker_slo_v1")).scalars()
        return [int(target_id) for target_id in rows]

    def _call(self, function: str, **params: object) -> dict[str, Any] | None:
        bound = ", ".join(
            "CAST(:items AS JSONB)" if name == "items" else f":{name}" for name in params
        )
        row = self._session.execute(
            text(f"SELECT * FROM {function}({bound})"), params
        ).mappings().first()
        return dict(row) if row else None

    def _require(self, function: str, **params: object) -> dict[str, Any]:
        row = self._call(function, **params)
        if row is None:
            raise StaleTelegramWorkerError("Fenced Telegram worker function отклонила mutation")
        return row

    @staticmethod
    def _lease_params(lease: TelegramWorkerLease, **extra: object) -> dict[str, object]:
        return {
            "target_id": lease.target_id, "owner_id": lease.owner_id,
            "global_fence_token": lease.global_fence_token,
            "target_fence_token": lease.target_fence_token,
            "lifecycle_fence_token": lease.lifecycle_fence_token, **extra,
        }


def _checkpoint(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    loaded = json.loads(str(value)) if isinstance(value, str) else value
    if not isinstance(loaded, dict) or not isinstance(loaded.get("sequence"), int):
        raise TelegramWorkerError("Некорректный durable checkpoint Telegram worker-а")
    return {"sequence": loaded["sequence"]}
