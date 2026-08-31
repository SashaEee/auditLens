import pytest

from bank_audit.loophole.chat.graph import run_chat, stream_chat
from bank_audit.loophole.chat.state import ChatState


@pytest.mark.asyncio
async def test_run_chat_await_clarify(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": False, "questions": [{"id": "q1", "question": "Какой банк?"}]}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {"query": "найди лазейки", "workspace_id": 1, "user_id": "u1"}
    out = await run_chat(state)
    assert out["phase"] == "await_clarify"
    assert len(out["clarify_questions"]) == 1


@pytest.mark.asyncio
async def test_run_chat_complete_does_not_crash(monkeypatch, session):
    from bank_audit.loophole.agent import AgentResult
    from bank_audit.loophole.chat import clarify as clarify_mod
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    _create_research_schema(session)

    async def fake_gen(question, history=None):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    async def fake_run(state, *, llm=None, session=None):
        return AgentResult(answer="Исследование выполнено", run_id=state["run_id"])

    monkeypatch.setattr("bank_audit.loophole.chat.graph._run_nanobot", fake_run)

    state: ChatState = {"query": "сколько записей в базе", "workspace_id": 1, "user_id": "u1"}
    out = await run_chat(state, session=session)
    assert out["phase"] == "done"
    assert "answer" in out


@pytest.mark.asyncio
async def test_stream_chat_await_clarify(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": False, "questions": [{"id": "q1", "question": "Какой банк?"}]}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {"query": "найди лазейки", "workspace_id": 1, "user_id": "u1"}
    events = []
    async for ev in stream_chat(state):
        events.append(ev)
    assert any(
        e["event"] == "phase" and e["data"].get("phase") == "await_clarify" for e in events
    )


@pytest.mark.asyncio
async def test_stream_chat_saves_confirmed_findings_only_to_research_before_explicit_import(
    monkeypatch, session
):
    """Мутация: managed run не должен напрямую пополнять общий каталог."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context

        async def stream(self, _prompt, *, hook):
            self.context.fetched_sources["https://example.ru/source"] = {
                "url": "https://example.ru/source",
                "title": "Источник",
                "extracted_text": "Текст источника",
                "published_at": None,
            }
            self.context.pending_records.append(
                {
                    "title": "Обход комиссии",
                    "url": "https://example.ru/source",
                    "snippet": "Подтверждающая цитата",
                    "bank_slug": "sberbank",
                    "raw_text": "Текст источника",
                    "is_loophole": True,
                }
            )
            hook.final_answer = "Готово"
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    state: ChatState = {
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }
    _events = [event async for event in stream_chat(state, session=session)]

    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 0
    source = session.execute(
        text(
            "SELECT source.url, source.extracted_text "
            "FROM loophole_research_source AS source "
            "JOIN loophole_research AS research ON research.research_id = source.research_id "
            "WHERE research.workspace_id = 1"
        )
    ).mappings().one_or_none()
    assert source == {
        "url": "https://example.ru/source",
        "extracted_text": "Текст источника",
    }


@pytest.mark.asyncio
async def test_stream_partial_run_does_not_persist_research_sources(monkeypatch, session):
    """Частичный ответ агента не должен становиться доказательством исследования."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class FakeAgent:
        def __init__(self, context):
            self.context = context

        async def stream(self, _prompt, *, hook):
            self.context.fetched_sources["https://example.ru/source"] = {
                "url": "https://example.ru/source",
                "title": "Источник",
                "extracted_text": "Прочитанный текст",
                "published_at": "2026-08-01T00:00:00+03:00",
            }
            self.context.pending_records.append({
                "title": "Подозрение",
                "url": "https://example.ru/source",
                "snippet": "Цитата",
                "raw_text": "Прочитанный текст",
                "is_loophole": True,
            })
            hook.stop_reason = "error"
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, **_kwargs):
            return FakeAgent(context)

    monkeypatch.setattr(graph, "AgentFactory", FakeFactory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)

    events = [event async for event in graph.stream_chat({
        "query": "Найди лазейки",
        "workspace_id": 1,
        "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]

    assert not any(event["event"] == "records" for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research")).scalar_one() == 0
