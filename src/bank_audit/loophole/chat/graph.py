"""Адаптер чата loophole на базе nanobot.

Сохраняет внешний контракт для `web.py`:
  - `run_chat(state, *, llm=None, session=None) -> ChatState`
  - `stream_chat(state, *, llm=None, session=None) -> AsyncIterator[dict]`

Внутри: nanobot-агент с кастомными tools (`audit_web_search`, `audit_db_query`, ...)
и lifecycle hook для сбора records/SSE-событий.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from .. import repository as repo
from ..agent import (
    AGENT_UNAVAILABLE_MESSAGE,
    AgentFactory,
    AgentResult,
    AgentRunContext,
    _safe_run_id,
)
from ..research_cases import ResearchCaseService
from . import clarify as clarify_mod
from .hooks import AuditHook, public_tool_name
from .nanobot_agent import build_prompt
from .state import ChatState

log = logging.getLogger(__name__)


class AgentAuditError(RuntimeError):
    """Обязательная запись аудита не выполнена безопасным образом."""


_SESSION_UNAVAILABLE_MESSAGE = "Исследование недоступно: отсутствует серверная сессия."
_CLARIFICATION_ASSEMBLY_MESSAGE = (
    "Не удалось подготовить исследование. Повторите отправку ответа."
)


def _normalized_run_id(value: Any, fallback: Any = None) -> str:
    """Возвращает безопасный slug для workspace и всех audit fallback-путей."""
    for candidate in (value, fallback, str(uuid4())):
        try:
            return _safe_run_id(candidate)
        except (TypeError, ValueError):
            continue
    raise RuntimeError("Не удалось сформировать безопасный run_id")


def _state_history(state: ChatState) -> list[dict[str, str]]:
    """Нормализует историю сообщений из state."""
    history = state.get("messages") or []
    out: list[dict[str, str]] = []
    for msg in history:
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
            out.append({
                "role": msg["role"],
                "content": clarify_mod._mask_for_llm(msg.get("content", "")),
            })
    return out


async def _run_nanobot(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> AgentResult:
    """Запускает отдельный managed agent."""
    query = clarify_mod._mask_for_llm(state.get("query", ""))
    history = _state_history(state)
    prompt = build_prompt(query, history)
    run_id = _normalized_run_id(state.get("run_id"))
    context = AgentRunContext(
        user_id=state.get("user_id") or "unknown",
        workspace_id=state.get("workspace_id"),
        query=query,
        run_id=run_id,
    )
    agent = AgentFactory().create(context, llm=llm, session=session)
    return await agent.run(prompt, session=session)


def _save_agent_audit(
    state: ChatState,
    result: AgentResult,
    *,
    started_at: float,
    session: Any,
) -> None:
    """Записывает redacted итог запуска, не сохраняя payload tools."""
    if session is None:
        raise AgentAuditError("Аудит запуска недоступен: отсутствует серверная сессия")
    run_id = _normalized_run_id(result.run_id, state.get("run_id"))
    try:
        repo.create_agent_audit(
            run_id=run_id,
            user_id=state.get("user_id") or "unknown",
            workspace_id=state.get("workspace_id"),
            query=state.get("query", ""),
            tools_used=list(result.tools_used),
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            result=result.answer,
            status="partial" if result.partial else "completed",
            error_code=(result.errors[0] if result.errors else None),
            session=session,
        )
    except Exception:  # noqa: BLE001 — audit boundary возвращает typed ошибку вызывающему
        log.warning("[run_chat] не удалось записать аудит managed agent")
        raise AgentAuditError("Аудит запуска недоступен") from None


def _persist_confirmed_findings(
    findings: list[dict],
    *,
    sources: list[dict] | None,
    workspace_id: int | None,
    run_id: str,
    query: str,
    session: Any,
) -> list[dict]:
    """Сохраняет находки только в изолированное исследование.

    Общий каталог намеренно не меняется: его пополняет только явный endpoint
    переноса предварительных источников аналитиком.
    """
    if session is None or not isinstance(workspace_id, int) or (not findings and not sources):
        return []
    try:
        persisted = ResearchCaseService(session).persist_managed_run(
            workspace_id=workspace_id,
            run_id=run_id,
            query=query,
            findings=findings,
            sources=sources,
        )
    except (AttributeError, KeyError, SQLAlchemyError, TypeError, ValueError):
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            rollback()
        log.warning("[research_persistence] пропущена некорректная находка")
        return []
    research_id = persisted["research_id"]
    candidate_urls = set(persisted.get("candidate_urls", ()))
    return [
        {**finding, "research_id": research_id, "status": "preliminary"}
        for finding in findings
        if finding.get("is_loophole") and str(finding.get("url") or "") in candidate_urls
    ]


async def run_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> ChatState:
    """Прогон чата через nanobot. Сохраняет контракт `run_chat` для `web.py`."""
    session = session if session is not None else state.get("session")
    state = {**state, "session": session}
    workspace_id = state.get("workspace_id")
    run_id = _normalized_run_id(state.get("run_id"))
    state = {**state, "run_id": run_id, "iterations": state.get("iterations", 0)}

    query = state.get("query", "")
    if state.get("clarification_verified") is True:
        enriched = query
    else:
        clarification = await clarify_mod.generate_clarifications(
            query,
            history=state.get("messages"),
        )
        if not clarification.get("complete"):
            questions = clarify_mod.clarification_questions(clarification, query=query)
            token = (
                clarify_mod.issue_clarification_token(
                    user_id=state.get("user_id") or "unknown",
                    workspace_id=workspace_id,
                    query=query,
                )
                if questions
                else None
            )
            return {
                **state,
                "phase": "await_clarify",
                "clarify_questions": questions,
                "clarification_token": token,
            }

        clarify_answers = state.get("clarify_answers") or []
        if clarify_answers:
            try:
                enriched = await clarify_mod.build_enriched_question(query, clarify_answers)
            except Exception:  # noqa: BLE001 - fail-closed boundary перед агентом
                log.warning("[run_chat] deterministic clarification assembly failed")
                return {
                    **state,
                    "phase": "error",
                    "error": "clarification_assembly_failed",
                    "answer": _CLARIFICATION_ASSEMBLY_MESSAGE,
                }
            if not isinstance(enriched, str) or not enriched.strip():
                return {
                    **state,
                    "phase": "error",
                    "error": "clarification_assembly_failed",
                    "answer": _CLARIFICATION_ASSEMBLY_MESSAGE,
                }
        else:
            enriched = query
    state = {**state, "query": enriched}
    if session is None:
        return {
            **state,
            "phase": "error",
            "error": "session_unavailable",
            "answer": _SESSION_UNAVAILABLE_MESSAGE,
        }

    started_at = time.perf_counter()
    try:
        result = await _run_nanobot(state, llm=llm, session=session)
    except asyncio.CancelledError:
        result = AgentResult(
            answer="Исследование прервано до завершения.",
            errors=("agent_cancelled",),
            partial=True,
            run_id=run_id,
        )
        try:
            _save_agent_audit(state, result, started_at=started_at, session=session)
        except AgentAuditError:
            log.warning("[run_chat] не удалось записать partial audit после отмены")
        raise
    except Exception:
        log.exception("[run_chat] nanobot failed")
        result = AgentResult(
            answer="Не удалось завершить исследование. Попробуйте повторить запрос.",
            errors=("agent_error",),
            partial=True,
            run_id=run_id,
        )
    try:
        _save_agent_audit(state, result, started_at=started_at, session=session)
    except AgentAuditError:
        audit_warning = "Исследование завершено частично: журнал аудита недоступен."
        answer = result.answer + chr(10) * 2 + audit_warning if result.answer else audit_warning
        result = AgentResult(
            answer=answer,
            tools_used=result.tools_used,
            errors=tuple(dict.fromkeys((*result.errors, "audit_unavailable"))),
            partial=True,
            iterations=result.iterations,
            run_id=_normalized_run_id(result.run_id, run_id),
            records=result.records,
            stop_reason=result.stop_reason,
        )
    answer = result.answer
    tools_used = list(result.tools_used)
    records = _persist_confirmed_findings(
        list(result.records),
        sources=None,
        workspace_id=workspace_id,
        run_id=_normalized_run_id(result.run_id, run_id),
        query=state["query"],
        session=session,
    )
    agent_unavailable = (
        result.stop_reason == "error"
        and "agent_error" in result.errors
        and not records
    )
    if agent_unavailable:
        answer = AGENT_UNAVAILABLE_MESSAGE

    # Сохраняем ответ в БД.
    if workspace_id and answer:
        try:
            repo.add_chat_message(workspace_id, "assistant", answer, session=session)
        except Exception:
            log.warning("[run_chat] failed to save assistant message", exc_info=True)

    return {
        **state,
        "answer": answer,
        "phase": "error" if agent_unavailable else "done",
        "error": "agent_unavailable" if agent_unavailable else None,
        "tools_used": tools_used,
        "records": records,
        "pending_table_records": records,
        "run_id": _normalized_run_id(result.run_id, run_id),
        "iterations": result.iterations,
    }


async def stream_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> AsyncIterator[dict]:
    """SSE-стриминг nanobot-чата. События: phase, question, tool_call, tool_result, token, records."""
    session = session if session is not None else state.get("session")
    state = {**state, "session": session}
    workspace_id = state.get("workspace_id")
    query = state.get("query", "")
    run_id = _normalized_run_id(state.get("run_id"))
    state = {**state, "run_id": run_id, "iterations": state.get("iterations", 0)}

    # Clarify — ТОЛЬКО на первом ходе. skip_clarify=True приходит с /chat после
    # /clarify/answer (сообщение уже обогащено ответами) → пропускаем гейт и идём
    # выполнять. Иначе generate_clarifications перезапускался бы на КАЖДЫЙ /chat,
    # и агент зацикливался на уточнениях.
    if state.get("clarification_verified") is True:
        enriched = query
    else:
        yield {"event": "phase", "data": {"phase": "clarify"}}
        clarification = await clarify_mod.generate_clarifications(
            query, history=state.get("messages")
        )
        if not clarification.get("complete"):
            yield {"event": "phase", "data": {"phase": "await_clarify"}}
            questions = clarify_mod.clarification_questions(clarification, query=query)
            if questions:
                token = clarify_mod.issue_clarification_token(
                    user_id=state.get("user_id") or "unknown",
                    workspace_id=workspace_id,
                    query=query,
                )
                yield {
                    "event": "question",
                    "data": {"questions": questions, "clarification_token": token},
                }
            return
        clarify_answers = state.get("clarify_answers") or []
        if not clarify_answers:
            enriched = query
        else:
            try:
                enriched = await clarify_mod.build_enriched_question(query, clarify_answers)
            except Exception:  # noqa: BLE001 - fail-closed boundary SSE
                log.warning("[stream_chat] deterministic clarification assembly failed")
                yield {
                    "event": "phase",
                    "data": {
                        "phase": "error",
                        "error": "clarification_assembly_failed",
                        "message": _CLARIFICATION_ASSEMBLY_MESSAGE,
                    },
                }
                return
            if not isinstance(enriched, str) or not enriched.strip():
                yield {
                    "event": "phase",
                    "data": {
                        "phase": "error",
                        "error": "clarification_assembly_failed",
                        "message": _CLARIFICATION_ASSEMBLY_MESSAGE,
                    },
                }
                return
    state = {**state, "query": enriched}
    if session is None:
        yield {
            "event": "phase",
            "data": {
                "phase": "error",
                "error": "session_unavailable",
                "message": _SESSION_UNAVAILABLE_MESSAGE,
            },
        }
        return

    yield {"event": "phase", "data": {"phase": "execute"}}

    safe_enriched = clarify_mod._mask_for_llm(enriched)
    prompt = build_prompt(safe_enriched, _state_history(state))
    context = AgentRunContext(
        user_id=state.get("user_id") or "unknown",
        workspace_id=workspace_id,
        query=safe_enriched,
        run_id=run_id,
    )
    agent = None
    hook = AuditHook(session=session)
    started_at = time.perf_counter()
    stream_failed = False
    factory_failed = False
    audit_attempted = False
    try:
        streamed_any = False
        try:
            agent = AgentFactory().create(context, llm=llm, session=session)
            async for event in agent.stream(prompt, hook=hook):
                mapped = _map_event(event, hook)
                if mapped:
                    if mapped.get("event") == "token" and mapped.get("data"):
                        streamed_any = True
                    yield mapped
        except Exception:  # noqa: BLE001 — factory/stream завершаем безопасно
            if agent is None:
                factory_failed = True
                log.warning("[stream_chat] AgentFactory завершился ошибкой")
            else:
                stream_failed = True
                log.warning("[stream_chat] managed agent прерван — возвращаем partial")

        flush_stream = getattr(hook, "flush_stream_for_sse", None)
        if callable(flush_stream):
            tail = flush_stream()
            if tail:
                streamed_any = True
                yield {"event": "token", "data": tail}

        answer = hook.final_answer or ""
        errors = list(hook.tool_errors)
        provider_failed = hook.stop_reason == "error"
        if provider_failed:
            answer = ""
            if "agent_error" not in errors:
                errors.append("agent_error")
        if hook.stop_reason == "max_iterations" and "max_iterations" not in errors:
            errors.append("max_iterations")
        if factory_failed and "agent_error" not in errors:
            errors.append("agent_error")
        if stream_failed and "agent_stream_error" not in errors:
            errors.append("agent_stream_error")
        records = []
        if not errors:
            records = _persist_confirmed_findings(
                context.pending_records,
                sources=list({
                    str(source.get("url")): source
                    for source in context.fetched_sources.values()
                    if isinstance(source, dict) and source.get("url")
                }.values()),
                workspace_id=workspace_id,
                run_id=run_id,
                query=state["query"],
                session=session,
            )
        if not records and not errors:
            records = hook.records
        terminal_provider_error = provider_failed and not streamed_any and not records
        partial_explanation = ""
        if terminal_provider_error:
            answer = AGENT_UNAVAILABLE_MESSAGE
        elif errors:
            partial_explanation = (
                "Исследование завершено частично: достигнут лимит итераций."
                if "max_iterations" in errors
                else "Исследование завершено частично: выполнение остановлено безопасно."
            )
            answer = answer + chr(10) * 2 + partial_explanation if answer else partial_explanation
        stream_result = AgentResult(
            answer=answer,
            tools_used=tuple(dict.fromkeys(hook.tools_used)),
            errors=tuple(errors),
            partial=bool(errors),
            run_id=run_id,
            records=tuple(records),
            iterations=hook.iterations,
            stop_reason=hook.stop_reason,
        )
        audit_attempted = True
        try:
            _save_agent_audit(state, stream_result, started_at=started_at, session=session)
        except AgentAuditError:
            audit_warning = "Исследование завершено частично: журнал аудита недоступен."
            answer = (
                stream_result.answer + chr(10) * 2 + audit_warning
                if stream_result.answer
                else audit_warning
            )
            stream_result = AgentResult(
                answer=answer,
                tools_used=stream_result.tools_used,
                errors=tuple(dict.fromkeys((*stream_result.errors, "audit_unavailable"))),
                partial=True,
                iterations=stream_result.iterations,
                run_id=stream_result.run_id,
                records=stream_result.records,
                stop_reason=stream_result.stop_reason,
            )
            answer = stream_result.answer
            errors = list(stream_result.errors)
            if not partial_explanation:
                partial_explanation = audit_warning

        if records:
            yield {"event": "records", "data": records}
        if terminal_provider_error:
            yield {
                "event": "phase",
                "data": {
                    "phase": "error",
                    "error": "agent_unavailable",
                    "message": AGENT_UNAVAILABLE_MESSAGE,
                },
            }
            return
        yield {
            "event": "phase",
            "data": {"phase": "answer", "partial": bool(errors)},
        }
        # Полный ответ дошлём ТОЛЬКО если модель не стримила дельты. Иначе токен
        # с полным текстом дублирует уже показанный поток (или плодит пустой
        # пузырь при смене фазы) — это и был «(пустой ответ)».
        if answer and not streamed_any:
            yield {"event": "token", "data": answer}
        elif streamed_any and partial_explanation:
            yield {"event": "partial", "data": {"message": partial_explanation}}

        # Сохраняем ответ.
        if workspace_id and answer:
            try:
                repo.add_chat_message(workspace_id, "assistant", answer, session=session)
            except Exception:
                log.warning("[stream_chat] failed to save assistant message", exc_info=True)
    finally:
        if not audit_attempted:
            cancellation_errors = list(hook.tool_errors)
            if "agent_cancelled" not in cancellation_errors:
                cancellation_errors.append("agent_cancelled")
            cancellation_result = AgentResult(
                answer=hook.final_answer or "",
                tools_used=tuple(dict.fromkeys(hook.tools_used)),
                errors=tuple(cancellation_errors),
                partial=True,
                run_id=run_id,
                records=tuple(hook.records),
                iterations=hook.iterations,
                stop_reason=hook.stop_reason,
            )
            try:
                _save_agent_audit(
                    state,
                    cancellation_result,
                    started_at=started_at,
                    session=session,
                )
            except AgentAuditError:
                log.warning("[stream_chat] не удалось записать partial audit после отмены")
        if agent is not None:
            try:
                await agent.aclose()
            except Exception:  # noqa: BLE001 — cleanup не должен ронять безопасный SSE
                log.warning("[stream_chat] cleanup managed agent завершился ошибкой")


def _record_stream_event(
    hook: Any,
    event_type: str,
    *,
    name: Any = None,
    error: Any = None,
) -> str:
    """Записывает событие в hook при наличии recorder и возвращает safe-имя."""
    recorder = getattr(hook, "record_stream_event", None)
    if callable(recorder):
        kwargs = {"name": name}
        if error is not None:
            kwargs["error"] = error
        recorder(event_type, **kwargs)
    return public_tool_name(name)


def _map_event(event: Any, hook: Any) -> dict | None:
    """Маппит nanobot StreamEvent на SSE-события loophole."""
    from nanobot.sdk.types import (
        STREAM_EVENT_RUN_FAILED,
        STREAM_EVENT_TEXT_DELTA,
        STREAM_EVENT_TOOL_COMPLETED,
        STREAM_EVENT_TOOL_FAILED,
        STREAM_EVENT_TOOL_STARTED,
    )

    ev_type = getattr(event, "type", None)
    if ev_type == STREAM_EVENT_TEXT_DELTA:
        # Текстовые дельты только накапливаются в hook. Полный redacted answer
        # публикуется после завершения stream через flush_stream_for_sse.
        delta = getattr(event, "delta", "") or ""
        buffer_delta = getattr(hook, "stream_delta_for_sse", None)
        if callable(buffer_delta):
            buffer_delta(delta)
        return None
    if ev_type == STREAM_EVENT_TOOL_STARTED:
        name = _record_stream_event(
            hook, "tool.started", name=getattr(event, "name", None)
        )
        return {"event": "tool_call", "data": {"name": name}}
    if ev_type == STREAM_EVENT_TOOL_COMPLETED:
        name = _record_stream_event(
            hook, "tool.completed", name=getattr(event, "name", None)
        )
        return {
            "event": "tool_result",
            "data": {"name": name, "status": "completed"},
        }
    if ev_type == STREAM_EVENT_TOOL_FAILED:
        name = _record_stream_event(
            hook,
            "tool.failed",
            name=getattr(event, "name", None),
            error=getattr(event, "error", None),
        )
        return {
            "event": "tool_result",
            "data": {"name": name, "status": "failed"},
        }
    if ev_type == STREAM_EVENT_RUN_FAILED:
        _record_stream_event(
            hook,
            "run.failed",
            error=getattr(event, "error", None),
        )
        return {"event": "token", "data": "Не удалось завершить исследование."}
    return None


# Legacy compat: graph compile оставлен для старых импортов, но ReAct фазы удалены.
# web.py использует только stream_chat/run_chat.
def build_graph():
    """Возвращает None — ReAct-граф заменён на nanobot."""
    return
