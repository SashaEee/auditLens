"""Узлы графа чата loophole: retrieve, llm, tools, cond."""
from __future__ import annotations

import logging
from typing import Any

from . import tools as chat_tools
from .state import ChatState

log = logging.getLogger(__name__)


def retrieve_node(state: ChatState) -> ChatState:
    """Извлекает релевантные лазейки из БД по запросу."""
    query = state.get("query", "")
    bank_slugs = state.get("bank_slugs") or None
    session = state.get("session")
    try:
        records = chat_tools.retrieve_loopholes(
            query, bank_slugs=bank_slugs, session=session
        )
    except Exception as e:
        log.warning("[retrieve_node] failed: %s", e)
        records = []
    return {**state, "records": records}


async def llm_node(state: ChatState, *, llm: Any = None) -> ChatState:
    """LLM-узел: генерирует ответ или tool-calls.

    Если в query есть /команда — парсит в tool_call. Иначе — вызывает LLM для ответа.
    llm — инъекция (мок) или langchain ChatModel.
    """
    query = state.get("query", "")
    messages = state.get("messages", [])
    # Парсинг /команд.
    tool_calls = _parse_tool_calls(query)
    if tool_calls:
        return {**state, "tool_calls": tool_calls, "answer": ""}
    # Обычный ответ LLM.
    if llm is None:
        return {**state, "answer": "", "error": "no_llm"}
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        sys = SystemMessage(content="Ты — аудитор-аналитик, помогающий анализировать лазейки в банковских продуктах. Отвечай на русском.")
        hist = [HumanMessage(content=query)]
        resp = await llm.ainvoke([sys] + hist)
        answer = getattr(resp, "content", None) or str(resp)
    except Exception as e:
        log.warning("[llm_node] failed: %s", e)
        answer = ""
    return {**state, "answer": answer, "tool_calls": []}


def tools_node(state: ChatState, *, session=None) -> ChatState:
    """Выполняет tool_calls из state."""
    calls = state.get("tool_calls") or []
    results = []
    for call in calls:
        cmd = call.get("name", "")
        args = call.get("args", {})
        res = chat_tools.dispatch(cmd, args, session=session)
        results.append({"name": cmd, "args": args, "result": res})
    return {**state, "tool_results": results, "tool_calls": []}


def cond_node(state: ChatState) -> str:
    """Роутинг: если есть tool_calls → 'tools', иначе → 'end'."""
    if state.get("tool_calls"):
        return "tools"
    return "end"


def _parse_tool_calls(query: str) -> list[dict]:
    """Парсит /команды из текста запроса.

    Поддерживает: /web_search запрос, /web_fetch url, /retrieve запрос, /export.
    """
    calls = []
    if not query:
        return calls
    stripped = query.strip()
    for cmd in ("/web_search", "/web_fetch", "/retrieve", "/export"):
        if stripped.startswith(cmd):
            rest = stripped[len(cmd):].strip()
            if cmd == "/web_search":
                calls.append({"name": cmd, "args": {"query": rest}})
            elif cmd == "/web_fetch":
                calls.append({"name": cmd, "args": {"url": rest}})
            elif cmd == "/retrieve":
                calls.append({"name": cmd, "args": {"query": rest}})
            elif cmd == "/export":
                calls.append({"name": cmd, "args": {"format": rest or "json"}})
            break
    return calls
