"""Доступ workspace и lifecycle канонической Telegram-цели (Story 6.2).

Сервис не создаёт Telegram-цели и не запускает worker. Он хранит только
явное намерение администратора: подписку workspace и fencing-сигнал при
деактивации. Последующие ingestion-истории используют lease и terminal signal
как узкий контракт остановки worker.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

_CAPABILITY_SUBSCRIPTION = "target_subscription_manage"
_CAPABILITY_LIFECYCLE = "target_lifecycle_manage"


class TargetAccessError(RuntimeError):
    """Базовая ошибка изолированного контура Telegram-целей."""


class TargetAccessDenied(TargetAccessError):
    """Операция не разрешена server-side политикой доступа."""


class TargetRedirectError(TargetAccessError):
    """Redirect ID нельзя использовать как управляющую Telegram-цель."""


class TargetNotFoundError(TargetAccessError):
    """Запрошенная Telegram-цель не зарегистрирована."""


class TargetInactiveError(TargetAccessError):
    """Новая попытка worker запрещена для деактивированной цели."""


class TargetSubscriptionNotFound(TargetAccessError):
    """Нельзя отозвать несуществующую subscription."""


@dataclass(frozen=True, slots=True)
class WorkspaceSubscription:
    """Результат явного grant или revoke для одного workspace."""

    target_id: int
    workspace_id: int
    status: str
    grant_version: int


@dataclass(frozen=True, slots=True)
class TargetCollectionLease:
    """Пара generation/fence, с которой worker может начать обход."""

    target_id: int
    generation: int
    fence_token: int


@dataclass(frozen=True, slots=True)
class FencedTerminalSignal:
    """Durable-сигнал для старого worker после deactivate."""

    target_id: int
    generation: int
    fence_token: int
    code: str


@dataclass(frozen=True, slots=True)
class TargetLifecycle:
    """Состояние lifecycle без раскрытия worker payload."""

    target_id: int
    status: str
    generation: int
    fence_token: int
    collection_mode: str


CapabilityChecker = Callable[[str, int, str], bool]


class TargetAccessService:
    """Меняет подписки и lifecycle только canonical Telegram-цели.

    ``capability_checker`` должен быть серверной проверкой active
    ``module_admin`` и workspace capability. Его отсутствие намеренно означает
    deny: сервис нельзя безопасно вызвать из неавторизованного транспорта.
    """

    def __init__(self, session: Session, *, capability_checker: CapabilityChecker | None = None) -> None:
        self._session = session
        self._capability_checker = capability_checker

    def grant_workspace_subscription(
        self,
        *,
        actor: str,
        actor_workspace_id: int,
        workspace_id: int,
        target_id: int,
    ) -> WorkspaceSubscription:
        """Выдаёт или реактивирует подписку своего workspace."""
        target = self._canonical_target(target_id)
        self._require_workspace_capability(
            actor, actor_workspace_id, workspace_id, _CAPABILITY_SUBSCRIPTION, target_id
        )
        existing = self._subscription(workspace_id, int(target["target_id"]))
        if existing is None:
            self._session.execute(
                text(
                    "INSERT INTO loophole_telegram_workspace_subscription "
                    "(workspace_id, target_id, status, grant_version, intent_sequence, granted_by) "
                    "VALUES (:workspace_id, :target_id, 'active', 1, 1, :actor)"
                ),
                {"workspace_id": workspace_id, "target_id": target_id, "actor": actor},
            )
            version = 1
        elif str(existing["status"]) == "active":
            version = int(existing["grant_version"])
        else:
            version = int(existing["grant_version"]) + 1
            self._session.execute(
                text(
                    "UPDATE loophole_telegram_workspace_subscription "
                    "SET status = 'active', grant_version = :version, "
                    "intent_sequence = intent_sequence + 1, granted_by = :actor, "
                    "updated_at = CURRENT_TIMESTAMP, revoked_at = NULL "
                    "WHERE workspace_id = :workspace_id AND target_id = :target_id"
                ),
                {
                    "workspace_id": workspace_id,
                    "target_id": target_id,
                    "version": version,
                    "actor": actor,
                },
            )
        self._audit(actor, target_id, workspace_id, "subscription_grant", "allow")
        return WorkspaceSubscription(target_id, workspace_id, "active", version)

    def revoke_workspace_subscription(
        self,
        *,
        actor: str,
        actor_workspace_id: int,
        workspace_id: int,
        target_id: int,
    ) -> WorkspaceSubscription:
        """Отзывает подписку, сохраняя её строку и версию намерения."""
        target = self._canonical_target(target_id)
        self._require_workspace_capability(
            actor, actor_workspace_id, workspace_id, _CAPABILITY_SUBSCRIPTION, target_id
        )
        existing = self._subscription(workspace_id, int(target["target_id"]))
        if existing is None:
            raise TargetSubscriptionNotFound("Подписка workspace на Telegram-цель не найдена")
        if str(existing["status"]) == "revoked":
            version = int(existing["grant_version"])
        else:
            version = int(existing["grant_version"]) + 1
            self._session.execute(
                text(
                    "UPDATE loophole_telegram_workspace_subscription "
                    "SET status = 'revoked', grant_version = :version, "
                    "intent_sequence = intent_sequence + 1, updated_at = CURRENT_TIMESTAMP, "
                    "revoked_at = CURRENT_TIMESTAMP "
                    "WHERE workspace_id = :workspace_id AND target_id = :target_id"
                ),
                {"workspace_id": workspace_id, "target_id": target_id, "version": version},
            )
        self._audit(actor, target_id, workspace_id, "subscription_revoke", "allow")
        return WorkspaceSubscription(target_id, workspace_id, "revoked", version)

    def deactivate_target(self, *, actor: str, target_id: int) -> FencedTerminalSignal:
        """Останавливает новое выполнение и fence-ит уже выданные lease."""
        target = self._canonical_target(target_id)
        self._require_lifecycle_capability(actor, target_id)
        current_generation = int(target["generation"])
        current_fence = int(target["fence_token"])
        if str(target["lifecycle_status"]) == "inactive":
            signal = self._terminal_signal(target_id, current_generation - 1, current_fence - 1)
            if signal is None:
                raise TargetInactiveError("Деактивированная цель не имеет terminal-сигнала")
            return signal

        self._session.execute(
            text(
                "UPDATE loophole_telegram_target "
                "SET lifecycle_status = 'inactive', generation = :generation, "
                "fence_token = :fence_token WHERE target_id = :target_id"
            ),
            {
                "target_id": target_id,
                "generation": current_generation + 1,
                "fence_token": current_fence + 1,
            },
        )
        self._session.execute(
            text(
                "INSERT INTO loophole_telegram_terminal_signal "
                "(target_id, generation, fence_token, code) "
                "VALUES (:target_id, :generation, :fence_token, 'target_deactivated')"
            ),
            {
                "target_id": target_id,
                "generation": current_generation,
                "fence_token": current_fence,
            },
        )
        signal = FencedTerminalSignal(
            target_id=target_id,
            generation=current_generation,
            fence_token=current_fence,
            code="target_deactivated",
        )
        self._audit(actor, target_id, None, "target_deactivate", "allow")
        return signal

    def activate_target(self, *, actor: str, target_id: int) -> TargetLifecycle:
        """Повторно разрешает worker, не сбрасывая checkpoint или историю."""
        target = self._canonical_target(target_id)
        self._require_lifecycle_capability(actor, target_id)
        if str(target["lifecycle_status"]) == "inactive":
            self._session.execute(
                text(
                    "UPDATE loophole_telegram_target SET lifecycle_status = 'active' "
                    "WHERE target_id = :target_id"
                ),
                {"target_id": target_id},
            )
            self._audit(actor, target_id, None, "target_activate", "allow")
            target = dict(target)
            target["lifecycle_status"] = "active"
        return self._lifecycle(target)

    def start_collection(self, *, target_id: int) -> TargetCollectionLease:
        """Выдаёт worker lease только active canonical-цели."""
        target = self._canonical_target(target_id)
        if str(target["lifecycle_status"]) != "active":
            raise TargetInactiveError("Telegram-цель деактивирована: новый обход запрещён")
        return TargetCollectionLease(
            target_id=target_id,
            generation=int(target["generation"]),
            fence_token=int(target["fence_token"]),
        )

    def terminal_signal_for(self, lease: TargetCollectionLease) -> FencedTerminalSignal | None:
        """Возвращает deactivation-сигнал только для точного старого fence."""
        return self._terminal_signal(lease.target_id, lease.generation, lease.fence_token)

    def _canonical_target(self, target_id: int):
        target = self._session.execute(
            text(
                "SELECT target_id, canonical_target_id, lifecycle_status, generation, "
                "fence_token, checkpoint_json FROM loophole_telegram_target "
                "WHERE target_id = :target_id"
            ),
            {"target_id": target_id},
        ).mappings().first()
        if target is None:
            raise TargetNotFoundError("Telegram-цель не найдена")
        if target["canonical_target_id"] is not None:
            raise TargetRedirectError("Redirect ID нельзя использовать для управления целью")
        return target

    def _require_workspace_capability(
        self,
        actor: str,
        actor_workspace_id: int,
        workspace_id: int,
        capability: str,
        target_id: int,
    ) -> None:
        if actor_workspace_id != workspace_id or not self._has_capability(actor, workspace_id, capability):
            self._audit(actor, target_id, workspace_id, "subscription_access", "deny")
            raise TargetAccessDenied("Нет capability для управления подпиской workspace")

    def _require_lifecycle_capability(self, actor: str, target_id: int) -> None:
        if not self._has_capability(actor, 0, _CAPABILITY_LIFECYCLE):
            self._audit(actor, target_id, None, "target_lifecycle_access", "deny")
            raise TargetAccessDenied("Нет capability для управления lifecycle Telegram-цели")

    def _has_capability(self, actor: str, workspace_id: int, capability: str) -> bool:
        return self._capability_checker is not None and self._capability_checker(
            actor, workspace_id, capability
        )

    def _subscription(self, workspace_id: int, target_id: int):
        return self._session.execute(
            text(
                "SELECT status, grant_version FROM loophole_telegram_workspace_subscription "
                "WHERE workspace_id = :workspace_id AND target_id = :target_id"
            ),
            {"workspace_id": workspace_id, "target_id": target_id},
        ).mappings().first()

    def _terminal_signal(
        self, target_id: int, generation: int, fence_token: int
    ) -> FencedTerminalSignal | None:
        row = self._session.execute(
            text(
                "SELECT target_id, generation, fence_token, code "
                "FROM loophole_telegram_terminal_signal WHERE target_id = :target_id "
                "AND generation = :generation AND fence_token = :fence_token"
            ),
            {"target_id": target_id, "generation": generation, "fence_token": fence_token},
        ).mappings().first()
        if row is None:
            return None
        return FencedTerminalSignal(
            target_id=int(row["target_id"]),
            generation=int(row["generation"]),
            fence_token=int(row["fence_token"]),
            code=str(row["code"]),
        )

    @staticmethod
    def _lifecycle(target) -> TargetLifecycle:
        checkpoint = target["checkpoint_json"]
        if checkpoint:
            try:
                collection_mode = "incremental" if json.loads(str(checkpoint)) else "initial"
            except json.JSONDecodeError:
                collection_mode = "incremental"
        else:
            collection_mode = "initial"
        return TargetLifecycle(
            target_id=int(target["target_id"]),
            status=str(target["lifecycle_status"]),
            generation=int(target["generation"]),
            fence_token=int(target["fence_token"]),
            collection_mode=collection_mode,
        )

    def _audit(
        self,
        actor: str,
        target_id: int,
        workspace_id: int | None,
        action: str,
        result: str,
    ) -> None:
        self._session.execute(
            text(
                "INSERT INTO loophole_telegram_target_audit "
                "(target_id, workspace_id, actor_username, action, result) "
                "VALUES (:target_id, :workspace_id, :actor, :action, :result)"
            ),
            {
                "target_id": target_id,
                "workspace_id": workspace_id,
                "actor": actor,
                "action": action,
                "result": result,
            },
        )
