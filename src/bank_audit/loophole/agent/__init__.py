"""Управляемый ReAct-агент исследования loophole."""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from ..chat.hooks import MODEL_PROTOCOL_ERROR, AuditHook, redact_stream_text
from ..chat.nanobot_agent import create_nanobot
from ..chat.tools_nanobot import ToolContext, _source_publication_period_error
from ..config import LoopholeSettings
from ..run_budget import ResearchBudget, requested_finding_count
from .registry import DEFAULT_ALLOWED_SKILLS, SkillRegistry, UnknownSkillError

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,127})$")
AGENT_UNAVAILABLE_MESSAGE = (
    "Аналитик временно недоступен. Повторите запрос через несколько секунд."
)
AGENT_TIME_BUDGET_MESSAGE = (
    "Исследование завершено частично: исчерпан общий бюджет времени. "
    "Представлены только результаты, полученные до остановки."
)
_PROGRESS_INTERVAL_SECONDS = 5.0
_CLEANUP_TIMEOUT_SECONDS = 2.0
log = structlog.get_logger(__name__)


async def _await_cleanup(task: asyncio.Task, *, deadline: float) -> None:
    """Даёт cleanup короткий срок и защищает его от повторной внешней отмены."""
    cancelled = False
    while not task.done():
        try:
            done, _ = await asyncio.wait((task,), timeout=max(0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            cancelled = True
            continue
        if not done:
            task.cancel()
            task.add_done_callback(lambda completed: (
                None if completed.cancelled() else completed.exception()
            ))
            if cancelled:
                raise asyncio.CancelledError
            raise TimeoutError("Превышен срок закрытия агента")
    if cancelled:
        raise asyncio.CancelledError
    if task.cancelled():
        # Собственный timeout cleanup не является отменой запроса пользователем.
        raise TimeoutError("Закрытие агента прервано по внутреннему лимиту")
    task.result()


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
    fetched_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    budget: ResearchBudget | None = field(default=None, compare=False)


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
    sources: tuple[dict, ...] = ()
    stop_reason: str | None = None


def eligible_findings(context: AgentRunContext) -> list[dict]:
    """Выбирает AI-кандидатов с цитатой из прочитанного источника и нужной датой."""
    sources = {
        str(source.get("url")): source
        for source in context.fetched_sources.values()
        if isinstance(source, dict) and source.get("url")
    }
    period_context = ToolContext(
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        session=None,
        query=context.query,
        source_publication_dates={url: source.get("published_at") for url, source in sources.items()},
    )
    selected = []
    seen = set()
    for finding in context.pending_records:
        url = str(finding.get("url") or "")
        quote = str(finding.get("evidence_quote") or "").strip()
        title = str(finding.get("title") or "").strip()
        source = sources.get(url)
        if finding.get("is_loophole") is not True or not title or not quote or not source:
            continue
        if _source_publication_period_error(period_context, url):
            continue
        # LLM получает redacted source; сравнение идёт с тем же безопасным представлением.
        raw_text = str(source.get("extracted_text") or "")
        normalized_quote = " ".join(
            redact_stream_text(quote, limit=max(10000, len(quote) * 2)).split()
        ).casefold()
        normalized_source = " ".join(
            redact_stream_text(raw_text, limit=max(10000, len(raw_text) * 2)).split()
        ).casefold()
        key = (url, normalized_quote)
        if not normalized_quote or normalized_quote not in normalized_source or key in seen:
            continue
        seen.add(key)
        selected.append(finding)
    return selected


def _candidate_report(records: list[dict]) -> str:
    """Формирует отчёт без нового обращения к LLM и без статуса экспертного подтверждения."""
    if not records:
        return ""
    parts = ["Найденные AI-кандидаты требуют проверки аудитора; решение ЦК КС не присвоено."]
    for index, record in enumerate(records, 1):
        parts.extend([
            f"{index}. {record['title']}",
            f"Механизм по источнику: {record.get('description') or record.get('snippet')}",
            f"Цитата: {record['evidence_quote']}",
            f"Источник: {record['url']}",
            f"Дата публикации: {record.get('published_at') or 'не установлена'}",
        ])
    parts.append("Рекомендация аудитору: сверить механизм с условиями продукта и доказательствами.")
    return redact_stream_text("\n\n".join(parts))


class _ResearchStopped(asyncio.CancelledError):
    """Внутренняя остановка runner до следующего обращения к модели."""


class _BudgetHook(AuditHook):
    """Проверяет бюджет на границах итераций и пишет только безопасные тайминги."""

    def __init__(self, agent: ManagedAgent) -> None:
        super().__init__()
        self._agent = agent
        self._reraise = True

    async def before_iteration(self, context: Any) -> None:
        self._agent._check_limits()
        self._agent._set_phase("waiting_model", iteration=getattr(context, "iteration", 0))

    async def before_execute_tools(self, context: Any) -> None:
        self._agent._check_limits()
        self._agent._set_phase("research_tools", iteration=getattr(context, "iteration", 0))

    async def after_iteration(self, context: Any) -> None:
        self._agent._check_limits()


def _public_partial_answer(
    answer: str,
    errors: tuple[str, ...],
    *,
    iterations: int = 0,
) -> str:
    if not errors:
        return answer
    if "time_budget" in errors:
        explanation = AGENT_TIME_BUDGET_MESSAGE
    elif "max_iterations" in errors:
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

    def __init__(
        self, context: AgentRunContext, bot: Any, config_path: str, *, model: str | None = None,
    ) -> None:
        self.context = context
        self._bot = bot
        self._config_path = config_path
        self.last_result: AgentResult | None = None
        self._model = model or LoopholeSettings.load().effective_nanobot_model()
        self._budget = context.budget or ResearchBudget(
            timeout_seconds=LoopholeSettings.load().agent_timeout_seconds,
            requested_count=requested_finding_count(context.query),
        )
        self._phase_started_at = time.monotonic()
        self._phase_durations: dict[str, float] = {}
        self._budget_finished = False
        self._cleanup_deadline: float | None = None

    async def _wait_cleanup(self, task: asyncio.Task) -> None:
        if self._cleanup_deadline is None:
            self._cleanup_deadline = time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
        await _await_cleanup(task, deadline=self._cleanup_deadline)

    def _set_phase(self, phase: str, *, iteration: int = 0) -> None:
        now = time.monotonic()
        previous = self._budget.phase
        duration = now - self._phase_started_at
        self._phase_durations[previous] = self._phase_durations.get(previous, 0.0) + duration
        self._phase_started_at = now
        self._budget.phase = phase
        log.info(
            "loophole_agent_phase", run_id=self.context.run_id, phase=phase,
            previous_phase=previous, duration_ms=round(duration * 1000), iteration=iteration,
            elapsed_seconds=self._budget.elapsed_seconds,
        )

    def _check_limits(self, *, check_findings: bool = True) -> None:
        if self._budget.stop_reason:
            raise _ResearchStopped
        if self._budget.expired:
            self._budget.stop_reason = "time_budget"
            raise _ResearchStopped
        count = self._budget.requested_count
        if check_findings and count and len(eligible_findings(self.context)) >= count:
            self._budget.stop_reason = "requested_count"
            raise _ResearchStopped

    def _finish_budget_stop(self, hook: AuditHook) -> None:
        if self._budget_finished:
            return
        self._budget_finished = True
        records = eligible_findings(self.context)
        count = self._budget.requested_count
        if count:
            records = records[:count]
        self.context.pending_records[:] = records
        report = _candidate_report(records)
        if self._budget.stop_reason == "requested_count":
            hook.final_answer = report
        else:
            hook.final_answer = report or (
                "До остановки не получено AI-кандидатов, прошедших проверку "
                "источника и условий запроса."
            )
            if "time_budget" not in hook.tool_errors:
                hook.tool_errors.append("time_budget")
        hook.stop_reason = self._budget.stop_reason

    async def run(self, prompt: str | None = None, *, session: Any = None) -> AgentResult:
        """Выполняет один запуск и превращает частичный сбой в результат."""
        hook = AuditHook(session=session)
        errors: list[str] = []
        result: Any = None
        try:
            async with asyncio.timeout(self._budget.remaining_seconds()) as deadline_scope:
                result = await self._bot.run(
                    prompt or self.context.query,
                    session_key=f"loophole:{self.context.workspace_id}:{self.context.run_id}",
                    channel="loophole",
                    hooks=[hook, _BudgetHook(self)],
                )
            errors.extend(hook.tool_errors)
            answer = redact_stream_text(hook.final_answer or getattr(result, "content", "") or "")
        except TimeoutError:
            if deadline_scope.expired() or self._budget.expired:
                self._budget.stop_reason = "time_budget"
                self._finish_budget_stop(hook)
                errors.extend(hook.tool_errors)
                answer = hook.final_answer
            else:
                errors.extend((*hook.tool_errors, "agent_error"))
                answer = redact_stream_text(hook.final_answer)
        except asyncio.CancelledError:
            if not self._budget.stop_reason or asyncio.current_task().cancelling():
                self._budget.cancelled = True
                raise
            self._finish_budget_stop(hook)
            errors.extend(hook.tool_errors)
            answer = hook.final_answer
        except Exception:  # noqa: BLE001 — внешний harness не раскрывается пользователю
            errors.extend(hook.tool_errors)
            if "agent_error" not in errors:
                errors.append("agent_error")
            answer = redact_stream_text(hook.final_answer)
        finally:
            self._budget.cancelled = True
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
        protocol_failed = not hook.validate_answer(answer)
        if protocol_failed:
            errors.append(MODEL_PROTOCOL_ERROR)
            answer = AGENT_UNAVAILABLE_MESSAGE
            stop_reason = MODEL_PROTOCOL_ERROR
            hook.records = []
        errors_tuple = tuple(dict.fromkeys(errors))
        final = (
            answer if protocol_failed
            else _public_partial_answer(answer, errors_tuple, iterations=int(iterations or 0))
        )
        self.last_result = AgentResult(
            answer=final,
            tools_used=tuple(dict.fromkeys(hook.tools_used)),
            errors=errors_tuple,
            partial=bool(errors_tuple) and not protocol_failed,
            iterations=int(iterations or 0),
            run_id=self.context.run_id,
            records=tuple(hook.records),
            sources=tuple(
                source for source in self.context.fetched_sources.values()
                if not protocol_failed and (
                    self._budget.stop_reason != "time_budget"
                    or source.get("url") in {record.get("url") for record in hook.records}
                )
            ),
            stop_reason=stop_reason,
        )
        return self.last_result

    async def stream(self, prompt: str, *, hook: AuditHook) -> Any:
        """Стримит события nanobot и закрывает ресурсы запуска."""
        iterator = self._bot.stream(
            prompt,
            session_key=f"loophole:{self.context.workspace_id}:{self.context.run_id}",
            channel="loophole",
            hooks=[hook, _BudgetHook(self)],
        )
        pending = None
        cleanup_task = None
        finished = False

        async def close_runner() -> None:
            try:
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                close_stream = getattr(iterator, "aclose", None)
                if callable(close_stream):
                    await close_stream()
            finally:
                await self.aclose()

        def begin_cleanup() -> asyncio.Task:
            nonlocal cleanup_task
            self._budget.cancelled = True
            if cleanup_task is None:
                cleanup_task = asyncio.create_task(close_runner())
            return cleanup_task

        async def expire() -> None:
            # Watchdog живёт независимо от consumer: SSE backpressure не продлевает бюджет.
            await asyncio.sleep(self._budget.remaining_seconds())
            if finished:
                return
            if not self._budget.stop_reason:
                self._budget.stop_reason = "time_budget"
            try:
                await self._wait_cleanup(begin_cleanup())
            except TimeoutError:
                hook.tool_errors.append("cleanup_timeout")
            finally:
                self._finish_budget_stop(hook)

        watchdog = asyncio.create_task(expire())
        next_progress = time.monotonic()
        try:
            while True:
                self._check_limits(check_findings=False)
                now = time.monotonic()
                if now >= next_progress:
                    yield SimpleNamespace(type="run.progress", metadata={
                        "phase": "execute", "stage": self._budget.phase,
                        "elapsed_seconds": self._budget.elapsed_seconds,
                        "message": (
                            "Проверка источников" if self._budget.phase == "research_tools"
                            else "Ожидание ответа модели"
                        ),
                    })
                    next_progress = now + _PROGRESS_INTERVAL_SECONDS
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait(
                    (pending,),
                    timeout=min(self._budget.remaining_seconds(), max(0, next_progress - now)),
                )
                if not done:
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    finished = True
                    break
                finally:
                    pending = None
                yield event
        except asyncio.CancelledError:
            if not self._budget.stop_reason or asyncio.current_task().cancelling():
                self._budget.cancelled = True
                raise
        finally:
            # Сначала runner/stream: bot.aclose() в nanobot закрывает только MCP/transport.
            finished = True
            watchdog.cancel()
            try:
                await self._wait_cleanup(begin_cleanup())
            except TimeoutError:
                if "cleanup_timeout" not in hook.tool_errors:
                    hook.tool_errors.append("cleanup_timeout")
                log.warning("loophole_agent_cleanup_timeout", run_id=self.context.run_id)
            finally:
                await asyncio.gather(watchdog, return_exceptions=True)
            if self._budget.stop_reason:
                self._finish_budget_stop(hook)
            hook.records = list(self.context.pending_records)

    async def aclose(self) -> None:
        """Закрывает nanobot и удаляет временный конфиг."""
        bot, config_path = self._bot, self._config_path
        self._bot = None
        self._config_path = ""
        if bot is not None:
            self._set_phase("finished")
            log.info(
                "loophole_agent_completed", run_id=self.context.run_id, model=self._model,
                elapsed_seconds=self._budget.elapsed_seconds, stop_reason=self._budget.stop_reason,
                phase_duration_ms={
                    key: round(value * 1000) for key, value in self._phase_durations.items()
                },
            )
        close_task = asyncio.create_task(bot.aclose()) if bot is not None else None
        try:
            if close_task is not None:
                await self._wait_cleanup(close_task)
        finally:
            if config_path:
                Path(config_path).unlink(missing_ok=True)


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
        budget = context.budget or ResearchBudget(
            timeout_seconds=settings.agent_timeout_seconds,
            requested_count=requested_finding_count(context.query),
        )
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
                fetched_sources=context.fetched_sources,
                budget=budget,
            ),
        )
        log.info(
            "loophole_agent_started", run_id=run_id,
            model=llm or settings.effective_nanobot_model(),
            timeout_seconds=budget.timeout_seconds, requested_count=budget.requested_count,
        )
        return ManagedAgent(
            AgentRunContext(
                user_id=context.user_id,
                workspace_id=context.workspace_id,
                query=context.query,
                run_id=run_id,
                max_iterations=context.max_iterations,
                pending_records=context.pending_records,
                fetched_sources=context.fetched_sources,
                budget=budget,
            ),
            bot,
            config_path,
            model=llm or settings.effective_nanobot_model(),
        )


__all__ = [
    "AGENT_TIME_BUDGET_MESSAGE",
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
