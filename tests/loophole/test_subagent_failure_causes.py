"""Регрессии причин сбоев исследователей из живых запусков 2026-09-08."""
import asyncio
import json

import pytest

from bank_audit.loophole.chat import subagents as sub
from bank_audit.loophole.run_budget import ResearchBudget


@pytest.mark.asyncio
async def test_waiting_for_slot_does_not_spend_child_deadline(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")

    async def search(*args, **kwargs):
        return []

    monkeypatch.setattr(sub, "run_blocking_network", search)
    state = sub.ResearchSubagents(ResearchBudget())
    state._timeout_seconds = 0.03
    for _ in range(3):
        await state._slots.acquire()
    task = asyncio.create_task(state.research(["карты"]))
    try:
        await asyncio.sleep(0.07)
        state._slots.release()
        result = (await asyncio.wait_for(task, 1))["subagents"][0]
        assert result["status"] == "completed"
        assert result["attempts"] == ["subagent-1"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_queued_child_obeys_total_deadline_and_cancellation(monkeypatch, cancel):
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    state = sub.ResearchSubagents(ResearchBudget(timeout_seconds=0 if cancel else 0.08))
    for _ in range(3):
        await state._slots.acquire()
    task = asyncio.create_task(state.research(["карты"]))
    assert (await asyncio.wait_for(state.events.get(), 1))["status"] == "queued"
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = (await asyncio.wait_for(task, 1))["subagents"][0]
        assert result["error_code"] == "timeout"
        assert result["attempts"] == ["subagent-1"]
    assert (await asyncio.wait_for(state.events.get(), 1))["status"] == (
        "cancelled" if cancel else "failed")


@pytest.mark.asyncio
async def test_progressing_batches_have_individual_deadlines(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")

    async def search(*args, **kwargs):
        return [{"url": f"https://example.test/{i}", "title": str(i), "snippet": "Описание"}
                for i in range(8)]

    async def classify(self, sources, model):
        await asyncio.sleep(0.02)
        return sub.parse_labels(json.dumps({"items": [
            {"id": i, "category": "irrelevant", "content_type": "article", "reason": "Реклама"}
            for i in range(len(sources))]}), sources)

    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr(sub.ResearchSubagents, "_classify", classify)
    state = sub.ResearchSubagents(ResearchBudget())
    state._timeout_seconds = 0.05
    result = (await state.research(["карты"]))["subagents"][0]
    assert result["completed"] == 8
    assert result["attempts"] == ["subagent-1"]


@pytest.mark.asyncio
async def test_qwen_classifier_requests_strict_labels_without_stream_idle_timer(monkeypatch):
    from nanobot.providers.base import LLMResponse
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    async def response(self, **kwargs):
        await self._ensure_client()
        assert self._client.timeout.connect == 10
        assert self._client.timeout.read is None
        assert self._client._client.timeout.connect == 10
        body = self._build_kwargs(**kwargs)
        schema = body["extra_body"]["response_format"]["json_schema"]["schema"]
        row = schema["properties"]["items"]["items"]
        assert set(row["properties"]["category"]["enum"]) == {
            "loophole", "fraud", "irrelevant", "insufficient_data"}
        assert row["properties"]["id"]["enum"] == [0]
        assert body.get("stream") is not True
        return LLMResponse(content=json.dumps({"items": [{"id": 0, "category": "irrelevant",
            "content_type": "article", "reason": "Штатная льгота"}]}))

    async def no_stream(*args, **kwargs):
        pytest.fail("Классификатор не должен использовать idle timeout потокового SDK")

    monkeypatch.setattr(OpenAICompatProvider, "chat", response)
    monkeypatch.setattr(OpenAICompatProvider, "chat_stream", no_stream)
    result = await sub.ResearchSubagents(ResearchBudget())._classify([
        {"url": "https://example.test", "title": "Источник", "snippet": "Льготы по карте"},
    ], "Qwen/Qwen3.6-35B-A3B")
    assert result[0]["category"] == "irrelevant"
