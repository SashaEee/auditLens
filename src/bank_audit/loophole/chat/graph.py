"""langgraph StateGraph чата loophole: ReAct-архитектура с фазами.

Фазы: clarify → plan → execute → aggregate → answer → done.
Entry point: clarify. Conditional edges по полю ``state["phase"]``.

Стриминг — через async generator (используется в web.py для SSE).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from . import nodes
from . import phases
from .state import ChatState

log = logging.getLogger(__name__)


def _route_after_clarify(state: ChatState) -> str:
    phase = state.get("phase")
    if phase == "await_clarify":
        return "__end__"
    return "plan"


def _route_after_execute(state: ChatState) -> str:
    phase = state.get("phase")
    if phase == "execute":
        return "execute"
    return "aggregate"


def build_graph():
    """Создаёт langgraph StateGraph фаз. Возвращает скомпилированный граф.

    Если langgraph недоступен — возвращает None (тесты используют run_chat).
    """
    try:
        from langgraph.graph import StateGraph, END
    except Exception:
        return None

    g = StateGraph(ChatState)
    g.add_node("clarify", phases.clarify_node)
    g.add_node("plan", phases.plan_node)
    g.add_node("execute", phases.execute_node)
    g.add_node("aggregate", phases.aggregate_node)
    g.add_node("answer", phases.answer_node)
    g.set_entry_point("clarify")
    g.add_conditional_edges(
        "clarify",
        _route_after_clarify,
        {"plan": "plan", END: END},
    )
    g.add_edge("plan", "execute")
    g.add_conditional_edges(
        "execute",
        _route_after_execute,
        {"execute": "execute", "aggregate": "aggregate"},
    )
    g.add_edge("aggregate", "answer")
    g.add_edge("answer", END)
    return g.compile()


# ── Fallback: последовательный прогон фаз ───────────────────────────────────
async def run_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> ChatState:
    """Прогон фаз без langgraph (последовательно). Fallback для тестов/проде.

    Сохраняет совместимость со старым контрактом: если в query есть /команда —
    прогоняет старый retrieve→llm→tools путь. Иначе — фазовый ReAct.
    """
    if _has_command(state.get("query", "")):
        return await _run_legacy(state, llm=llm, session=session)

    state = {**state, "session": session if session is not None else state.get("session")}
    state = await phases.clarify_node(state, llm=llm)
    if state.get("phase") == "await_clarify":
        return state
    state = await phases.plan_node(state, llm=llm)
    state = await phases.execute_node(state, llm=llm, session=session)
    while state.get("phase") == "execute":
        state = await phases.execute_node(state, llm=llm, session=session)
    state = await phases.aggregate_node(state, llm=llm)
    state = await phases.answer_node(state)
    return state


# ── Совместимость со старыми helpers ─────────────────────────────────────────
def _strip_command(query: str) -> str:
    """Убирает /команду из начала query."""
    stripped = (query or "").strip()
    for cmd in (
        "/web_search", "/web_fetch", "/retrieve", "/export",
        "/keywords", "/extract_loopholes", "/db_query", "/table_load",
        "/parser_create", "/parser_start", "/parser_stop", "/parser_status",
    ):
        if stripped.startswith(cmd):
            return stripped[len(cmd):].strip()
    return stripped


def _has_command(query: str) -> bool:
    stripped = (query or "").strip()
    return stripped.startswith("/") and " " in stripped


def _format_tool_results(tool_results: list[dict]) -> str:
    """Текстовая сводка результатов tools для SSE без LLM."""
    parts: list[str] = []
    for tr in tool_results or []:
        name = tr.get("name", "")
        result = tr.get("result")
        if isinstance(result, dict) and "error" in result:
            parts.append(f"{name}: {result['error']}")
            continue
        if name == "/web_search":
            items = result if isinstance(result, list) else []
            if not items:
                parts.append("Веб-поиск не дал результатов.")
            else:
                lines = [f"Результаты веб-поиска ({len(items)}):"]
                for i, r in enumerate(items[:8], 1):
                    lines.append(f"{i}. {r.get('title') or '—'}")
                    if r.get("url"):
                        lines.append(f"   {r['url']}")
                    if r.get("snippet"):
                        lines.append(f"   {str(r['snippet'])[:300]}")
                parts.append("\n".join(lines))
        elif name == "/web_fetch" and isinstance(result, dict):
            parts.append(f"Загружено: {result.get('title') or result.get('url') or '—'}")
            if result.get("excerpt"):
                parts.append(str(result["excerpt"])[:1500])
        elif name == "/retrieve":
            items = result if isinstance(result, list) else []
            parts.append(f"Найдено записей в БД: {len(items)}")
        elif name == "/export":
            parts.append("Экспорт подготовлен.")
        else:
            parts.append(f"{name}: {result}")
    return "\n\n".join(parts)


async def _finalize_after_tools(state: ChatState, *, llm: Any = None) -> ChatState:
    """Формирует итоговый answer после выполнения tools (legacy-путь)."""
    if state.get("answer"):
        return state
    if llm is not None:
        state = {**state, "query": _strip_command(state.get("query", ""))}
        return await nodes.llm_node(state, llm=llm)
    answer = _format_tool_results(state.get("tool_results") or [])
    return {**state, "answer": answer}


async def _run_legacy(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> ChatState:
    """Старый путь retrieve→llm→tools для /команд."""
    state = nodes.retrieve_node({**state, "session": session})
    state = await nodes.llm_node(state, llm=llm)
    if state.get("tool_calls"):
        state = await asyncio.to_thread(nodes.tools_node, state, session=session)
        state = {**state, "query": _strip_command(state.get("query", ""))}
        state = await _finalize_after_tools(state, llm=llm)
    return state


# ── SSE-стриминг фаз ─────────────────────────────────────────────────────────
async def stream_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> AsyncIterator[dict]:
    """SSE-генератор фазового ReAct-графа.

    Эмитит события: ``phase`` (при смене фазы), ``question`` (clarify questions),
    ``subtask`` (старт/завершение), ``tool_call``/``tool_result``, ``records``
    (pending_table_records), ``token`` (финальный answer).
    """
    state = {**state, "session": session if session is not None else state.get("session")}
    workspace_id = state.get("workspace_id")
    sess = state.get("session")
    prev_phase: str | None = None

    def _phase_event(phase: str) -> dict | None:
        nonlocal prev_phase
        if phase == prev_phase:
            return None
        prev_phase = phase
        return {"event": "phase", "data": {"phase": phase}}

    # ── clarify ──────────────────────────────────────────────────────────────
    state = await phases.clarify_node(state, llm=llm)
    ev = _phase_event("clarify")
    if ev:
        yield ev
    if state.get("phase") == "await_clarify":
        questions = state.get("clarify_questions") or []
        for q in questions:
            yield {"event": "question", "data": q}
        return

    # ── plan ─────────────────────────────────────────────────────────────────
    state = await phases.plan_node(state, llm=llm)
    ev = _phase_event("plan")
    if ev:
        yield ev
    for st in state.get("subtasks") or []:
        yield {"event": "subtask", "data": {"status": "start", "subtask": st}}

    # ── execute ──────────────────────────────────────────────────────────────
    ev = _phase_event("execute")
    if ev:
        yield ev
    state = await phases.execute_node(state, llm=llm, session=sess)
    for sr in state.get("subtask_results") or []:
        yield {"event": "subtask", "data": {"status": "done", "result": sr}}
        for obs in (sr.get("observations") or []) if isinstance(sr, dict) else []:
            yield {"event": "tool_call", "data": {"name": obs.get("action"), "args": obs.get("args")}}
            yield {"event": "tool_result", "data": {"name": obs.get("action"), "result": obs.get("result")}}

    # ── aggregate ────────────────────────────────────────────────────────────
    state = await phases.aggregate_node(state, llm=llm)
    ev = _phase_event("aggregate")
    if ev:
        yield ev
    records = state.get("pending_table_records") or []
    if records:
        yield {"event": "records", "data": records}

    # ── answer ───────────────────────────────────────────────────────────────
    state = await phases.answer_node(state)
    ev = _phase_event("answer")
    if ev:
        yield ev
    answer = state.get("answer") or ""
    if answer:
        yield {"event": "token", "data": answer}
    # Сохраняем ответ в БД.
    if workspace_id and answer:
        try:
            repo_add_chat_message(workspace_id, "assistant", answer, session=sess)
        except Exception:
            pass


def repo_add_chat_message(workspace_id: int, role: str, content: str, *, session=None) -> None:
    """Обёртка для тестов-моков (чтобы не импортировать repository на верху)."""
    from .. import repository as _repo

    _repo.add_chat_message(workspace_id, role, content, session=session)
