"""Адаптер чата loophole на базе nanobot.

Сохраняет внешний контракт для `web.py`:
  - `run_chat(state, *, llm=None, session=None) -> ChatState`
  - `stream_chat(state, *, llm=None, session=None) -> AsyncIterator[dict]`

Внутри: nanobot-агент с кастомными tools (`audit_web_search`, `audit_db_query`, ...)
и lifecycle hook для сбора records/SSE-событий.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from .hooks import AuditHook
from .nanobot_agent import build_prompt, create_nanobot
from .state import ChatState
from .. import repository as repo
from . import clarify as clarify_mod

log = logging.getLogger(__name__)


def _state_history(state: ChatState) -> list[dict[str, str]]:
    """Нормализует историю сообщений из state."""
    history = state.get("messages") or []
    out: list[dict[str, str]] = []
    for msg in history:
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
            out.append({"role": msg["role"], "content": str(msg.get("content", ""))})
    return out


async def _run_nanobot(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> tuple[str, list[str], list[dict]]:
    """Запускает nanobot, возвращает (answer, tools_used, records)."""
    query = state.get("query", "")
    workspace_id = state.get("workspace_id")
    history = _state_history(state)
    prompt = build_prompt(query, history)

    bot, config_path = create_nanobot(model=llm)
    try:
        session_key = f"loophole:{workspace_id}:{state.get('task_id', 'default')}"
        hook = AuditHook(session=session)
        result = await bot.run(
            prompt,
            session_key=session_key,
            channel="loophole",
            hooks=[hook],
        )
        content = result.content or ""
        return content, hook.tools_used, hook.records
    finally:
        await bot.aclose()
        from pathlib import Path

        Path(config_path).unlink(missing_ok=True)


async def run_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> ChatState:
    """Прогон чата через nanobot. Сохраняет контракт `run_chat` для `web.py`."""
    state = {**state, "session": session if session is not None else state.get("session")}
    workspace_id = state.get("workspace_id")

    # Clarify-воронка.
    clarification = await clarify_mod.generate_clarifications(
        state.get("query", ""),
        history=state.get("messages"),
    )
    if not clarification.get("complete"):
        return {
            **state,
            "phase": "await_clarify",
            "clarify_questions": clarification.get("questions", []),
        }

    enriched = await clarify_mod.build_enriched_question(
        state.get("query", ""), state.get("clarify_answers", [])
    )
    state = {**state, "query": enriched}

    try:
        answer, tools_used, records = await _run_nanobot(state, llm=llm, session=session)
    except Exception as e:
        log.exception("[run_chat] nanobot failed")
        answer = f"Ошибка при обработке запроса: {e}"
        tools_used = []
        records = []

    # Сохраняем ответ в БД.
    if workspace_id and answer:
        try:
            repo.add_chat_message(workspace_id, "assistant", answer, session=session)
        except Exception:
            log.warning("[run_chat] failed to save assistant message", exc_info=True)

    return {
        **state,
        "answer": answer,
        "phase": "done",
        "tools_used": tools_used,
        "records": records,
        "pending_table_records": records,
    }


async def stream_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> AsyncIterator[dict]:
    """SSE-стриминг nanobot-чата. События: phase, question, tool_call, tool_result, token, records."""
    state = {**state, "session": session if session is not None else state.get("session")}
    workspace_id = state.get("workspace_id")
    query = state.get("query", "")

    # Clarify — ТОЛЬКО на первом ходе. skip_clarify=True приходит с /chat после
    # /clarify/answer (сообщение уже обогащено ответами) → пропускаем гейт и идём
    # выполнять. Иначе generate_clarifications перезапускался бы на КАЖДЫЙ /chat,
    # и агент зацикливался на уточнениях.
    if state.get("skip_clarify"):
        enriched = query
    else:
        yield {"event": "phase", "data": {"phase": "clarify"}}
        clarification = await clarify_mod.generate_clarifications(
            query, history=state.get("messages")
        )
        if not clarification.get("complete"):
            yield {"event": "phase", "data": {"phase": "await_clarify"}}
            for q in clarification.get("questions", []):
                yield {"event": "question", "data": q}
            return
        enriched = await clarify_mod.build_enriched_question(
            query, state.get("clarify_answers", [])
        )
    state = {**state, "query": enriched}

    yield {"event": "phase", "data": {"phase": "execute"}}

    prompt = build_prompt(enriched, _state_history(state))
    bot, config_path = create_nanobot(model=llm)
    try:
        session_key = f"loophole:{workspace_id}:{state.get('task_id', 'default')}"
        hook = AuditHook(session=session)

        streamed_any = False
        async for event in bot.stream(prompt, session_key=session_key, hooks=[hook]):
            mapped = _map_event(event, hook)
            if mapped:
                if mapped.get("event") == "token" and mapped.get("data"):
                    streamed_any = True
                yield mapped

        answer = hook.final_answer or ""
        records = hook.records

        if records:
            yield {"event": "records", "data": records}
        yield {"event": "phase", "data": {"phase": "answer"}}
        # Полный ответ дошлём ТОЛЬКО если модель не стримила дельты. Иначе токен
        # с полным текстом дублирует уже показанный поток (или плодит пустой
        # пузырь при смене фазы) — это и был «(пустой ответ)».
        if answer and not streamed_any:
            yield {"event": "token", "data": answer}

        # Сохраняем ответ.
        if workspace_id and answer:
            try:
                repo.add_chat_message(workspace_id, "assistant", answer, session=session)
            except Exception:
                log.warning("[stream_chat] failed to save assistant message", exc_info=True)
    finally:
        await bot.aclose()
        from pathlib import Path

        Path(config_path).unlink(missing_ok=True)


def _map_event(event: Any, hook: AuditHook) -> dict | None:
    """Маппит nanobot StreamEvent на SSE-события loophole."""
    from nanobot.sdk.types import (
        STREAM_EVENT_TEXT_DELTA,
        STREAM_EVENT_TOOL_STARTED,
        STREAM_EVENT_TOOL_COMPLETED,
        STREAM_EVENT_TOOL_FAILED,
        STREAM_EVENT_RUN_FAILED,
    )

    ev_type = getattr(event, "type", None)
    if ev_type == STREAM_EVENT_TEXT_DELTA:
        return {"event": "token", "data": getattr(event, "content", "")}
    if ev_type == STREAM_EVENT_TOOL_STARTED:
        return {
            "event": "tool_call",
            "data": {
                "name": getattr(event, "metadata", {}).get("tool_name"),
                "args": getattr(event, "metadata", {}).get("tool_args"),
            },
        }
    if ev_type == STREAM_EVENT_TOOL_COMPLETED:
        return {
            "event": "tool_result",
            "data": {
                "name": getattr(event, "metadata", {}).get("tool_name"),
                "result": getattr(event, "content", ""),
            },
        }
    if ev_type == STREAM_EVENT_TOOL_FAILED:
        return {
            "event": "tool_result",
            "data": {
                "name": getattr(event, "metadata", {}).get("tool_name"),
                "error": getattr(event, "error", "tool failed"),
            },
        }
    if ev_type == STREAM_EVENT_RUN_FAILED:
        return {"event": "token", "data": f"Ошибка: {getattr(event, 'error', 'unknown')}"}
    return None


# Legacy compat: graph compile оставлен для старых импортов, но ReAct фазы удалены.
# web.py использует только stream_chat/run_chat.
def build_graph():
    """Возвращает None — ReAct-граф заменён на nanobot."""
    return None
