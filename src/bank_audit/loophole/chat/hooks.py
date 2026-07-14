"""Lifecycle hook для nanobot-агента loophole.

Собирает:
- использованные tools;
- итоговый ответ (final_answer);
- records из audit_table_load / audit_export для отображения в таблице.

Передаёт текстовые дельты в callback (для SSE-стриминга).
"""
from __future__ import annotations

from typing import Any

from ..pii_mask import mask as pii_mask


def _audit_hook_base() -> type:
    """Возвращает AgentHook из nanobot, если доступен, иначе object-заглушку."""
    try:
        from nanobot.agent.hook import AgentHook  # type: ignore[import-not-found]

        return AgentHook
    except ImportError:  # pragma: no cover - nanobot optional
        return object


class AuditHook(_audit_hook_base()):
    """Hook для nanobot-запуска: собирает tools, records, финальный ответ."""

    def __init__(self, *, session: Any = None) -> None:
        base = _audit_hook_base()
        if base is object:
            raise RuntimeError(
                "nanobot-ai не установлен. Установите: pip install -e '.[loophole-nanobot]'"
            )
        super(base, self).__init__()
        self.session = session
        self.tools_used: list[str] = []
        self.records: list[dict] = []
        self.final_answer: str = ""
        self._current_tool_name: str | None = None

    def wants_streaming(self) -> bool:
        return True

    async def on_stream(self, context: Any, delta: str) -> None:
        self.final_answer += delta

    async def after_iteration(self, context: Any) -> None:
        for call in getattr(context, "tool_calls", []):
            name = getattr(call, "name", None)
            if name:
                self.tools_used.append(name)
        for event in getattr(context, "tool_events", []):
            if isinstance(event, dict):
                name = event.get("tool_name")
                if name:
                    self.tools_used.append(name)

    async def after_run(self, context: Any) -> None:
        final = getattr(context, "final_content", None)
        if final:
            self.final_answer = str(final)
        for name in getattr(context, "tools_used", []):
            if name not in self.tools_used:
                self.tools_used.append(name)

    def finalize_content(self, context: Any, content: str | None) -> str | None:
        if content is None:
            return content
        masked, _ = pii_mask(content)
        return masked
