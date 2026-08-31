"""Независимое и безопасное сохранение Telegram ingress-объектов (Story 6.3).

Модуль намеренно не импортирует research, verification, каталог, LLM или audit:
worker сохраняет только историю ingress и устойчивый checkpoint canonical-цели.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from .pii_mask import mask
from .target_access import TargetAccessService

_ALLOWED_KINDS = frozenset({"post", "message", "comment"})
_SAFE_METADATA_KEYS = frozenset(
    {"author_id", "published_at", "thread_id", "message_id", "reply_to_id", "source_peer_id"}
)
_MAX_TEXT_LENGTH = 100_000
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class TelegramIngressError(RuntimeError):
    """Невозможно безопасно сохранить объект worker-а."""


@dataclass(frozen=True, slots=True)
class TelegramIngressItem:
    """Вход worker-а до санитарной обработки и без сетевой ответственности."""

    identity: str
    version: str
    object_kind: str
    sequence: int
    text: object | None
    metadata: Mapping[str, object]
    attachments: object | None = None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Детерминированная сводка одного завершённого прохода worker-а."""

    sync_mode: str
    checkpoint_before: dict[str, int] | None
    checkpoint_after: dict[str, int] | None
    accepted_count: int
    quarantined_count: int
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class _SanitizedItem:
    identity: str
    version: str
    object_kind: str
    sequence: int
    sanitized_text: str | None
    metadata: dict[str, object]
    quarantine_reason: str | None


