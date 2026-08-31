"""Управляемый ReAct-агент исследования loophole."""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..chat.hooks import AuditHook, redact_stream_text
from ..chat.nanobot_agent import create_nanobot
from ..chat.tools_nanobot import ToolContext
from ..config import LoopholeSettings
from .registry import DEFAULT_ALLOWED_SKILLS, SkillRegistry, UnknownSkillError

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,127})$")
AGENT_UNAVAILABLE_MESSAGE = (
    "Аналитик временно недоступен. Повторите запрос через несколько секунд."
)


def _safe_run_id(value: str) -> str:
    """Проверяет run_id как безопасный slug/UUID без path-компонентов."""
    if not isinstance(value, str) or not _SAFE_RUN_ID.fullmatch(value):
        raise ValueError("Некорректный run_id: разрешён только безопасный slug или UUID")
    return value


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """Неизменяемый контекст одного изолированного запуска."""

    user_id: str
    workspace_id: int | None
    query: str
    run_id: str
    max_iterations: int | None = None
    pending_records: list[dict] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Безопасный результат запуска без payload tools."""

    answer: str
    tools_used: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    partial: bool = False
    iterations: int = 0
    run_id: str = ""
    records: tuple[dict, ...] = ()
    stop_reason: str | None = None


def _public_partial_answer(
    answer: str,
    errors: tuple[str, ...],
    *,
    iterations: int = 0,
) -> str:
    if not errors:
        return answer
    if "max_iterations" in errors:
        suffix = f" ({iterations})" if iterations else ""
        explanation = (
            "Исследование завершено частично: достигнут лимит итераций"
            f"{suffix}."
        )
    elif "agent_error" in errors:
        explanation = AGENT_UNAVAILABLE_MESSAGE
    else:
        explanation = "Исследование завершено частично: один из инструментов недоступен."
    if explanation in answer:
        return answer
    return answer + chr(10) * 2 + explanation if answer else explanation


class ManagedAgent:
    """Адаптер жизненного цикла nanobot для одного AgentRunContext."""

    def __init__(self, context: AgentRunContext, bot: Any, config_path: str) -> None:
        self.context = context
        self._bot = bot
        self._config_path = config_path
        self.last_result: AgentResult | None = None

    async def run(self, prompt: str | None = None, *, session: Any = None) -> AgentResult:
        """Выполняет один запуск и превращает частичный сбой в результат."""
        hook = AuditHook(session=session)
        errors: list[str] = []
        result: Any = None
        try:
            result = await self._bot.run(
                prompt or self.context.query,
                session_key=f"loophole:{self.context.workspace_id}:{self.context.run_id}",
                channel="loophole",
                hooks=[hook],
            )
            errors.extend(hook.tool_errors)
            answer = redact_stream_text(hook.final_answer or getattr(result, "content", "") or "")
        except Exception:  # noqa: BLE001 — внешний harness не раскрывается пользователю
            errors.extend(hook.tool_errors)
            if "agent_error" not in errors:
                errors.append("agent_error")
            answer = redact_stream_text(hook.final_answer)
        finally:
            await self.aclose()

        hook.records = list(self.context.pending_records)

        stop_reason = getattr(result, "stop_reason", None) or getattr(hook, "stop_reason", None)
        metadata = getattr(result, "metadata", None)
        metadata_iterations = metadata.get("iterations") if isinstance(metadata, dict) else None
        iterations = getattr(hook, "iterations", 0) or metadata_iterations or 0
        if stop_reason == "max_iterations":
            if "max_iterations" not in errors:
                errors.append("max_iterations")
            if not iterations:
                iterations = self.context.max_iterations or LoopholeSettings.load().nanobot_max_iterations
        if stop_reason == "error":
            if "agent_error" not in errors:
                errors.append("agent_error")
            answer = ""
        if getattr(result, "error", None) and "agent_error" not in errors:
            errors.append("agent_error")
        errors_tuple = tuple(dict.fromkeys(errors))
        final = _public_partial_answer(answer, errors_tuple, iterations=int(iterations or 0))
        self.last_result = AgentResult(
            answer=final,
            tools_used=tuple(dict.fromkeys(hook.tools_used)),
            errors=errors_tuple,
            partial=bool(errors_tuple),
            iterations=int(iterations or 0),
            run_id=self.context.run_id,
            records=tuple(hook.records),
            stop_reason=stop_reason,
        )
        return self.last_result

    async def stream(self, prompt: str, *, hook: AuditHook) -> Any:
        """Стримит события nanobot и закрывает ресурсы запуска."""
        try:
            async for event in self._bot.stream(
                prompt,
                session_key=f"loophole:{self.context.workspace_id}:{self.context.run_id}",
                channel="loophole",
                hooks=[hook],
            ):
                yield event
        finally:
            hook.records = list(self.context.pending_records)
            await self.aclose()

    async def aclose(self) -> None:
        """Закрывает nanobot и удаляет временный конфиг."""
        bot, config_path = self._bot, self._config_path
        self._bot = None
        self._config_path = ""
        close_task = asyncio.create_task(bot.aclose()) if bot is not None else None
        cancelled = False
        try:
            while close_task is not None and not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError:
                    cancelled = True
            if close_task is not None:
                close_task.result()
        finally:
            if config_path:
                Path(config_path).unlink(missing_ok=True)
        if cancelled:
            raise asyncio.CancelledError


class AgentFactory:
    """Создаёт отдельный managed agent для каждого запуска."""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or SkillRegistry.default()

    def create(
        self,
        context: AgentRunContext,
        *,
        llm: Any = None,
        session: Any = None,
    ) -> ManagedAgent:
        """Создаёт nanobot только с server-side разрешёнными tools."""
        settings = LoopholeSettings.load()
        run_id = _safe_run_id(context.run_id or str(uuid.uuid4()))
        workspace_root = Path(settings.workspace_dir).expanduser().resolve()
        workspace = (
            workspace_root
            / f"workspace-{context.workspace_id}"
            / run_id
        ).resolve()
        try:
            workspace.relative_to(workspace_root)
        except ValueError as exc:
            raise ValueError("Путь workspace выходит за пределы корня агента") from exc
        bot, config_path = create_nanobot(
            model=llm,
            max_iterations=context.max_iterations,
            workspace=workspace,
            tool_classes=self.registry.tool_classes(),
            tool_context=ToolContext(
                user_id=context.user_id,
                workspace_id=context.workspace_id,
                session=session,
                query=context.query,
                pending_records=context.pending_records,
            ),
        )
        return ManagedAgent(
            AgentRunContext(
                user_id=context.user_id,
                workspace_id=context.workspace_id,
                query=context.query,
                run_id=run_id,
                max_iterations=context.max_iterations,
                pending_records=context.pending_records,
            ),
            bot,
            config_path,
        )


__all__ = [
    "AGENT_UNAVAILABLE_MESSAGE",
    "DEFAULT_ALLOWED_SKILLS",
    "AgentFactory",
    "AgentResult",
    "AgentRunContext",
    "ManagedAgent",
    "SkillRegistry",
    "UnknownSkillError",
    "create_nanobot",
]
