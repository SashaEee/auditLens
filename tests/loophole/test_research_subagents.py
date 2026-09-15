"""Контракт предварительных меток, изоляция и жизненный цикл subagents."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from bank_audit.loophole.run_budget import ResearchBudget


def test_managed_registry_offers_research_delegation():
    from bank_audit.loophole.agent import SkillRegistry

    assert "audit_research_subagents" in SkillRegistry.default().names


def _module():
    from bank_audit.loophole.chat import subagents

    return subagents


@pytest.mark.asyncio
async def test_labels_keep_real_sources_and_mask_model_inputs(monkeypatch, tmp_path):
    sub = _module()
    monkeypatch.setenv("LOOPHOLE_SUBAGENT_MODEL", "junior-test")
    captured = []
    settings = {}

    async def search(*args, **kwargs):
        return [{"title": "Пост", "url": "https://example.ru/post", "snippet": "a@test.ru"}]

    class Bot:
        async def run(self, prompt, **kwargs):
            captured.append(prompt)
            settings.update(kwargs)
            return SimpleNamespace(content=json.dumps({"items": [{
                "id": 0, "category": "fraud", "content_type": "post",
                "reason": "Признаки обмана a@test.ru", "url": "https://invented.ru",
            }]}))

        async def aclose(self):
            captured.append("closed")

    def factory(**kwargs):
        assert kwargs["model"] == "junior-test"
        assert kwargs["tool_classes"] == ()
        path = tmp_path / "child.json"
        path.write_text("{}")
        return Bot(), str(path)

    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr("bank_audit.loophole.chat.nanobot_agent.create_nanobot", factory)
    state = sub.ResearchSubagents(ResearchBudget())
    result = await state.research(["карты"], max_results=1)
    item = result["subagents"][0]["items"][0]
    assert item["url"] == "https://example.ru/post"
    assert item["category"] == "fraud"
    assert item["preliminary"] is True
    assert "is_loophole" not in item
    assert "a@test.ru" not in captured[0] + json.dumps(result)
    assert captured[-1] == "closed"
    assert settings["ephemeral"] is True
    assert settings["session_key"].startswith("loophole-subagent:")
    assert not (tmp_path / "child.json").exists()
    stages = []
    while not state.events.empty():
        stages.append(state.events.get_nowait()["status"])
    assert stages == ["searching", "classifying", "completed"]
    # Разметка со сниппетом копится на раннере для персистенции в общий контур.
    assert len(state.triaged) == 1
    assert state.triaged[0]["url"] == "https://example.ru/post"
    assert state.triaged[0]["category"] == "fraud"
    assert state.triaged[0]["snippet"]


@pytest.mark.parametrize("items", [
    [{"id": 5, "category": "fraud", "content_type": "post", "reason": "текст"}],
    [{"id": True, "category": "fraud", "content_type": "post", "reason": "текст"}],
    [{"id": 0, "category": "confirmed", "content_type": "post", "reason": "текст"}],
    [],
])
def test_invalid_model_labels_are_not_accepted(items):
    sub = _module()
    with pytest.raises(ValueError):
        sub.parse_labels(json.dumps({"items": items}), [
            {"url": "https://example.ru", "title": "Источник", "snippet": "Описание"},
        ])


# Резерв на основную модель (LLM_MODEL_NAME) и fail-closed при пустой цепочке
# покрыты в test_agent_stability.py (CAP-5).


@pytest.mark.asyncio
async def test_cancel_propagates_and_emits_terminal_state(monkeypatch):
    sub = _module()
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def search(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(sub, "run_blocking_network", search)
    state = sub.ResearchSubagents(ResearchBudget())
    task = asyncio.create_task(state.research(["карты"]))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    events = []
    while not state.events.empty():
        events.append(state.events.get_nowait())
    assert events[-1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_one_failure_does_not_hide_success_and_launches_are_bounded(monkeypatch):
    sub = _module()
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    active = peak = 0

    async def search(_fn, query, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            if query == "ошибка":
                raise RuntimeError("secret credential")
            return []
        finally:
            active -= 1

    monkeypatch.setattr(sub, "run_blocking_network", search)
    state = sub.ResearchSubagents(ResearchBudget())
    result = await state.research(["ошибка", "карты", "вклады"])
    assert [r["status"] for r in result["subagents"]] == ["failed", "completed", "completed"]
    assert "secret credential" not in json.dumps(result)
    await state.research(["раз", "два", "три"])
    assert (await state.research(["ещё"]))["error"] == "subagent_limit_reached"
    assert peak <= 3


def test_sse_drops_untrusted_fields_and_invalid_events():
    from bank_audit.loophole.chat.graph import _map_event

    event = SimpleNamespace(type="subagent.progress", metadata={
        "id": "subagent-1", "status": "searching", "title": "a@test.ru",
        "total": 0, "completed": 0, "items": [], "secret": "credential",
    })
    mapped = _map_event(event, None)
    assert mapped["event"] == "subagent"
    assert "secret" not in mapped["data"]
    assert "a@test.ru" not in json.dumps(mapped)
    event.metadata["status"] = "invented"
    assert _map_event(event, None) is None


@pytest.mark.asyncio
async def test_managed_stream_exposes_progress_before_worker_finishes(monkeypatch):
    from bank_audit.loophole import agent
    from bank_audit.loophole.chat.hooks import AuditHook

    sub = _module()
    finished = asyncio.Event()
    release = asyncio.Event()
    budget = ResearchBudget(timeout_seconds=3)
    state = sub.ResearchSubagents(budget)

    class Bot:
        async def stream(self, *args, **kwargs):
            state.events.put_nowait({"id": "subagent-1", "status": "classifying",
                                      "title": "Карты", "total": 1, "completed": 0, "items": []})
            await release.wait()
            finished.set()
            yield SimpleNamespace(type="run.completed")

        async def aclose(self):
            pass

    context = agent.AgentRunContext("u", 1, "карты", "stream-test", budget=budget,
                                    subagents=state)
    managed = agent.ManagedAgent(context, Bot(), "")
    stream = managed.stream("карты", hook=AuditHook())
    try:
        async with asyncio.timeout(1):
            async for event in stream:
                if event.type == "subagent.progress":
                    assert event.metadata["status"] == "classifying"
                    assert not finished.is_set()
                    break
        release.set()
        async for _ in stream:
            pass
        assert finished.is_set()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_factory_shares_subagent_budget_and_context(monkeypatch, tmp_path):
    from bank_audit.loophole import agent

    captured = {}

    class Bot:
        async def aclose(self):
            pass

    def factory(**kwargs):
        captured.update(kwargs)
        return Bot(), ""

    monkeypatch.setattr(agent, "create_nanobot", factory)
    monkeypatch.setenv("LOOPHOLE_WORKSPACE_DIR", str(tmp_path))
    managed = agent.AgentFactory().create(agent.AgentRunContext("u", 1, "карты", "factory-test"))
    try:
        assert managed.context.subagents is captured["tool_context"].subagents
        assert managed.context.subagents.budget is managed.context.budget
    finally:
        await managed.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model,extra_body", [
    ("junior-offline", {}),
    ("Qwen/Qwen3.6-35B-A3B", {"chat_template_kwargs": {"enable_thinking": False}}),
])
async def test_real_nanobot_child_uses_selected_model_without_tools(monkeypatch, model, extra_body):
    """Проверяет настоящий SDK; заменён только внешний ответ провайдера."""
    from nanobot.providers.base import LLMResponse
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    sub = _module()
    monkeypatch.setenv("LOOPHOLE_SUBAGENT_MODEL", model)
    observed = []

    async def search(*args, **kwargs):
        return [{"url": "https://example.ru/comment", "title": "Комментарий",
                 "snippet": "Автор описывает пробел в условиях программы"}]

    async def response(self, **kwargs):
        assert all(self._extra_body.get(key) == value for key, value in extra_body.items())
        if extra_body:
            assert self._extra_body["response_format"]["json_schema"]["strict"] is True
        else:
            assert not self._extra_body
        observed.append(kwargs)
        return LLMResponse(content=json.dumps({"items": [{
            "id": 0, "category": "loophole", "content_type": "comment",
            "reason": "Описан пробел в условиях",
        }]}))

    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr(OpenAICompatProvider, "chat_stream_with_retry", response)
    monkeypatch.setattr(OpenAICompatProvider, "chat_with_retry", response)
    result = await sub.ResearchSubagents(ResearchBudget()).research(["карты"])
    assert result["subagents"][0]["status"] == "completed"
    assert result["subagents"][0]["items"][0]["category"] == "loophole"
    assert len(observed) == 1
    assert observed[0]["model"] == model
    assert not observed[0].get("tools")


@pytest.mark.asyncio
async def test_deadline_cancels_child_llm_and_closes_client(monkeypatch, tmp_path):
    sub = _module()
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    closed = asyncio.Event()
    entered = asyncio.Event()

    async def search(*args, **kwargs):
        return [{"title": "Материал", "url": "https://example.ru", "snippet": "Описание"}]

    class Bot:
        async def run(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    config = tmp_path / "child.json"
    config.write_text("{}")
    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr("bank_audit.loophole.chat.nanobot_agent.create_nanobot",
                        lambda **kwargs: (Bot(), str(config)))
    state = sub.ResearchSubagents(ResearchBudget(timeout_seconds=0.1))
    result = await asyncio.wait_for(state.research(["карты"]), 2)
    assert entered.is_set() and closed.is_set()
    assert result["subagents"][0]["status"] == "failed"
    assert result["subagents"][0]["items"] == []
    assert not config.exists()


@pytest.mark.asyncio
async def test_slow_research_can_finish_after_old_sixty_second_cap(monkeypatch):
    """Ускоренные часы: поиск в 80 условных секунд не обрывается старым лимитом 60."""
    sub = _module()
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    monkeypatch.delenv("LOOPHOLE_SUBAGENT_TIMEOUT_SECONDS", raising=False)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(sub.asyncio, "timeout", lambda seconds: real_timeout(seconds / 1000))

    async def slow_search(*args, **kwargs):
        await asyncio.sleep(0.08)
        return []

    monkeypatch.setattr(sub, "run_blocking_network", slow_search)
    result = await sub.ResearchSubagents(ResearchBudget(timeout_seconds=300)).research(["карты"])
    assert result["subagents"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_timeout_is_explained_in_public_event_and_safe_log(monkeypatch, caplog):
    sub = _module()
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")

    async def search(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(sub, "run_blocking_network", search)
    state = sub.ResearchSubagents(ResearchBudget(timeout_seconds=0.02))
    result = await state.research(["карты"])
    event = result["subagents"][0]
    assert event["status"] == "failed"
    assert event["error_code"] == "timeout"
    assert "времени" in event["message"]
    assert "loophole_subagent_failed" in caplog.text
    assert "stage=searching" in caplog.text


@pytest.mark.asyncio
async def test_provider_failure_does_not_masquerade_as_bad_json(monkeypatch, tmp_path, caplog):
    sub = _module()
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")

    async def search(*args, **kwargs):
        return [{"url": "https://example.ru", "title": "Материал", "snippet": "Описание"}]

    class Bot:
        async def run(self, *args, **kwargs):
            return SimpleNamespace(content="secret-credential", stop_reason="error",
                                   error="secret-credential")

        async def aclose(self):
            pass

    config = tmp_path / "failed-child.json"
    config.write_text("{}")
    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr("bank_audit.loophole.chat.nanobot_agent.create_nanobot",
                        lambda **kwargs: (Bot(), str(config)))
    result = await sub.ResearchSubagents(ResearchBudget()).research(["карты"])
    assert result["subagents"][0]["error_code"] == "model_error"
    assert "secret-credential" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
async def test_configured_child_timeout_keeps_global_deadline(monkeypatch):
    sub = _module()
    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    monkeypatch.setenv("LOOPHOLE_SUBAGENT_TIMEOUT_SECONDS", "600")

    async def search(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(sub, "run_blocking_network", search)
    result = await sub.ResearchSubagents(ResearchBudget(timeout_seconds=0.02)).research(["карты"])
    assert result["subagents"][0]["error_code"] == "timeout"
