"""Тест chat/phases.py: прогон графа по фазам clarify→plan→execute→aggregate→answer.

Мок LLM (возвращает предзаготовленный JSON), мок tools/dispatch, мок session.
Без сети/реальной БД.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from bank_audit.loophole.chat import phases
from bank_audit.loophole.chat import tools as chat_tools
from bank_audit.loophole.chat.state import ChatState


# ── SQLite-схема (минимальная для agent_task) ────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE loophole_agent_task (
    task_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id     INTEGER,
    query_text       TEXT,
    enriched_query   TEXT,
    phase            TEXT DEFAULT 'clarify',
    status           TEXT DEFAULT 'running',
    subtasks         TEXT,
    subtask_results  TEXT,
    iterations       INTEGER DEFAULT 0,
    clarify_questions TEXT,
    clarify_answers  TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.connection.executescript(SCHEMA_SQL)
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    yield s
    s.close()


def _llm_mock(responses: list[str]):
    """Мок LLM, возвращающий responses по очереди."""
    msgs = [MagicMock(content=r) for r in responses]
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=msgs)
    return llm


# ── clarify_node ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_clarify_node_complete(monkeypatch):
    """complete=true → phase='plan'."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    async def fake_gen(question, history=None):
        return {"complete": True, "questions": [], "reason": "ok"}
    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {"query": "Проверь вклады Сбербанка 2025", "workspace_id": None}
    out = await phases.clarify_node(state)
    assert out["phase"] == "plan"
    assert out["clarify_questions"] == []