class TelegramIngestionService:
    """Сохраняет только безопасный ingress canonical active Telegram-цели.

    Долговечный checkpoint обновляется после записи всего переданного набора.
    Поздний комментарий с меньшим sequence не отбрасывается: id/version-дедуп
    даёт ему отдельную историю, а checkpoint никогда не регрессирует.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def ingest(self, *, target_id: int, items: Iterable[TelegramIngressItem]) -> IngestionResult:
        """Санитизирует и атомарно сохраняет один полученный набор worker-а."""
        TargetAccessService(self._session).start_collection(target_id=target_id)
        checkpoint_before = self._checkpoint(target_id)
        sync_mode = "incremental" if checkpoint_before is not None else "initial"
        materialized = list(items)
        sanitized = [self._sanitize(item) for item in materialized]
        run_id = self._create_run(target_id, sync_mode, checkpoint_before)

        accepted_count = 0
        quarantined_count = 0
        duplicate_count = 0
        max_sequence = checkpoint_before["sequence"] if checkpoint_before else None
        for item in sanitized:
            max_sequence = max(item.sequence, max_sequence) if max_sequence is not None else item.sequence
            if self._already_saved(target_id, item.identity, item.version):
                duplicate_count += 1
                continue
            if item.quarantine_reason:
                self._save_quarantine(target_id, run_id, item)
                quarantined_count += 1
            else:
                self._save_ingress(target_id, run_id, item)
                accepted_count += 1

        checkpoint_after = {"sequence": max_sequence} if max_sequence is not None else checkpoint_before
        self._finish_run(target_id, run_id, checkpoint_after, accepted_count, quarantined_count, duplicate_count)
        return IngestionResult(
            sync_mode=sync_mode,
            checkpoint_before=checkpoint_before,
            checkpoint_after=checkpoint_after,
            accepted_count=accepted_count,
            quarantined_count=quarantined_count,
            duplicate_count=duplicate_count,
        )

    def _checkpoint(self, target_id: int) -> dict[str, int] | None:
        value = self._session.execute(
            text("SELECT checkpoint_json FROM loophole_telegram_target WHERE target_id = :target_id"),
            {"target_id": target_id},
        ).scalar_one()
        if value is None:
            return None
        try:
            loaded = json.loads(str(value))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TelegramIngressError("Некорректный устойчивый checkpoint Telegram-цели") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("sequence"), int):
            raise TelegramIngressError("Некорректный устойчивый checkpoint Telegram-цели")
        return {"sequence": loaded["sequence"]}

    def _create_run(
        self, target_id: int, sync_mode: str, checkpoint_before: dict[str, int] | None
    ) -> int:
        row = self._session.execute(
            text(
                "INSERT INTO loophole_telegram_ingestion_run "
                "(target_id, sync_mode, checkpoint_before_json, checkpoint_after_json, "
                "accepted_count, quarantined_count, duplicate_count) "
                "VALUES (:target_id, :sync_mode, :before, NULL, 0, 0, 0) "
                "RETURNING ingestion_run_id"
            ),
            {
                "target_id": target_id,
                "sync_mode": sync_mode,
                "before": _json(checkpoint_before) if checkpoint_before else None,
            },
        ).scalar_one()
        return int(row)

    def _finish_run(
        self,
        target_id: int,
        run_id: int,
        checkpoint_after: dict[str, int] | None,
        accepted_count: int,
        quarantined_count: int,
        duplicate_count: int,
    ) -> None:
        checkpoint_json = _json(checkpoint_after) if checkpoint_after else None
        self._session.execute(
            text(
                "UPDATE loophole_telegram_ingestion_run "
                "SET checkpoint_after_json = :after, accepted_count = :accepted, "
                "quarantined_count = :quarantined, duplicate_count = :duplicates "
                "WHERE ingestion_run_id = :run_id"
            ),
            {
                "run_id": run_id,
                "after": checkpoint_json,
                "accepted": accepted_count,
                "quarantined": quarantined_count,
                "duplicates": duplicate_count,
            },
        )
        self._session.execute(
            text(
                "UPDATE loophole_telegram_target SET checkpoint_json = :checkpoint "
                "WHERE target_id = :target_id"
            ),
            {"target_id": target_id, "checkpoint": checkpoint_json},
        )

    def _already_saved(self, target_id: int, identity: str, version: str) -> bool:
        return (
            self._session.execute(
                text(
                    "SELECT 1 FROM loophole_telegram_ingress "
                    "WHERE target_id = :target_id AND source_identity = :identity "
                    "AND source_version = :version "
                    "UNION ALL "
                    "SELECT 1 FROM loophole_telegram_ingress_quarantine "
                    "WHERE target_id = :target_id AND source_identity = :identity "
                    "AND source_version = :version LIMIT 1"
                ),
                {"target_id": target_id, "identity": identity, "version": version},
            ).first()
            is not None
        )

    def _save_ingress(self, target_id: int, run_id: int, item: _SanitizedItem) -> None:
        self._session.execute(
            text(
                "INSERT INTO loophole_telegram_ingress "
                "(target_id, source_identity, source_version, object_kind, sequence_no, "
                "sanitized_text, metadata_json, ingestion_run_id) "
                "VALUES (:target_id, :identity, :version, :kind, :sequence, :body, :metadata, :run_id)"
            ),
            {
                "target_id": target_id,
                "identity": item.identity,
                "version": item.version,
                "kind": item.object_kind,
                "sequence": item.sequence,
                "body": item.sanitized_text,
                "metadata": _json(item.metadata),
                "run_id": run_id,
            },
        )

    def _save_quarantine(self, target_id: int, run_id: int, item: _SanitizedItem) -> None:
        self._session.execute(
            text(
                "INSERT INTO loophole_telegram_ingress_quarantine "
                "(target_id, source_identity, source_version, object_kind, sequence_no, "
                "metadata_json, reason_code, ingestion_run_id) "
                "VALUES (:target_id, :identity, :version, :kind, :sequence, :metadata, :reason, :run_id)"
            ),
            {
                "target_id": target_id,
                "identity": item.identity,
                "version": item.version,
                "kind": item.object_kind,
                "sequence": item.sequence,
                "metadata": _json(item.metadata),
                "reason": item.quarantine_reason,
                "run_id": run_id,
            },
        )

    @staticmethod
    def _sanitize(item: TelegramIngressItem) -> _SanitizedItem:
        if not isinstance(item, TelegramIngressItem):
            raise TelegramIngressError("Worker передал объект неизвестного контракта")
        _validate_identity(item.identity, "identity")
        _validate_identity(item.version, "version")
        if item.object_kind not in _ALLOWED_KINDS:
            raise TelegramIngressError("Недопустимый вид Telegram ingress-объекта")
        if not isinstance(item.sequence, int) or isinstance(item.sequence, bool) or item.sequence < 0:
            raise TelegramIngressError("Недопустимый sequence Telegram ingress-объекта")

        metadata, metadata_is_safe = _safe_metadata(item.metadata)
        if item.attachments:
            return _SanitizedItem(
                item.identity,
                item.version,
                item.object_kind,
                item.sequence,
                None,
                metadata,
                "attachments_not_approved",
            )
        if not metadata_is_safe:
            return _SanitizedItem(
                item.identity,
                item.version,
                item.object_kind,
                item.sequence,
                None,
                metadata,
                "metadata_not_approved",
            )
        if item.text is not None and not isinstance(item.text, str):
            return _SanitizedItem(
                item.identity,
                item.version,
                item.object_kind,
                item.sequence,
                None,
                metadata,
                "text_not_approved",
            )
        if isinstance(item.text, str) and (len(item.text) > _MAX_TEXT_LENGTH or "\x00" in item.text):
            return _SanitizedItem(
                item.identity,
                item.version,
                item.object_kind,
                item.sequence,
                None,
                metadata,
                "text_not_approved",
            )
        sanitized_text = mask(item.text)[0] if isinstance(item.text, str) else None
        return _SanitizedItem(
            item.identity,
            item.version,
            item.object_kind,
            item.sequence,
            sanitized_text,
            metadata,
            None,
        )


def _validate_identity(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise TelegramIngressError(f"Недопустимое поле {field} Telegram ingress-объекта")


def _safe_metadata(value: object) -> tuple[dict[str, object], bool]:
    """Пропускает узкий allowlist, никогда не сериализуя неизвестные значения."""
    if not isinstance(value, Mapping):
        return {}, False
    result: dict[str, object] = {}
    safe = True
    for key, item in value.items():
        if key not in _SAFE_METADATA_KEYS:
            safe = False
            continue
        if _is_safe_metadata_value(key, item):
            result[key] = item
            continue
        safe = False
    return result, safe


def _is_safe_metadata_value(key: str, value: object) -> bool:
    """Одобряет только типовые ID и ISO-время, но не произвольный текст."""
    if key == "published_at":
        if not isinstance(value, str) or len(value) > 64:
            return False
        try:
            datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
        except ValueError:
            return False
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value >= 0
    return isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) is not None


def _json(value: Mapping[str, object] | None) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
