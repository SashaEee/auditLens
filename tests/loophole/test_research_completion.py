"""Ограничение бесплодного поиска и сохранение полезного частичного результата."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from bank_audit.loophole.agent import AgentRunContext, ManagedAgent
from bank_audit.loophole.chat.hooks import AuditHook
from bank_audit.loophole.run_budget import ResearchBudget


def context():
    return AgentRunContext("analyst", 1, "Лазейки за 2026 год", "completion-test",
                           budget=ResearchBudget(timeout_seconds=300))


@pytest.mark.asyncio
async def test_no_progress_stops_and_keeps_materials():
    ctx = context()
    ctx.budget.search_results = [{"title": "Материал", "url": "https://example.test/post"}]

    class Bot:
        async def stream(self, prompt, **kwargs):
            for i in range(20):
                await kwargs["hooks"][1].after_iteration(SimpleNamespace(iteration=i))
                yield SimpleNamespace(type="test.iteration")
            pytest.fail("Бесплодный поиск не остановлен")

        async def aclose(self):
            pass

    hook = AuditHook()
    events = [e async for e in ManagedAgent(ctx, Bot(), "").stream("query", hook=hook)]
    assert len(events) < 10
    assert hook.stop_reason == "no_progress"
    assert "https://example.test/post" in hook.final_answer
    assert "не проверены" in hook.final_answer


@pytest.mark.asyncio
async def test_model_call_deadline_covers_retries_and_keeps_sources(monkeypatch, tmp_path):
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot

    ctx = context()
    ctx.budget.model_timeout_seconds = 0.03
    ctx.fetched_sources["https://example.test/read"] = {
        "url": "https://example.test/read", "title": "Прочитанная статья",
        "extracted_text": "Текст", "published_at": None,
    }
    bot, path = create_nanobot(workspace=tmp_path)
    cancelled = []

    async def slow(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(bot._loop.provider, "chat_stream_with_retry", slow)
    hook = AuditHook()
    async with asyncio.timeout(3):
        _events = [e async for e in ManagedAgent(ctx, bot, path).stream("query", hook=hook)]
    assert cancelled
    assert hook.stop_reason == "model_timeout"
    assert "https://example.test/read" in hook.final_answer
    assert "Дата публикации не подтверждена" in hook.final_answer


@pytest.mark.asyncio
async def test_read_source_is_extracted_before_next_model_round(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    ctx = context()
    url = "https://example.test/read"
    ctx.fetched_sources[url] = {
        "url": url, "title": "Статья", "extracted_text": "Дословная цитата о механизме",
        "published_at": "2026-04-01T00:00:00+00:00",
    }
    seen = []

    async def extract(text):
        seen.append(text)
        return [{"title": "Кандидат", "is_loophole": True,
                 "evidence_quote": text, "description": "Механизм"}]

    monkeypatch.setattr(tools, "extract_loopholes", extract)

    class Bot:
        async def run(self, prompt, **kwargs):
            hook = kwargs["hooks"][1]
            await hook.after_iteration(SimpleNamespace(iteration=0))
            await hook.after_iteration(SimpleNamespace(iteration=1))
            assert len(seen) == 1
            assert len(ctx.pending_records) == 1
            return SimpleNamespace(content="Отчёт", stop_reason="completed")

        async def aclose(self):
            pass

    result = await ManagedAgent(ctx, Bot(), "").run()
    assert not result.partial
    assert len(seen) == 1
    assert len(ctx.pending_records) == 1


@pytest.mark.asyncio
async def test_failed_fetch_is_retried_then_reported_and_cached(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    ctx = context()
    monkeypatch.setattr(tools, "_TOOL_RETRY_DELAYS", (0, 0))
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("secret provider detail")

    monkeypatch.setattr(tools, "run_blocking_network", unavailable)
    tool = tools.AuditWebFetchTool(tools.ToolContext(
        "analyst", 1, None, query=ctx.query, budget=ctx.budget,
    ))
    first = json.loads(await tool.execute("https://example.test/down"))
    second = json.loads(await tool.execute("https://example.test/down"))
    assert first == second and first["error"] == "source_unavailable"
    assert len(calls) == 3  # исходная попытка + два ретрая транзиента; далее — из кэша
    assert "secret" not in json.dumps(ctx.budget.source_failures)


@pytest.mark.asyncio
async def test_model_retries_transient_errors_before_giving_up(monkeypatch, tmp_path):
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot
    from nanobot.providers.base import LLMResponse

    ctx = context()
    calls = []
    bot, path = create_nanobot(workspace=tmp_path)

    async def provider(**kwargs):
        calls.append(True)
        return LLMResponse(content="Error calling llm: connection error secret", finish_reason="error")

    async def heartbeat(delay, **kwargs):
        for _ in range(3):
            if kwargs.get("on_retry_wait"):
                await kwargs["on_retry_wait"]("Same wait, another heartbeat")

    monkeypatch.setattr(bot._loop.provider, "_safe_chat", provider)
    monkeypatch.setattr(bot._loop.provider, "_safe_chat_stream", provider)
    monkeypatch.setattr(bot._loop.provider, "_sleep_with_heartbeat", heartbeat)
    result = await ManagedAgent(ctx, bot, path).run()
    assert calls == [True, True, True, True]  # штатные ретраи SDK (1, 2, 4) не урезаны
    assert result.stop_reason == "model_unavailable" and result.partial
    assert "secret" not in result.answer


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["no_progress", "model_timeout"])
async def test_sse_partial_keeps_valid_candidate_and_material_report(monkeypatch, session, reason):
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_agent_latency_budget import _add_candidate
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)
    saved = []

    class Factory:
        def create(self, supplied, **kwargs):
            from dataclasses import replace

            ctx = replace(supplied, budget=ResearchBudget(timeout_seconds=300))
            _add_candidate(ctx)
            ctx.budget.search_results.append({"title": "Зацепка", "url": "https://example.test/lead"})

            class Bot:
                async def stream(self, prompt, **kwargs):
                    await kwargs["hooks"][0].on_stream(None, "Незавершённый черновик модели")
                    ctx.budget.stop_reason = reason
                    await kwargs["hooks"][1].before_iteration(SimpleNamespace(iteration=0))
                    yield

                async def aclose(self):
                    pass

            return ManagedAgent(ctx, Bot(), "")

    monkeypatch.setattr(graph, "AgentFactory", Factory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(graph.repo, "add_chat_message", lambda *args, **kwargs: saved.append(args))
    events = [e async for e in graph.stream_chat({
        "query": "Найди лазейки за 2026 год", "workspace_id": 1, "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]
    assert any(e["event"] == "records" for e in events)
    assert len(saved) == 1
    tokens = "".join(e["data"] for e in events if e["event"] == "token")
    assert "https://example.test/lead" in tokens
    assert "Незавершённый черновик" not in tokens
    assert "https://example.test/lead" in saved[0][2]
    assert "AI-кандидаты" in saved[0][2]
    # Подтверждённый кандидат частичного прогона автоимпортируется в каталог.
    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM loophole_research_candidate")).scalar_one() == 1


@pytest.mark.asyncio
async def test_search_quota_and_duplicate_do_not_make_more_requests(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    ctx = context()
    ctx.budget.search_limit = 1
    calls = []

    async def search(*args, **kwargs):
        calls.append(True)
        return [{"title": "Материал", "url": "https://example.test/lead"}]

    monkeypatch.setattr(tools, "run_blocking_network", search)
    tool = tools.AuditWebSearchTool(tools.ToolContext("analyst", 1, None, budget=ctx.budget))
    await tool.execute("  Карта   Сбер ")
    await tool.execute("карта сбер")
    denied = json.loads(await tool.execute("другой запрос"))
    assert denied["error"] == "search_limit" and len(calls) == 1
    assert len(ctx.budget.search_results) == 1


@pytest.mark.asyncio
async def test_extraction_failure_is_not_empty_success(monkeypatch, caplog):
    from bank_audit.loophole.chat import tools_nanobot as tools
    from bank_audit.loophole.chat.tools_nanobot import extract_loopholes

    monkeypatch.setattr(tools, "_EXTRACTION_RETRY_DELAYS", ())

    class LLM:
        async def ainvoke(self, messages):
            raise TimeoutError("secret provider response")

    with pytest.raises(RuntimeError, match="extraction_failed"):
        await extract_loopholes("Текст статьи", llm=LLM())
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_completed_answer_is_not_a_stalled_search_round():
    ctx = context()

    class Bot:
        async def run(self, prompt, **kwargs):
            for i in range(2):
                await kwargs["hooks"][1].after_iteration(SimpleNamespace(iteration=i))
            await kwargs["hooks"][1].after_iteration(SimpleNamespace(
                iteration=2, tool_calls=[], stop_reason="completed",
            ))
            return SimpleNamespace(content="Полезный итог", stop_reason="completed")

        async def aclose(self):
            pass

    result = await ManagedAgent(ctx, Bot(), "").run()
    assert not result.partial and result.answer == "Полезный итог"


def test_model_state_keeps_one_leading_system_message_and_refreshes_candidates():
    from tests.loophole.test_agent_latency_budget import _add_candidate

    ctx = context()
    managed = ManagedAgent(ctx, SimpleNamespace(), "")
    hook_ctx = SimpleNamespace(messages=[
        {"role": "system", "content": "Правила платформы"},
        {"role": "user", "content": "Запрос пользователя"},
    ])
    managed._update_model_state(hook_ctx)
    _add_candidate(ctx)
    managed._update_model_state(hook_ctx)
    assert [m["role"] for m in hook_ctx.messages] == ["system", "user"]
    assert hook_ctx.messages[0]["content"].count("Состояние проверки источников AuditLens.") == 1
    assert "Цитата о механизме" in hook_ctx.messages[0]["content"]
    assert hook_ctx.messages[1]["content"] == "Запрос пользователя"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_real_sdk_next_request_receives_auto_extraction(monkeypatch, tmp_path, stream):
    from bank_audit.loophole.agent import AgentFactory
    from bank_audit.loophole.chat import tools_nanobot as tools
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    seen = []
    url = "https://example.test/article"

    async def network(*args, **kwargs):
        return {"url": url, "title": "Статья", "excerpt": "Дословная цитата о механизме",
                "published_at": "2026-04-01T00:00:00+00:00"}

    async def extract(text):
        return [{"title": "Автоматически найденный кандидат", "description": "Описание",
                 "is_loophole": True, "evidence_quote": text}]

    async def response(self, **kwargs):
        messages = kwargs["messages"]
        seen.append(messages)
        assert messages[0]["role"] == "system"
        assert all(m["role"] != "system" for m in messages[1:])
        if len(seen) == 1:
            return LLMResponse(content=None, tool_calls=[
                ToolCallRequest("read-1", "audit_web_fetch", {"url": url}),
            ])
        assert "Автоматически найденный кандидат" in messages[0]["content"]
        return LLMResponse(content="Отчёт с результатами извлечения")

    monkeypatch.setattr(tools, "run_blocking_network", network)
    monkeypatch.setenv("LOOPHOLE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(tools, "extract_loopholes", extract)
    monkeypatch.setattr(OpenAICompatProvider, "chat_with_retry", response)
    monkeypatch.setattr(OpenAICompatProvider, "chat_stream_with_retry", response)
    managed = AgentFactory().create(context())
    if stream:
        hook = AuditHook()
        _events = [e async for e in managed.stream("query", hook=hook)]
        assert len(seen) == 2 and not hook.tool_errors
        assert "audit_extract_loopholes" in hook.tools_used
        assert len(hook.records) == 1
    else:
        result = await managed.run()
        assert len(seen) == 2 and not result.partial, result.errors
        assert "audit_extract_loopholes" in result.tools_used
        assert len(result.records) == 1


@pytest.mark.asyncio
async def test_processing_new_sources_is_progress_even_without_candidates(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    ctx = context()
    for i in range(12):
        url = f"https://example.test/{i}"
        ctx.fetched_sources[url] = {
            "url": url, "title": "Материал", "extracted_text": "Обычные условия продукта",
            "published_at": "2026-04-01T00:00:00+00:00",
        }

    async def extract(text):
        return []

    monkeypatch.setattr(tools, "extract_loopholes", extract)
    managed = ManagedAgent(ctx, SimpleNamespace(), "")
    for _ in range(6):
        await managed._complete_iteration()
    assert len(ctx.budget.analysis_status) == 12 and not ctx.budget.stop_reason
