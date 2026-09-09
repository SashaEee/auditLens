"""Возобновление незавершённых материалов и ожидание основной модели без дедлайна."""
import asyncio
import json

import pytest

from bank_audit.loophole.chat import subagents as sub
from bank_audit.loophole.run_budget import ResearchBudget


@pytest.mark.asyncio
async def test_successor_receives_only_unfinished_sources(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    sources = [{"url": f"https://example.test/{i}", "title": str(i), "snippet": str(i)}
               for i in range(4)]
    searches = []
    batches = []

    async def search(*args, **kwargs):
        searches.append(True)
        return sources

    async def classify(self, rows, model):
        batches.append([s["url"] for s in rows])
        if len(batches) == 2:
            raise sub.SubagentResponseError("model_error")
        return sub.parse_labels(json.dumps({"items": [
            {"id": i, "category": "fraud", "content_type": "post", "reason": "Описание"}
            for i in range(len(rows))]}), rows)

    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr(sub.ResearchSubagents, "_classify", classify)
    state = sub.ResearchSubagents(ResearchBudget())
    result = (await state.research(["карты"]))["subagents"][0]
    assert result["status"] == "completed"
    assert result["completed"] == result["total"] == 4
    assert {i["url"] for i in result["items"]} == {s["url"] for s in sources}
    assert len(searches) == 1
    assert batches == [[s["url"] for s in sources[:2]],
                       [s["url"] for s in sources[2:]], [s["url"] for s in sources[2:]]]
    events = []
    while not state.events.empty():
        events.append(state.events.get_nowait())
    assert any(e["status"] == "failed" and e["completed"] == 2 for e in events)
    assert any(e.get("retry_of") == "subagent-1" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_unlimited_main_model_can_finish_and_be_cancelled(
    monkeypatch, tmp_path, streaming, cancel,
):
    from nanobot.providers.base import LLMResponse
    from bank_audit.loophole.agent import AgentRunContext, ManagedAgent
    from bank_audit.loophole.chat.hooks import AuditHook
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot

    monkeypatch.setenv("LOOPHOLE_MODEL_TIMEOUT_SECONDS", "0")
    budget = ResearchBudget(timeout_seconds=0)
    bot, path = create_nanobot(workspace=tmp_path, disable_model_timeouts=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(**kwargs):
        entered.set()
        await release.wait()
        return LLMResponse(content="Готовый отчёт")

    monkeypatch.setattr(bot._loop.provider, "chat", response)
    ctx = AgentRunContext("u", 1, "карты", "unlimited", budget=budget)
    managed = ManagedAgent(ctx, bot, path)
    hook = AuditHook()

    async def run():
        if streaming:
            return [e async for e in managed.stream("карты", hook=hook)]
        return await managed.run()

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        budget.started_at -= 100000
        await asyncio.sleep(0.03)
        assert not task.done()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            release.set()
            await asyncio.wait_for(task, 2)
        assert budget.stop_reason is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_main_http_timeout_disabled_only_on_parent(tmp_path):
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot
    parent, pp = create_nanobot(workspace=tmp_path / "parent", disable_model_timeouts=True)
    child, cp = create_nanobot(workspace=tmp_path / "child")
    try:
        for bot in (parent, child):
            await bot._loop.provider._ensure_client()
        assert parent._loop.provider._client.timeout is None
        assert parent._loop.provider._client._client.timeout.read is None
        assert child._loop.provider._client._client.timeout.read is not None
    finally:
        from pathlib import Path
        await parent.aclose()
        await child.aclose()
        Path(pp).unlink(missing_ok=True)
        Path(cp).unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_count", [None, 1])
async def test_automatic_extraction_emits_tool_progress_before_result(monkeypatch, requested_count):
    from types import SimpleNamespace
    from bank_audit.loophole.agent import AgentRunContext, ManagedAgent
    from bank_audit.loophole.chat.hooks import AuditHook
    from bank_audit.loophole.chat import tools_nanobot
    from bank_audit.loophole.chat.graph import _map_event

    release = asyncio.Event()

    async def extract(text):
        await release.wait()
        return ([{"title": "Кандидат", "is_loophole": True, "evidence_quote": text,
                  "description": "Механизм"}] if requested_count else [])

    monkeypatch.setattr(tools_nanobot, "extract_loopholes", extract)
    ctx = AgentRunContext("u", 1, "за 2026 год", "auto-progress",
                          budget=ResearchBudget(requested_count=requested_count))
    ctx.fetched_sources["https://example.test"] = {
        "url": "https://example.test", "extracted_text": "Статья", "title": "Источник",
        "published_at": "2026-04-01T00:00:00+00:00",
    }

    class Bot:
        async def stream(self, prompt, **kwargs):
            await kwargs["hooks"][1].after_iteration(SimpleNamespace(iteration=1))
            yield SimpleNamespace(type="run.completed")

        async def aclose(self):
            pass

    hook = AuditHook()
    events = []
    stream = ManagedAgent(ctx, Bot(), "").stream("query", hook=hook)
    try:
        async with asyncio.timeout(1):
            async for event in stream:
                mapped = _map_event(event, hook)
                if mapped:
                    events.append(mapped)
                    if mapped["event"] == "tool_call":
                        release.set()
                        await asyncio.sleep(0.03)
        assert [e["event"] for e in events if e["event"].startswith("tool_")] == [
            "tool_call", "tool_result"]
        assert [e["data"]["status"] for e in events if e["event"] == "tool_result"] == [
            "completed"]
        if requested_count:
            assert hook.stop_reason == "requested_count"
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_child_timeout_replacement_keeps_original_search(monkeypatch, recover):
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    batches = []

    async def search(*args, **kwargs):
        assert not batches, "Найденный материал нельзя терять и искать заново"
        return [{"url": "https://example.test/post", "title": "Пост", "snippet": "Описание"}]

    async def classify(self, rows, model):
        batches.append(rows)
        if not recover or len(batches) == 1:
            await asyncio.Event().wait()
        return sub.parse_labels(json.dumps({"items": [{"id": 0, "category": "loophole",
            "content_type": "post", "reason": "Пробел в условиях"}]}), rows)

    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr(sub.ResearchSubagents, "_classify", classify)
    state = sub.ResearchSubagents(ResearchBudget())
    state._timeout_seconds = 0.03
    result = (await asyncio.wait_for(state.research(["карты"]), 1))["subagents"][0]
    assert len(batches) == (2 if recover else 3)
    assert result["status"] == ("completed" if recover else "failed")
    assert len(result["pending_sources"]) == (0 if recover else 1)
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_full_response_adapter_retries_error_without_emitting_error_text(monkeypatch, tmp_path):
    from nanobot.providers.base import LLMResponse
    from bank_audit.loophole.agent import AgentRunContext, ManagedAgent
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot

    bot, path = create_nanobot(workspace=tmp_path, disable_model_timeouts=True)
    calls = []
    deltas = []

    async def chat(**kwargs):
        calls.append(True)
        return (LLMResponse(content="Error: private provider detail", finish_reason="error",
                            error_should_retry=True) if len(calls) == 1
                else LLMResponse(content="Проверка прошла"))

    async def delta(text):
        deltas.append(text)

    monkeypatch.setattr(bot._loop.provider, "chat", chat)
    managed = ManagedAgent(AgentRunContext("u", 1, "query", "retry-full"), bot, path)
    try:
        result = await bot._loop.provider.chat_stream_with_retry(
            messages=[{"role": "user", "content": "query"}], on_content_delta=delta)
        assert result.content == "Проверка прошла"
        assert deltas == ["Проверка прошла"]
        assert len(calls) == 2
    finally:
        await managed.aclose()


@pytest.mark.asyncio
async def test_close_drains_background_tasks_scheduled_during_cleanup(tmp_path):
    from pathlib import Path
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot

    bot, path = create_nanobot(workspace=tmp_path)
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    errors = []
    finished = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def second():
        await asyncio.sleep(0.01)
        finished.append(True)

    async def first():
        bot._loop._schedule_background(second())

    try:
        bot._loop._schedule_background(first())
        await bot.aclose()
        assert finished, "Закрытие должно дождаться фоновой работы, появившейся при очистке"
        await asyncio.sleep(0.02)
        assert not errors
    finally:
        await asyncio.sleep(0.03)
        loop.set_exception_handler(old_handler)
        Path(path).unlink(missing_ok=True)
