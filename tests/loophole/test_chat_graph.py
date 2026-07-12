"""Тест chat graph: мок tools + LLM → состояние графа и tool-calls /web_search, /web_fetch."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bank_audit.loophole.chat import graph as chat_graph
from bank_audit.loophole.chat import nodes
from bank_audit.loophole.chat import tools as chat_tools
from bank_audit.loophole.chat.state import ChatState
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.hashing import sha256_text


from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


@pytest.fixture
def session(sqlite_session):
    return sqlite_session


def _llm_mock(answer="Ответ аудитора."):
    msg = MagicMock()
    msg.content = answer
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


@pytest.mark.asyncio
async def test_run_chat_plain_answer(session):
    # Обычный запрос идёт через фазовый ReAct-граф (clarify→plan→…→answer).
    state: ChatState = {"query": "какие лазейки у Сбербанка?", "messages": []}
    result = await chat_graph.run_chat(state, llm=_llm_mock("Ответ."), session=session)
    assert result.get("phase") == "done"
    assert result["answer"]


@pytest.mark.asyncio
async def test_run_chat_web_search_command(session):
    state: ChatState = {"query": "/web_search скрытая комиссия сбербанк", "messages": []}
    # Подменяем search_impl через monkeypatch web_search в tools.
    import bank_audit.loophole.chat.tools as t
    orig = t.web_search
    t.web_search = lambda query, **kw: [{"title": "результат", "url": "http://x", "snippet": "s", "domain": "x"}]
    try:
        result = await chat_graph.run_chat(state, llm=_llm_mock(), session=session)
    finally:
        t.web_search = orig
    assert result["tool_results"]
    assert result["tool_results"][0]["name"] == "/web_search"
    assert result["tool_results"][0]["result"][0]["title"] == "результат"


@pytest.mark.asyncio
async def test_run_chat_web_fetch_command(session):
    state: ChatState = {"query": "/web_fetch https://example.ru/doc", "messages": []}
    import bank_audit.loophole.chat.tools as t
    orig = t.web_fetch
    t.web_fetch = lambda url, **kw: {"url": url, "title": "документ", "excerpt": "текст"}
    try:
        result = await chat_graph.run_chat(state, llm=_llm_mock(), session=session)
    finally:
        t.web_fetch = orig
    assert result["tool_results"][0]["name"] == "/web_fetch"
    assert result["tool_results"][0]["result"]["title"] == "документ"


@pytest.mark.asyncio
async def test_retrieve_node_finds_records(session):
    rec = LoopholeRecord(sha256=sha256_text("a"), title="лазейка сбербанк",
                        snippet="скрытая комиссия", bank_slug="sberbank", raw_text="комиссия")
    rid = repo.insert_record(rec, session=session)
    repo.update_verdict(rid, is_loophole=True, confidence=0.9, reason="ок", model="m", session=session)
    state: ChatState = {"query": "лазейка", "bank_slugs": ["sberbank"], "session": session}
    out = nodes.retrieve_node(state)
    assert len(out["records"]) >= 1


def test_parse_tool_calls():
    assert nodes._parse_tool_calls("/web_search тест") == [
        {"name": "/web_search", "args": {"query": "тест"}}
    ]
    assert nodes._parse_tool_calls("/web_fetch http://x") == [
        {"name": "/web_fetch", "args": {"url": "http://x"}}
    ]
    assert nodes._parse_tool_calls("обычный вопрос") == []


def test_dispatch_unknown_command():
    res = chat_tools.dispatch("/unknown", {})
    assert "error" in res


@pytest.mark.asyncio
async def test_stream_chat_emits_events(session):
    state: ChatState = {"query": "вопрос", "messages": []}
    events = []
    async for ev in chat_graph.stream_chat(state, llm=_llm_mock("ответ"), session=session):
        events.append(ev)
    # Фазовый граф эмитит phase-события и финальный token.
    assert any(e["event"] == "phase" for e in events)
    assert any(e["event"] == "token" for e in events)


def test_build_graph_returns_compiled():
    """langgraph установлен — build_graph возвращает скомпилированный граф."""
    g = chat_graph.build_graph()
    assert g is not None
