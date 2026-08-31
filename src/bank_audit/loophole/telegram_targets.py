"""Изолированная регистрация Telegram-целей для Story 6.1.

Сервис только нормализует и сохраняет адрес цели. Он намеренно не выполняет
проверку доступа аккаунта, сбор сообщений, вступление в группу или управление
сессией: это контракты последующих историй.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_PUBLIC_HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_INVITE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{5,128}$")
_TELEGRAM_HOSTS = {"t.me", "telegram.me"}


class InvalidTelegramTarget(ValueError):
    """Адрес не является допустимой Telegram-целью для мониторинга."""


@dataclass(frozen=True, slots=True)
class RegisteredTelegramTarget:
    """Результат регистрации без заявления о доступе или собранных данных."""

    target_id: int
    normalized_address: str
    target_kind: str
    registration: str
    account_access: str = "unchecked"
    collection: str = "not_started"


class TargetRegistryService:
    """Идемпотентно регистрирует Telegram-цели в выделенной таблице.

    Уникальность ``normalized_address`` является инвариантом схемы. Вставка
    выполняется в savepoint, поэтому конфликт уникальности не откатывает
    внешнюю транзакцию вызывающего кода.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def register(self, address: str) -> RegisteredTelegramTarget:
        """Создаёт цель или возвращает прежнюю, не меняя её произвольно."""
        normalized_address, target_kind = normalize_telegram_target(address)
        row = self._find(normalized_address)
        if row is not None:
            return self._result(row, registration="existing")

        try:
            with self._session.begin_nested():
                self._session.execute(
                    text(
                        "INSERT INTO loophole_telegram_target "
                        "(normalized_address, target_kind) VALUES (:address, :kind)"
                    ),
                    {"address": normalized_address, "kind": target_kind},
                )
        except IntegrityError:
            row = self._find(normalized_address)
            if row is None:
                raise
            return self._result(row, registration="existing")

        row = self._find(normalized_address)
        if row is None:  # pragma: no cover - защита от нештатного DB-драйвера
            raise RuntimeError("Зарегистрированная Telegram-цель не найдена")
        return self._result(row, registration="created")

    def _find(self, normalized_address: str):
        return self._session.execute(
            text(
                "SELECT target_id, normalized_address, target_kind "
                "FROM loophole_telegram_target WHERE normalized_address = :address"
            ),
            {"address": normalized_address},
        ).mappings().first()

    @staticmethod
    def _result(row, *, registration: str) -> RegisteredTelegramTarget:
        return RegisteredTelegramTarget(
            target_id=int(row["target_id"]),
            normalized_address=str(row["normalized_address"]),
            target_kind=str(row["target_kind"]),
            registration=registration,
        )


def normalize_telegram_target(address: str) -> tuple[str, str]:
    """Возвращает канонический адрес и тип публичной или invite-цели.

    Допускаются только ``@handle``, ``t.me/handle`` и invite-ссылки
    ``t.me/+token``/``telegram.me/joinchat/token``. Любой иной ввод отклоняется
    до обращения к БД, чтобы сервис работал fail-closed.
    """
    raw = (address or "").strip().rstrip(".,);]")
    if not raw:
        raise InvalidTelegramTarget("Не задан адрес Telegram-цели")
    if raw.startswith("@"):
        handle = raw[1:]
        if not _PUBLIC_HANDLE.fullmatch(handle):
            raise InvalidTelegramTarget("Некорректный Telegram-хендл")
        return f"t.me/{handle.lower()}", "public"

    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidTelegramTarget("Некорректная Telegram-ссылка") from exc
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidTelegramTarget("Некорректная Telegram-ссылка") from exc
    if (
        parts.scheme not in {"http", "https"}
        or parts.hostname is None
        or parts.hostname.lower() not in _TELEGRAM_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.query
        or parts.fragment
    ):
        raise InvalidTelegramTarget("Поддерживаются только безопасные Telegram-ссылки")

    path = parts.path.strip("/")
    if path.startswith("+") and _INVITE_TOKEN.fullmatch(path[1:]):
        return f"t.me/+{path[1:]}", "invite"
    if path.startswith("joinchat/"):
        token = path.removeprefix("joinchat/")
        if _INVITE_TOKEN.fullmatch(token):
            return f"t.me/+{token}", "invite"
    if path.lower() == "joinchat":
        raise InvalidTelegramTarget("Неполная invite-ссылка Telegram")
    if "/" not in path and _PUBLIC_HANDLE.fullmatch(path):
        return f"t.me/{path.lower()}", "public"
    raise InvalidTelegramTarget("Некорректный адрес Telegram-цели")

