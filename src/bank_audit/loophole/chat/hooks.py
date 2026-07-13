"""Lifecycle hook для nanobot-агента loophole.

Собирает:
- использованные tools;
- итоговый ответ (final_answer);
- records из audit_table_load / audit_export для отображения в таблице.

Передаёт текстовые дельты в callback (для SSE-стриминга).
"""
from __future__ import annotations

from typing import Any

from nanobot.agent.hook import AgentHook

from ..pii_mask import mask as pii_mask


class AuditHook(AgentHook):
    """Hook для nanobot-запуска: собирает tools, records, финальный ответ."""

    def __init__(self, *, session: Any = None) -> None:
        super().__init__()
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
