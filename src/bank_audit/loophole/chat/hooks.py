"""Lifecycle hook для nanobot-агента loophole.

Собирает:
- использованные tools;
- итоговый ответ (final_answer);
- records из audit_table_load / audit_export для отображения в таблице.

Передаёт текстовые дельты в callback (для SSE-стриминга).
"""
from __future__ import annotations

import re
from typing import Any

from .. import repository as repo

UNKNOWN_PUBLIC_TOOL = "инструмент недоступен"


_STREAM_EMAIL_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]*"
)


def redact_stream_text(value: Any) -> str:
    """Маскирует ПДн и credentials перед сохранением или выдачей delta."""
    masked = repo.redact_audit_text(value, limit=10000)
    return _STREAM_EMAIL_CANDIDATE.sub("[EMAIL]", masked)


class StreamRedactor:
    """Буферизует весь текст до единого полного redaction перед SSE."""

    def __init__(self) -> None:
        self._pending = ""

    def push(self, value: Any) -> str:
        """Накапливает delta и намеренно ничего не публикует до завершения."""
        self._pending += str(value or "")
        return ""

    def flush(self) -> str:
        """Маскирует и выдаёт полный накопленный текст одним сообщением."""
        safe = redact_stream_text(self._pending)
        self._pending = ""
        return safe


def public_tool_name(name: Any) -> str:
    """Возвращает только публичное имя из server-side read-only allowlist."""
    from ..agent.registry import DEFAULT_ALLOWED_SKILLS

    return name if isinstance(name, str) and name in DEFAULT_ALLOWED_SKILLS else UNKNOWN_PUBLIC_TOOL


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
        self._stream_source: str = ""
        self._stream_redactor = StreamRedactor()
        self.tool_errors: list[str] = []
        self._current_tool_name: str | None = None
        self.iterations = 0
        self.stop_reason: str | None = None

    def wants_streaming(self) -> bool:
        return True

    async def on_stream(self, context: Any, delta: str) -> None:
        self._stream_source += str(delta or "")
        self.final_answer = redact_stream_text(self._stream_source)

    def stream_delta_for_sse(self, delta: Any) -> str:
        """Возвращает безопасную SSE-дельту с учётом предыдущих chunks."""
        return self._stream_redactor.push(delta)

    def flush_stream_for_sse(self) -> str:
        """Выдаёт полный безопасный ответ после окончания stream."""
        safe_answer = self._stream_redactor.flush()
        if safe_answer and not self.final_answer:
            self.final_answer = safe_answer
        return safe_answer

    def _add_tool(self, name: Any) -> None:
        public_name = public_tool_name(name)
        if public_name not in self.tools_used:
            self.tools_used.append(public_name)

    def record_stream_event(
        self,
        event_type: Any,
        *,
        name: Any = None,
        error: Any = None,
    ) -> str:
        """Фиксирует только безопасный итог stream-события в hook."""
        event_name = str(event_type or "").lower()
        public_name = public_tool_name(name)
        if event_name == "tool.failed":
            self._add_tool(name)
            if "skill_failed" not in self.tool_errors:
                self.tool_errors.append("skill_failed")
        elif event_name == "run.failed":
            if "agent_error" not in self.tool_errors:
                self.tool_errors.append("agent_error")
        return public_name

    async def after_iteration(self, context: Any) -> None:
        iteration = getattr(context, "iteration", None)
        if isinstance(iteration, int) and iteration >= 0:
            self.iterations = max(self.iterations, iteration + 1)
        stop_reason = getattr(context, "stop_reason", None)
        if stop_reason:
            self.stop_reason = str(stop_reason)

        for call in getattr(context, "tool_calls", []):
            name = getattr(call, "name", None)
            self._add_tool(name)
        for event in getattr(context, "tool_events", []):
            if isinstance(event, dict):
                name = event.get("name") or event.get("tool_name")
                status = event.get("status") or event.get("type")
                error = event.get("error")
            else:
                name = getattr(event, "name", None) or getattr(event, "tool_name", None)
                status = getattr(event, "status", None) or getattr(event, "type", None)
                error = getattr(event, "error", None)
            self._add_tool(name)
            failed = str(status).lower() in {"error", "failed", "tool_failed"} or bool(error)
            if failed and "skill_failed" not in self.tool_errors:
                self.tool_errors.append("skill_failed")

    async def after_run(self, context: Any) -> None:
        final = getattr(context, "final_content", None)
        if final:
            self._stream_source = str(final)
            self.final_answer = redact_stream_text(final)
        stop_reason = getattr(context, "stop_reason", None)
        if stop_reason:
            self.stop_reason = str(stop_reason)
        for name in getattr(context, "tools_used", []):
            self._add_tool(name)

    def finalize_content(self, context: Any, content: str | None) -> str | None:
        if content is None:
            return content
        return redact_stream_text(content)
