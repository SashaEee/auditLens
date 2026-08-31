"""SQLite test adapter устойчивого Telegram-ingestion worker-а (Story 6.4).

Этот adapter не открывает Telegram-сессию и не знает о содержимом источника. Он
выдаёт global/target lease, fence-ит каждую durable-запись и сохраняет только
безопасную операционную сводку. Получение объектов остаётся ответственностью
узкого adapter-а, а санитарная обработка — Story 6.3.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from .telegram_ingestion import TelegramIngestionService, TelegramIngressItem, _json

_GLOBAL_LEASE_NAME = "telegram-worker"


class TelegramWorkerError(RuntimeError):
    """Базовая ошибка durable-контура Telegram worker-а."""


class StaleTelegramWorkerError(TelegramWorkerError):
    """Lease или fencing token устарел до попытки durable-записи."""


@dataclass(frozen=True, slots=True)
class GlobalWorkerLease:
    """Эксклюзивное владение одним логическим worker-сеансом."""

    owner_id: str
    fence_token: int


@dataclass(frozen=True, slots=True)
class TelegramWorkerLease:
    """Владение одной целью, привязанное к global и lifecycle fence."""

    target_id: int
    owner_id: str
    global_fence_token: int
    target_fence_token: int
    lifecycle_fence_token: int


@dataclass(frozen=True, slots=True)
class TelegramWorkerAttempt:
    """Одна durable-попытка обхода с checkpoint на старте."""

    attempt_id: int
    target_id: int
    sync_mode: str
    checkpoint_before: dict[str, int] | None
    started_monotonic: float


@dataclass(frozen=True, slots=True)
class TelegramWorkerBatchResult:
    """Счётчики безопасно сохранённого batch-а."""

    checkpoint_before: dict[str, int] | None
    checkpoint_after: dict[str, int] | None
    accepted_count: int
    quarantined_count: int
    duplicate_count: int


class TelegramWorkerService:
    """Координирует durable Telegram worker без сетевого транспорта.

    Все ingress/checkpoint mutation используют один и тот же SQL guard. Это
    важно не только для локальной блокировки: устаревший процесс может жить
    после истечения lease, поэтому его token перепроверяется БД в момент write.
    """

    def __init__(self, session: Session, *, owner_id: str, lease_seconds: int = 60) -> None:
        if not owner_id or len(owner_id) > 128:
            raise ValueError("owner_id Telegram worker-а обязателен и ограничен 128 символами")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds должен быть положительным")
        self._session = session
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds

    def acquire_global_lease(self) -> GlobalWorkerLease | None:
        """Атомарно получает singleton lease, повышая fencing token."""
        lease_until = _lease_until(self._lease_seconds)
        result = self._session.execute(
            text(
                "UPDATE loophole_telegram_worker_global_lease "
                "SET owner_id = :owner_id, fence_token = fence_token + 1, lease_until = :lease_until "
                "WHERE lease_name = :lease_name "
                "AND (lease_until <= CURRENT_TIMESTAMP OR owner_id = :owner_id)"
            ),
            {
                "owner_id": self._owner_id,
                "lease_until": lease_until,
                "lease_name": _GLOBAL_LEASE_NAME,
            },
        )
        if result.rowcount != 1:
            return None
        row = self._session.execute(
            text(
                "SELECT fence_token FROM loophole_telegram_worker_global_lease "
                "WHERE lease_name = :lease_name AND owner_id = :owner_id"
            ),
            {"lease_name": _GLOBAL_LEASE_NAME, "owner_id": self._owner_id},
        ).mappings().one()
        return GlobalWorkerLease(owner_id=self._owner_id, fence_token=int(row["fence_token"]))

    def acquire_target_lease(
        self, *, target_id: int, global_lease: GlobalWorkerLease
    ) -> TelegramWorkerLease | None:
        """Получает target lease только пока exact global lease актуален."""
        self._assert_global_lease(global_lease)
        self._session.execute(
            text(
                "INSERT INTO loophole_telegram_worker_target_lease "
                "(target_id, owner_id, global_fence_token, target_fence_token, "
                "lifecycle_fence_token, lease_until) "
                "SELECT target_id, NULL, 0, 0, 0, CURRENT_TIMESTAMP "
                "FROM loophole_telegram_target "
                "WHERE target_id = :target_id AND canonical_target_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM loophole_telegram_worker_target_lease "
                "WHERE target_id = :target_id)"
            ),
            {"target_id": target_id},
        )
        result = self._session.execute(
            text(
                "UPDATE loophole_telegram_worker_target_lease "
                "SET owner_id = :owner_id, global_fence_token = :global_fence_token, "
                "target_fence_token = target_fence_token + 1, "
                "lifecycle_fence_token = (SELECT fence_token FROM loophole_telegram_target "
                "WHERE target_id = :target_id), lease_until = :lease_until "
                "WHERE target_id = :target_id "
                "AND (lease_until <= CURRENT_TIMESTAMP OR owner_id = :owner_id) "
                "AND EXISTS (SELECT 1 FROM loophole_telegram_worker_global_lease "
                "WHERE lease_name = :lease_name AND owner_id = :owner_id "
                "AND fence_token = :global_fence_token AND lease_until > CURRENT_TIMESTAMP) "
                "AND EXISTS (SELECT 1 FROM loophole_telegram_target "
                "WHERE target_id = :target_id AND canonical_target_id IS NULL "
                "AND lifecycle_status = 'active')"
            ),
            {
                "target_id": target_id,
                "owner_id": self._owner_id,
                "global_fence_token": global_lease.fence_token,
                "lease_until": _lease_until(self._lease_seconds),
                "lease_name": _GLOBAL_LEASE_NAME,
            },
        )
        if result.rowcount != 1:
            return None
        row = self._session.execute(
            text(
                "SELECT target_fence_token, lifecycle_fence_token "
                "FROM loophole_telegram_worker_target_lease "
                "WHERE target_id = :target_id AND owner_id = :owner_id "
                "AND global_fence_token = :global_fence_token"
            ),
            {
                "target_id": target_id,
                "owner_id": self._owner_id,
                "global_fence_token": global_lease.fence_token,
            },
        ).mappings().one()
        return TelegramWorkerLease(
            target_id=target_id,
            owner_id=self._owner_id,
            global_fence_token=global_lease.fence_token,
            target_fence_token=int(row["target_fence_token"]),
            lifecycle_fence_token=int(row["lifecycle_fence_token"]),
        )

    def start_attempt(self, lease: TelegramWorkerLease) -> TelegramWorkerAttempt:
        """Фиксирует attempt_started и durable checkpoint до обхода."""
        self._assert_current(lease)
        checkpoint_before = self._checkpoint(lease.target_id)
        sync_mode = "incremental" if checkpoint_before is not None else "initial"
        row = self._session.execute(
            text(
                "INSERT INTO loophole_telegram_worker_attempt "
                "(target_id, owner_id, global_fence_token, target_fence_token, lifecycle_fence_token, "
                "sync_mode, checkpoint_before_json, status, lease_until) "
                "SELECT :target_id, :owner_id, :global_fence_token, :target_fence_token, "
                ":lifecycle_fence_token, :sync_mode, :checkpoint_before, 'running', "
                "(SELECT lease_until FROM loophole_telegram_worker_target_lease "
                "WHERE target_id = :target_id) WHERE "
                + _current_exists_sql()
                + " RETURNING attempt_id"
            ),
            self._lease_params(lease, checkpoint_before=_json(checkpoint_before) if checkpoint_before else None,
                               sync_mode=sync_mode),
        ).scalar()
        if row is None:
            raise StaleTelegramWorkerError("Устаревший worker не может начать attempt")
        attempt = TelegramWorkerAttempt(
            attempt_id=int(row),
            target_id=lease.target_id,
            sync_mode=sync_mode,
            checkpoint_before=checkpoint_before,
            started_monotonic=time.monotonic(),
        )
        self._write_journal(
            attempt=attempt,
            event_type="attempt_started",
            checkpoint_before=checkpoint_before,
            checkpoint_after=None,
            accepted_count=0,
            quarantined_count=0,
            duplicate_count=0,
            duration_ms=0,
            error_code=None,
            lease=lease,
        )
        return attempt

    def ingest_batch(
        self,
        attempt: TelegramWorkerAttempt,
        lease: TelegramWorkerLease,
        items: Iterable[TelegramIngressItem],
    ) -> TelegramWorkerBatchResult:
        """Сохраняет batch только с актуальным fencing token и checkpoint."""
        self._assert_attempt_matches(attempt, lease)
        self._assert_current(lease)
        checkpoint_before = self._checkpoint(lease.target_id)
        sanitized = [TelegramIngestionService._sanitize(item) for item in items]
        run_id = self._create_ingestion_run(attempt, lease, checkpoint_before)
        accepted_count = quarantined_count = duplicate_count = 0
        max_sequence = checkpoint_before["sequence"] if checkpoint_before else None
        for item in sanitized:
            max_sequence = max(item.sequence, max_sequence) if max_sequence is not None else item.sequence
            if self._already_saved(lease.target_id, item.identity, item.version):
                duplicate_count += 1
                continue
            if item.quarantine_reason:
                self._guarded_insert(
                    "loophole_telegram_ingress_quarantine",
                    "(target_id, source_identity, source_version, object_kind, sequence_no, "
                    "metadata_json, reason_code, ingestion_run_id)",
                    ":target_id, :identity, :version, :kind, :sequence, :metadata, :reason, :run_id",
                    self._lease_params(
                        lease,
                        identity=item.identity,
                        version=item.version,
                        kind=item.object_kind,
                        sequence=item.sequence,
                        metadata=_json(item.metadata),
                        reason=item.quarantine_reason,
                        run_id=run_id,
                    ),
                )
                quarantined_count += 1
            else:
                self._guarded_insert(
                    "loophole_telegram_ingress",
                    "(target_id, source_identity, source_version, object_kind, sequence_no, "
                    "sanitized_text, metadata_json, ingestion_run_id)",
                    ":target_id, :identity, :version, :kind, :sequence, :body, :metadata, :run_id",
                    self._lease_params(
                        lease,
                        identity=item.identity,
                        version=item.version,
                        kind=item.object_kind,
                        sequence=item.sequence,
                        body=item.sanitized_text,
                        metadata=_json(item.metadata),
                        run_id=run_id,
                    ),
                )
                accepted_count += 1
        checkpoint_after = {"sequence": max_sequence} if max_sequence is not None else checkpoint_before
        params = self._lease_params(
            lease,
            attempt_id=attempt.attempt_id,
            checkpoint_after=_json(checkpoint_after) if checkpoint_after else None,
            accepted_count=accepted_count,
            quarantined_count=quarantined_count,
            duplicate_count=duplicate_count,
            run_id=run_id,
        )
        self._guarded_update(
            "loophole_telegram_ingestion_run",
            "checkpoint_after_json = :checkpoint_after, accepted_count = :accepted_count, "
            "quarantined_count = :quarantined_count, duplicate_count = :duplicate_count",
            "ingestion_run_id = :run_id",
            params,
        )
        self._guarded_update(
            "loophole_telegram_target",
            "checkpoint_json = :checkpoint_after",
            "target_id = :target_id",
            params,
        )
        self._guarded_update(
            "loophole_telegram_worker_attempt",
            "checkpoint_after_json = :checkpoint_after, accepted_count = accepted_count + :accepted_count, "
            "quarantined_count = quarantined_count + :quarantined_count, "
            "duplicate_count = duplicate_count + :duplicate_count",
            "attempt_id = :attempt_id AND status = 'running'",
            params,
        )
        self._write_journal(
            attempt=attempt,
            event_type="batch_finished",
            checkpoint_before=checkpoint_before,
            checkpoint_after=checkpoint_after,
            accepted_count=accepted_count,
            quarantined_count=quarantined_count,
            duplicate_count=duplicate_count,
            duration_ms=_duration_ms(attempt.started_monotonic),
            error_code=None,
            lease=lease,
        )
        return TelegramWorkerBatchResult(
            checkpoint_before=checkpoint_before,
            checkpoint_after=checkpoint_after,
            accepted_count=accepted_count,
            quarantined_count=quarantined_count,
            duplicate_count=duplicate_count,
        )

    def complete_attempt(self, attempt: TelegramWorkerAttempt, lease: TelegramWorkerLease) -> None:
        """Финализирует успешный обход, не создавая внешней доставки."""
        self._assert_attempt_matches(attempt, lease)
        self._assert_current(lease)
        row = self._session.execute(
            text(
                "SELECT checkpoint_after_json, accepted_count, quarantined_count, duplicate_count "
                "FROM loophole_telegram_worker_attempt WHERE attempt_id = :attempt_id "
                "AND status = 'running'"
            ),
            {"attempt_id": attempt.attempt_id},
        ).mappings().first()
        if row is None:
            raise StaleTelegramWorkerError("Attempt уже завершён или принадлежит устаревшему worker-у")
        params = self._lease_params(lease, attempt_id=attempt.attempt_id)
        self._guarded_update(
            "loophole_telegram_worker_attempt",
            "status = 'completed', finished_at = CURRENT_TIMESTAMP",
            "attempt_id = :attempt_id AND status = 'running'",
            params,
        )
        checkpoint_after = _load_checkpoint(row["checkpoint_after_json"])
        self._write_journal(
            attempt=attempt,
            event_type="attempt_finished",
            checkpoint_before=attempt.checkpoint_before,
            checkpoint_after=checkpoint_after,
            accepted_count=int(row["accepted_count"]),
            quarantined_count=int(row["quarantined_count"]),
            duplicate_count=int(row["duplicate_count"]),
            duration_ms=_duration_ms(attempt.started_monotonic),
            error_code=None,
            lease=lease,
        )

    def reap_expired_attempts(self) -> int:
        """Один раз terminalize-ит abandoned attempt и создаёт safe outbox summary."""
        rows = self._session.execute(
            text(
                "SELECT attempt_id, target_id, sync_mode, checkpoint_before_json, checkpoint_after_json, "
                "accepted_count, quarantined_count, duplicate_count "
                "FROM loophole_telegram_worker_attempt WHERE status = 'running' "
                "AND lease_until <= CURRENT_TIMESTAMP"
            )
        ).mappings().all()
        terminalized = 0
        for row in rows:
            changed = self._session.execute(
                text(
                    "UPDATE loophole_telegram_worker_attempt SET status = 'reaped', "
                    "finished_at = CURRENT_TIMESTAMP WHERE attempt_id = :attempt_id "
                    "AND status = 'running' AND lease_until <= CURRENT_TIMESTAMP"
                ),
                {"attempt_id": row["attempt_id"]},
            )
            if changed.rowcount != 1:
                continue
            payload = _summary_payload(row, reason="lease_expired")
            self._session.execute(
                text(
                    "INSERT INTO loophole_telegram_worker_outbox "
                    "(attempt_id, target_id, event_type, payload_json) "
                    "VALUES (:attempt_id, :target_id, 'attempt_reaped', :payload)"
                ),
                {"attempt_id": row["attempt_id"], "target_id": row["target_id"], "payload": payload},
            )
            self._write_reaper_journal(row)
            terminalized += 1
        return terminalized

    def slo_violations(self) -> list[int]:
        """Возвращает active-цели без start за скользящие 24 часа и без run."""
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).replace(tzinfo=None)
        rows = self._session.execute(
            text(
                "SELECT target_id FROM loophole_telegram_target t "
                "WHERE t.canonical_target_id IS NULL AND t.lifecycle_status = 'active' "
                "AND NOT EXISTS (SELECT 1 FROM loophole_telegram_worker_journal j "
                "WHERE j.target_id = t.target_id AND j.event_type = 'attempt_started' "
                "AND j.created_at >= :cutoff) "
                "AND NOT EXISTS (SELECT 1 FROM loophole_telegram_worker_attempt a "
                "WHERE a.target_id = t.target_id AND a.status = 'running')"
            ),
            {"cutoff": cutoff},
        ).scalars().all()
        return [int(target_id) for target_id in rows]

    def _assert_global_lease(self, global_lease: GlobalWorkerLease) -> None:
        if global_lease.owner_id != self._owner_id or not self._global_is_current(global_lease):
            raise StaleTelegramWorkerError("Global lease Telegram worker-а устарел")

    def _assert_current(self, lease: TelegramWorkerLease) -> None:
        if lease.owner_id != self._owner_id or not self._lease_is_current(lease):
            raise StaleTelegramWorkerError("Target lease или fencing token Telegram worker-а устарел")

    @staticmethod
    def _assert_attempt_matches(attempt: TelegramWorkerAttempt, lease: TelegramWorkerLease) -> None:
        if attempt.target_id != lease.target_id:
            raise StaleTelegramWorkerError("Attempt не соответствует Telegram target lease")

    def _global_is_current(self, lease: GlobalWorkerLease) -> bool:
        return (
            self._session.execute(
                text(
                    "SELECT 1 FROM loophole_telegram_worker_global_lease "
                    "WHERE lease_name = :lease_name AND owner_id = :owner_id "
                    "AND fence_token = :fence_token AND lease_until > CURRENT_TIMESTAMP"
                ),
                {
                    "lease_name": _GLOBAL_LEASE_NAME,
                    "owner_id": lease.owner_id,
                    "fence_token": lease.fence_token,
                },
            ).first()
            is not None
        )

    def _lease_is_current(self, lease: TelegramWorkerLease) -> bool:
        return self._session.execute(text("SELECT 1 WHERE " + _current_exists_sql()), self._lease_params(lease)).first() is not None

    def _checkpoint(self, target_id: int) -> dict[str, int] | None:
        value = self._session.execute(
            text("SELECT checkpoint_json FROM loophole_telegram_target WHERE target_id = :target_id"),
            {"target_id": target_id},
        ).scalar_one()
        return _load_checkpoint(value)

    def _create_ingestion_run(
        self,
        attempt: TelegramWorkerAttempt,
        lease: TelegramWorkerLease,
        checkpoint_before: dict[str, int] | None,
    ) -> int:
        row = self._session.execute(
            text(
                "INSERT INTO loophole_telegram_ingestion_run "
                "(target_id, sync_mode, checkpoint_before_json, checkpoint_after_json, "
                "accepted_count, quarantined_count, duplicate_count) "
                "SELECT :target_id, :sync_mode, :checkpoint_before, NULL, 0, 0, 0 WHERE "
                + _current_exists_sql()
                + " RETURNING ingestion_run_id"
            ),
            self._lease_params(
                lease,
                sync_mode=attempt.sync_mode,
                checkpoint_before=_json(checkpoint_before) if checkpoint_before else None,
            ),
        ).scalar()
        if row is None:
            raise StaleTelegramWorkerError("Устаревший worker не может создать ingress batch")
        return int(row)

    def _guarded_insert(self, table: str, columns: str, values: str, params: dict[str, object]) -> None:
        result = self._session.execute(
            text(f"INSERT INTO {table} {columns} SELECT {values} WHERE " + _current_exists_sql()),
            params,
        )
        if result.rowcount != 1:
            raise StaleTelegramWorkerError("Устаревший worker не может записать Telegram ingress")

    def _guarded_update(self, table: str, assignment: str, condition: str, params: dict[str, object]) -> None:
        result = self._session.execute(
            text(f"UPDATE {table} SET {assignment} WHERE {condition} AND " + _current_exists_sql()),
            params,
        )
        if result.rowcount != 1:
            raise StaleTelegramWorkerError("Устаревший worker не может изменить checkpoint или attempt")

    def _already_saved(self, target_id: int, identity: str, version: str) -> bool:
        return (
            self._session.execute(
                text(
                    "SELECT 1 FROM loophole_telegram_ingress "
                    "WHERE target_id = :target_id AND source_identity = :identity "
                    "AND source_version = :version UNION ALL "
                    "SELECT 1 FROM loophole_telegram_ingress_quarantine "
                    "WHERE target_id = :target_id AND source_identity = :identity "
                    "AND source_version = :version LIMIT 1"
                ),
                {"target_id": target_id, "identity": identity, "version": version},
            ).first()
            is not None
        )

    def _write_journal(
        self,
        *,
        attempt: TelegramWorkerAttempt,
        event_type: str,
        checkpoint_before: dict[str, int] | None,
        checkpoint_after: dict[str, int] | None,
        accepted_count: int,
        quarantined_count: int,
        duplicate_count: int,
        duration_ms: int,
        error_code: str | None,
        lease: TelegramWorkerLease,
    ) -> None:
        result = self._session.execute(
            text(
                "INSERT INTO loophole_telegram_worker_journal "
                "(attempt_id, target_id, event_type, sync_mode, checkpoint_before_json, "
                "checkpoint_after_json, accepted_count, quarantined_count, duplicate_count, "
                "duration_ms, error_code) "
                "SELECT :attempt_id, :target_id, :event_type, :sync_mode, :checkpoint_before, "
                ":checkpoint_after, :accepted_count, :quarantined_count, :duplicate_count, "
                ":duration_ms, :error_code WHERE "
                + _current_exists_sql()
            ),
            self._lease_params(
                lease,
                attempt_id=attempt.attempt_id,
                event_type=event_type,
                sync_mode=attempt.sync_mode,
                checkpoint_before=_json(checkpoint_before) if checkpoint_before else None,
                checkpoint_after=_json(checkpoint_after) if checkpoint_after else None,
                accepted_count=accepted_count,
                quarantined_count=quarantined_count,
                duplicate_count=duplicate_count,
                duration_ms=duration_ms,
                error_code=error_code,
            ),
        )
        if result.rowcount != 1:
            raise StaleTelegramWorkerError("Устаревший worker не может записать журнал attempt")

    def _write_reaper_journal(self, row) -> None:
        self._session.execute(
            text(
                "INSERT INTO loophole_telegram_worker_journal "
                "(attempt_id, target_id, event_type, sync_mode, checkpoint_before_json, "
                "checkpoint_after_json, accepted_count, quarantined_count, duplicate_count, "
                "duration_ms, error_code) VALUES "
                "(:attempt_id, :target_id, 'attempt_reaped', :sync_mode, :checkpoint_before, "
                ":checkpoint_after, :accepted_count, :quarantined_count, :duplicate_count, 0, "
                "'lease_expired')"
            ),
            {
                "attempt_id": row["attempt_id"],
                "target_id": row["target_id"],
                "sync_mode": row["sync_mode"],
                "checkpoint_before": row["checkpoint_before_json"],
                "checkpoint_after": row["checkpoint_after_json"],
                "accepted_count": row["accepted_count"],
                "quarantined_count": row["quarantined_count"],
                "duplicate_count": row["duplicate_count"],
            },
        )

    def _lease_params(self, lease: TelegramWorkerLease, **extra: object) -> dict[str, object]:
        return {
            "target_id": lease.target_id,
            "owner_id": lease.owner_id,
            "global_fence_token": lease.global_fence_token,
            "target_fence_token": lease.target_fence_token,
            "lifecycle_fence_token": lease.lifecycle_fence_token,
            "lease_name": _GLOBAL_LEASE_NAME,
            **extra,
        }


def _current_exists_sql() -> str:
    """SQL-предикат exact global/target/lifecycle fencing без raw payload."""
    return (
        "EXISTS (SELECT 1 FROM loophole_telegram_worker_global_lease global_lease "
        "JOIN loophole_telegram_worker_target_lease target_lease "
        "ON target_lease.target_id = :target_id "
        "JOIN loophole_telegram_target target ON target.target_id = :target_id "
        "WHERE global_lease.lease_name = :lease_name "
        "AND global_lease.owner_id = :owner_id "
        "AND global_lease.fence_token = :global_fence_token "
        "AND global_lease.lease_until > CURRENT_TIMESTAMP "
        "AND target_lease.owner_id = :owner_id "
        "AND target_lease.global_fence_token = :global_fence_token "
        "AND target_lease.target_fence_token = :target_fence_token "
        "AND target_lease.lifecycle_fence_token = :lifecycle_fence_token "
        "AND target_lease.lease_until > CURRENT_TIMESTAMP "
        "AND target.canonical_target_id IS NULL AND target.lifecycle_status = 'active' "
        "AND target.fence_token = :lifecycle_fence_token)"
    )


def _lease_until(seconds: int) -> datetime:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(tzinfo=None)


def _load_checkpoint(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise TelegramWorkerError("Некорректный durable checkpoint Telegram worker-а") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("sequence"), int):
        raise TelegramWorkerError("Некорректный durable checkpoint Telegram worker-а")
    return {"sequence": loaded["sequence"]}


def _duration_ms(started_monotonic: float) -> int:
    return max(0, int((time.monotonic() - started_monotonic) * 1000))


def _summary_payload(row, *, reason: str) -> str:
    """Outbox содержит только счётчики и checkpoint, но не адрес/текст ingress."""
    return json.dumps(
        {
            "accepted_count": int(row["accepted_count"]),
            "checkpoint_after": _load_checkpoint(row["checkpoint_after_json"]),
            "duplicate_count": int(row["duplicate_count"]),
            "quarantined_count": int(row["quarantined_count"]),
            "reason": reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