@pytest.mark.asyncio
async def test_clarify_node_await(monkeypatch, session):
    """complete=false → phase='await_clarify' + questions."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    questions = [{"id": "bank", "question": "Какой банк?", "type": "single",
                  "options": [{"value": "sber", "label": "Сбер"}], "allow_other": True}]
    async def fake_gen(question, history=None):
        return {"complete": False, "questions": questions, "reason": "не указан банк"}
    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {
        "query": "найди лазейки",
        "workspace_id": 1,
        "user_id": "u1",
        "session": session,
    }
    out = await phases.clarify_node(state)
    assert out["phase"] == "await_clarify"
    assert len(out["clarify_questions"]) == 1
    assert out["task_id"] is not None


# ── plan_node ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_plan_node_parses_subtasks():
    plan_json = json.dumps({
        "subtasks": [
            {"id": "gen_keywords", "title": "Ключевые слова",
             "algorithm": "1. Сгенерировать.", "tools": ["/keywords"]},
            {"id": "web_search", "title": "Веб-поиск",
             "algorithm": "1. Поиск.", "tools": ["/web_search"]},
        ]
    })
    llm = _llm_mock([plan_json])
    state: ChatState = {"query": "вклады сбербанк", "clarify_answers": []}
    out = await phases.plan_node(state, llm=llm)
    assert out["phase"] == "execute"
    assert len(out["subtasks"]) == 2
    assert out["subtasks"][0]["id"] == "gen_keywords"
    assert out["iterations"] == 0


@pytest.mark.asyncio
async def test_plan_node_empty_on_llm_failure():
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    state: ChatState = {"query": "вопрос", "clarify_answers": []}
    out = await phases.plan_node(state, llm=llm)
    assert out["phase"] == "execute"
    assert out["subtasks"] == []


# ── execute_node ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execute_node_runs_subtasks_in_parallel(monkeypatch):
    """execute_node запускает подзадачи через asyncio.gather (параллельно)."""
    order: list[str] = []

    async def fake_react(subtask, state, *, llm, session=None):
        order.append(subtask["id"])
        return {"id": subtask["id"], "title": subtask["title"],
                "observations": [], "final_answer": "ok", "iterations": 1}

    monkeypatch.setattr(phases, "_run_subtask_react", fake_react)

    subtasks = [
        {"id": "a", "title": "A", "algorithm": "", "tools": []},
        {"id": "b", "title": "B", "algorithm": "", "tools": []},
        {"id": "c", "title": "C", "algorithm": "", "tools": []},
    ]
    state: ChatState = {"subtasks": subtasks, "query": "q", "iterations": 0}
    out = await phases.execute_node(state, llm=MagicMock())
    assert out["phase"] == "aggregate"
    assert len(out["subtask_results"]) == 3
    assert set(order) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_execute_node_empty_subtasks():
    state: ChatState = {"subtasks": [], "iterations": 0}
    out = await phases.execute_node(state, llm=MagicMock())
    assert out["phase"] == "aggregate"
    assert out["subtask_results"] == []


@pytest.mark.asyncio
async def test_react_iteration_limit(monkeypatch):
    """ReAct-цикл останавливается после 10 итераций."""
    # LLM всегда отвечает Thought/Action без Final Answer → 10 итераций.
    llm = _llm_mock([
        "Thought: шаг\nAction: /web_search\nAction Input: {\"query\": \"test\"}"
    ] * 20)
    monkeypatch.setattr(
        chat_tools, "dispatch",
        lambda cmd, args, *, session=None: [{"title": "результат"}],
    )
    subtask = {"id": "x", "title": "X", "algorithm": "", "tools": ["/web_search"]}
    state: ChatState = {"query": "q"}
    result = await phases._run_subtask_react(subtask, state, llm=llm, session=None)
    assert result["iterations"] == phases._MAX_REACT_ITERATIONS
    assert "лимит" in result["final_answer"].lower()


@pytest.mark.asyncio
async def test_react_final_answer_terminates(monkeypatch):
    llm = _llm_mock([
        "Thought: готово\nFinal Answer: найдена лазейка X",
    ])
    subtask = {"id": "x", "title": "X", "algorithm": "", "tools": []}
    state: ChatState = {"query": "q"}
    result = await phases._run_subtask_react(subtask, state, llm=llm, session=None)
    assert result["final_answer"] == "найдена лазейка X"
    assert result["iterations"] == 0  # ни одного tool-вызова


# ── aggregate_node ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_aggregate_filters_is_loophole_and_masks_pii(monkeypatch):
    """aggregate оставляет только is_loophole=true и маскирует ПД."""
    subtask_results = [{
        "observations": [
            {
                "action": "/extract_loopholes",
                "args": {},
                "result": [
                    {
                        "title": "Скрытая комиссия для Иванова Ивана Ивановича",
                        "description": "Комиссия 100 руб., телефон +7 999 123 45 67",
                        "category": "Скрытые комиссии",
                        "severity": "high",
                        "evidence_quote": "Банк списал 100 руб. с карты 4111 1111 1111 1111",
                        "is_loophole": True,
                    },
                    {
                        "title": "Обычное условие",
                        "is_loophole": False,
                    },
                ],
            }
        ]
    }]
    state: ChatState = {"subtask_results": subtask_results}
    # LLM не вызываем (aggregate fallback).
    out = await phases.aggregate_node(state, llm=_llm_mock(["{}"]))
    records = out["records"]
    assert len(records) == 1  # только is_loophole=True
    # ПД замаскированы в текстовых полях.
    text = records[0]["evidence_quote"] + records[0]["description"]
    assert "4111 1111 1111 1111" not in text
    assert "+7 999 123 45 67" not in text
    assert out["phase"] == "answer"
    assert len(out["pending_table_records"]) == 1


# ── answer_node ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_answer_node_sets_done():
    state: ChatState = {"answer": "итоговый ответ", "records": []}
    out = await phases.answer_node(state)
    assert out["phase"] == "done"
    assert out["answer"] == "итоговый ответ"


@pytest.mark.asyncio
async def test_answer_node_generates_default_from_records():
    state: ChatState = {
        "answer": "",
        "records": [{"title": "Лазейка 1"}, {"title": "Лазейка 2"}],
    }
    out = await phases.answer_node(state)
    assert out["phase"] == "done"
    assert "2" in out["answer"]
    assert "Лазейка 1" in out["answer"]


# ── Полный прогон через run_chat ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_chat_full_phase_pipeline(monkeypatch, session):
    """Полный прогон clarify→plan→execute→aggregate→answer через run_chat."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat import graph as chat_graph

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": True, "questions": [], "reason": "ok"}
    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    plan_json = json.dumps({
        "subtasks": [{"id": "s1", "title": "поиск", "algorithm": "шаг", "tools": []}]
    })
    final_json = json.dumps({"summary": "найдено 0 лазеек", "table_records": []})

    # responses: plan, execute(react→final), aggregate
    llm = _llm_mock([
        plan_json,
        "Thought: готово\nFinal Answer: ничего не найдено",
        final_json,
    ])

    async def fake_react(subtask, state, *, llm, session=None):
        return {"id": subtask["id"], "title": subtask["title"],
                "observations": [], "final_answer": "ok", "iterations": 0}
    monkeypatch.setattr(phases, "_run_subtask_react", fake_react)

    state: ChatState = {
        "query": "проверь вклады сбербанка",
        "workspace_id": 1,
        "user_id": "u1",
        "session": session,
    }
    out = await chat_graph.run_chat(state, llm=llm, session=session)
    assert out["phase"] == "done"
    assert out["answer"]


# ── stream_chat ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stream_chat_emits_phase_events(monkeypatch, session):
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat import graph as chat_graph

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": True, "questions": [], "reason": "ok"}
    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    plan_json = json.dumps({"subtasks": []})
    final_json = json.dumps({"summary": "пусто", "table_records": []})
    llm = _llm_mock([plan_json, final_json])

    state: ChatState = {
        "query": "вопрос",
        "workspace_id": 1,
        "session": session,
    }
    events = []
    async for ev in chat_graph.stream_chat(state, llm=llm, session=session):
        events.append(ev)
    phases_emitted = [e["data"]["phase"] for e in events if e["event"] == "phase"]
    assert "clarify" in phases_emitted
    assert "answer" in phases_emitted
    assert any(e["event"] == "token" for e in events)
