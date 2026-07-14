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
async def test_run_chat_complete_does_not_crash(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    async def fake_gen(question, history=None):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)

    state: ChatState = {"query": "сколько записей в базе", "workspace_id": 1, "user_id": "u1"}
    out = await run_chat(state)
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
