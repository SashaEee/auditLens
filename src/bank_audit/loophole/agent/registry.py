"""Allowlist-реестр навыков управляемого агента."""
from __future__ import annotations

import os
from collections.abc import Iterable

DEFAULT_ALLOWED_SKILLS = (
    "audit_web_search",
    "audit_web_fetch",
    "audit_extract_loopholes",
    "audit_db_query",
    "audit_table_load",
    "audit_export",
)


class UnknownSkillError(ValueError):
    """Запрошенный skill отсутствует в server-side allowlist."""


class SkillRegistry:
    """Реестр классов nanobot tools с fail-closed выбором."""

    def __init__(self, tool_classes: Iterable[type], *, allowlist: Iterable[str]) -> None:
        self._allowlist = tuple(dict.fromkeys(allowlist))
        allowed_names = set(DEFAULT_ALLOWED_SKILLS)
        unknown = set(self._allowlist) - allowed_names
        if unknown:
            raise UnknownSkillError(
                "Skill отсутствует в неизменяемом read-only allowlist: "
                + ", ".join(sorted(unknown))
            )

        self._tools = {}
        not_read_only: list[str] = []
        for tool_class in tool_classes:
            tool = tool_class()
            name = tool.name
            if name in self._tools:
                raise UnknownSkillError(f"Дублирующийся skill запрещён: {name}")
            self._tools[name] = tool_class
            if name in self._allowlist and getattr(tool, "read_only", False) is not True:
                not_read_only.append(name)

        missing = set(self._allowlist) - self._tools.keys()
        if missing or not_read_only:
            details = sorted(missing | set(not_read_only))
            raise UnknownSkillError(
                "Skill не прошёл server-side read-only проверку: "
                + ", ".join(details)
            )

    @classmethod
    def default(cls) -> SkillRegistry:
        """Создаёт реестр из фиксированного server-side набора tools.

        Переменная окружения сохраняется только как проверка deploy-настройки:
        она может подтвердить подмножество read-only skills, но не меняет
        зарегистрированный allowlist. Любое write/неизвестное имя блокирует
        запуск целиком.
        """
        from ..chat.tools_nanobot import NANOBOT_TOOLS

        configured = os.getenv("LOOPHOLE_AGENT_SKILLS")
        if configured and configured.strip():
            requested = tuple(name.strip() for name in configured.split(",") if name.strip())
            invalid = set(requested) - set(DEFAULT_ALLOWED_SKILLS)
            if invalid:
                raise UnknownSkillError(
                    "Env содержит skill вне неизменяемого read-only allowlist: "
                    + ", ".join(sorted(invalid))
                )
        return cls(NANOBOT_TOOLS, allowlist=DEFAULT_ALLOWED_SKILLS)

    @property
    def names(self) -> tuple[str, ...]:
        """Имена активных skills без технических аргументов."""
        return self._allowlist

    def select(self, names: Iterable[str]) -> tuple[type, ...]:
        """Возвращает tools только из allowlist, иначе останавливает запуск."""
        selected: list[type] = []
        for name in names:
            if name not in self._allowlist:
                raise UnknownSkillError(f"Skill запрещён allowlist: {name}")
            selected.append(self._tools[name])
        return tuple(selected)

    def tool_classes(self) -> tuple[type, ...]:
        """Возвращает все разрешённые классы в стабильном порядке."""
        return self.select(self._allowlist)
