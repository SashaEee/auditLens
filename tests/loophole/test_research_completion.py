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
async def test_no_progress_does_not_preempt_new_search_clusters():
    ctx = context()
    ctx.budget.search_results = [{"title": "Материал", "url": "https://example.test/post"}]

    class Bot:
        async def stream(self, prompt, **kwargs):
            for i in range(13):
                await kwargs["hooks"][1].after_iteration(SimpleNamespace(iteration=i))
                yield SimpleNamespace(type="test.iteration")

        async def aclose(self):
            pass

    hook = AuditHook()
    events = [e async for e in ManagedAgent(ctx, Bot(), "").stream("query", hook=hook)]
    assert len(events) >= 13
    assert hook.stop_reason is None
    assert hook.final_answer == ""


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
    assert "https://example.test/source" in tokens
    assert "Незавершённый черновик" not in tokens
    assert "https://example.test/lead" not in saved[0][2]
    assert "AI-кандидаты" in saved[0][2]
    # Подтверждённый кандидат частичного прогона автоимпортируется в каталог.
    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM loophole_research_candidate")).scalar_one() == 1


@pytest.mark.asyncio
async def test_search_continues_past_legacy_quota_and_deduplicates_queries(monkeypatch):
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
    await tool.execute("другой запрос")
    assert len(calls) == 2
    assert len(ctx.budget.search_results) == 1


@pytest.mark.asyncio
async def test_redirect_alias_and_failed_fetch_do_not_count_as_successful_page(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    ctx = context()
    calls = []

    async def network(*args):
        calls.append(True)
        return {
            "url": "https://example.test/alias",
            "final_url": "https://example.test/canonical#fragment",
            "title": "Прочитанная страница",
            "excerpt": "Непустой извлечённый текст",
        }

    monkeypatch.setattr(tools, "run_blocking_network", network)
    tool = tools.AuditWebFetchTool(tools.ToolContext(
        "analyst", 1, None, query=ctx.query, budget=ctx.budget,
        fetched_sources=ctx.fetched_sources,
    ))
    first = json.loads(await tool.execute("https://example.test/alias"))
    duplicate = json.loads(await tool.execute("https://example.test/canonical"))
    assert first["excerpt"] == "Непустой извлечённый текст"
    assert duplicate["error"] == "source_duplicate"
    assert ctx.budget.successful_page_count == 1
    assert list(ctx.fetched_sources) == ["https://example.test/canonical"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_page_limit_without_findings_is_normal_zero_report():
    ctx = context()
    for index in range(100):
        url = f"https://example.test/{index}"
        assert ctx.budget.register_successful_page(url)
        ctx.fetched_sources[url] = {
            "url": url, "title": f"Материал {index}", "extracted_text": "Текст",
            "published_at": "2026-04-01T00:00:00+00:00",
        }

    hook = AuditHook()
    managed = ManagedAgent(ctx, SimpleNamespace(), "")
    with pytest.raises(asyncio.CancelledError):
        managed._check_limits()
    managed._finish_budget_stop(hook)
    assert hook.stop_reason == "page_limit"
    assert hook.tool_errors == []
    assert "100 уникальных успешно прочитанных страниц" in hook.final_answer
    assert "не доказывает отсутствие лазеек" in hook.final_answer


@pytest.mark.asyncio
async def test_page_limit_with_findings_finishes_with_confirmed_only():
    from tests.loophole.test_agent_latency_budget import _add_candidate

    ctx = context()
    _add_candidate(ctx)
    fraud = {**ctx.pending_records[0], "classification": "fraud_scheme",
             "title": "Мошенническая схема"}
    ctx.pending_records.append(fraud)
    for index in range(100):
        assert ctx.budget.register_successful_page(f"https://example.test/{index}")

    hook = AuditHook()
    managed = ManagedAgent(ctx, SimpleNamespace(), "")
    with pytest.raises(asyncio.CancelledError):
        managed._check_limits()
    managed._finish_budget_stop(hook)
    assert hook.stop_reason == "page_limit"
    assert hook.tool_errors == []
    assert "Кандидат" in hook.final_answer
    assert "Мошенническая схема" not in hook.final_answer
    assert "100 уникальных успешно прочитанных страниц" in hook.final_answer
    # Все находки агента остаются в persistence-контуре: отчёт показывает только
    # запрошенный тип, но схемы тоже идут в каталог с типом fraud_scheme.
    assert [record["title"] for record in ctx.pending_records] == [
        "Кандидат", "Мошенническая схема",
    ]


@pytest.mark.asyncio
async def test_sse_page_limit_with_findings_is_completed_report(monkeypatch, session):
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_agent_latency_budget import _add_candidate
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)
    saved = []

    class Factory:
        def create(self, supplied, **kwargs):
            from dataclasses import replace

            from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA

            budget = ResearchBudget(timeout_seconds=300)
            ctx = replace(supplied, budget=budget)
            _add_candidate(ctx)
            for index in range(99):
                assert budget.register_successful_page(f"https://example.test/page-{index}")

            class Bot:
                async def stream(self, prompt, **kwargs):
                    yield SimpleNamespace(type=STREAM_EVENT_TEXT_DELTA,
                                          delta="Незавершённый черновик модели")
                    # Потоковая дельта уже в буфере редактора, когда достигнут потолок.
                    assert budget.register_successful_page("https://example.test/final")
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
    tokens = "".join(e["data"] for e in events if e["event"] == "token")
    answer_phases = [e for e in events
                     if e["event"] == "phase" and e["data"].get("phase") == "answer"]
    assert len(answer_phases) == 1
    assert answer_phases[0]["data"]["partial"] is False
    assert answer_phases[0]["data"]["stop_reason"] == "page_limit"
    assert "Незавершённый черновик" not in tokens
    assert "https://example.test/source" in tokens
    assert "100 уникальных успешно прочитанных страниц" in tokens
    assert len(saved) == 1 and "AI-кандидаты" in saved[0][2]
    # Доказательная находка сохраняется и автоимпортируется в каталог как обычно.
    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 1
    assert session.execute(text("SELECT count(*) FROM loophole_research_source")).scalar_one() == 1


@pytest.mark.asyncio
async def test_page_limit_zero_result_persists_snapshot_without_catalog_import(monkeypatch, session):
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class Factory:
        def create(self, supplied, **kwargs):
            from dataclasses import replace

            budget = ResearchBudget(timeout_seconds=300)
            ctx = replace(supplied, budget=budget)
            for index in range(100):
                url = f"https://example.test/snapshot-{index}"
                assert budget.register_successful_page(url)
                ctx.fetched_sources[url] = {
                    "url": url, "title": f"Материал {index}", "extracted_text": "Текст",
                    "published_at": "2026-04-01T00:00:00+00:00",
                }

            class Bot:
                async def stream(self, prompt, **kwargs):
                    await kwargs["hooks"][1].before_iteration(SimpleNamespace(iteration=0))
                    yield

                async def aclose(self):
                    pass

            return ManagedAgent(ctx, Bot(), "")

    monkeypatch.setattr(graph, "AgentFactory", Factory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)
    events = [event async for event in graph.stream_chat({
        "query": "Лазейки за 2026 год", "workspace_id": 1, "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]
    answer = "".join(event["data"] for event in events if event["event"] == "token")
    assert "100 уникальных успешно прочитанных страниц" in answer
    assert "event: report" not in "\n".join(str(event) for event in events)
    assert session.execute(text("SELECT count(*) FROM loophole_research_source")).scalar_one() == 100
    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 0


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


@pytest.mark.asyncio
async def test_stream_flush_contains_only_last_model_round(monkeypatch, session):
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA

    from bank_audit.loophole.chat import graph

    class Factory:
        def create(self, supplied, **kwargs):
            from dataclasses import replace

            ctx = replace(supplied, budget=ResearchBudget(timeout_seconds=300))

            class Bot:
                async def stream(self, prompt, **kwargs):
                    hook, budget_hook = kwargs["hooks"]
                    yield SimpleNamespace(type=STREAM_EVENT_TEXT_DELTA,
                                          delta="Промежуточные рассуждения раунда 1")
                    # Новый раунд модели: сервер очищает буфер стрима.
                    await budget_hook.before_iteration(SimpleNamespace(iteration=1))
                    yield SimpleNamespace(type=STREAM_EVENT_TEXT_DELTA, delta="Итоговый отчёт")
                    await hook.on_stream(None, "Итоговый отчёт")
                    hook.stop_reason = "completed"

                async def aclose(self):
                    pass

            return ManagedAgent(ctx, Bot(), "")

    monkeypatch.setattr(graph, "AgentFactory", Factory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)
    events = [e async for e in graph.stream_chat({
        "query": "Найди лазейки за 2026 год", "workspace_id": 1, "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]
    tokens = "".join(e["data"] for e in events if e["event"] == "token")
    assert "Промежуточные рассуждения" not in tokens
    assert "Итоговый отчёт" in tokens


@pytest.mark.asyncio
async def test_sse_max_iterations_keeps_final_report_without_intermediate_rounds(
    monkeypatch, session,
):
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA

    from bank_audit.loophole.chat import graph

    class Factory:
        def create(self, supplied, **kwargs):
            from dataclasses import replace

            ctx = replace(supplied, budget=ResearchBudget(timeout_seconds=300))

            class Bot:
                async def stream(self, prompt, **kwargs):
                    hook, budget_hook = kwargs["hooks"]
                    yield SimpleNamespace(type=STREAM_EVENT_TEXT_DELTA,
                                          delta="Черновик промежуточного раунда")
                    # Новый раунд модели: сервер очищает буфер стрима.
                    await budget_hook.before_iteration(SimpleNamespace(iteration=1))
                    yield SimpleNamespace(type=STREAM_EVENT_TEXT_DELTA,
                                          delta="Итоговый отчёт модели")
                    await hook.after_run(SimpleNamespace(
                        final_content="Итоговый отчёт модели",
                        stop_reason="max_iterations",
                        tools_used=[],
                    ))

                async def aclose(self):
                    pass

            return ManagedAgent(ctx, Bot(), "")

    monkeypatch.setattr(graph, "AgentFactory", Factory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)
    events = [e async for e in graph.stream_chat({
        "query": "Найди лазейки за 2026 год", "workspace_id": 1, "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]
    tokens = "".join(e["data"] for e in events if e["event"] == "token")
    assert "Черновик промежуточного" not in tokens
    assert "Итоговый отчёт модели" in tokens
    token_index = next(index for index, event in enumerate(events)
                       if event["event"] == "token" and "Итоговый отчёт модели" in event["data"])
    partial_events = [index for index, event in enumerate(events) if event["event"] == "partial"]
    assert partial_events and partial_events[0] > token_index


def test_similar_known_records_section_matches_db_and_excludes_current_findings(session):
    from sqlalchemy import text

    from bank_audit.loophole.chat.graph import _similar_known_records_section

    session.execute(text(
        "INSERT INTO loophole_record (sha256, title, url, bank_slug, is_loophole, status) VALUES "
        "('h1', 'Обход грейс-периода кредитки', 'https://example.test/known-grace', "
        "'sberbank', 1, 'published'), "
        "('h2', 'Схема с кошельком', 'https://example.test/known-wallet', 'vtb', 1, 'preliminary'), "
        "('h3', 'Не лазейка', 'https://example.test/other', 'sberbank', 0, 'published'), "
        "('h4', 'Текущая находка прогона', 'https://example.test/current', 'sberbank', 1, 'published')"
    ))
    section = _similar_known_records_section(
        session,
        query="Найди лазейки по кредиткам Сбербанка",
        records=[{"url": "https://example.test/current", "bank_slug": "sberbank"}],
    )
    assert "Обход грейс-периода" in section
    assert "https://example.test/known-grace" in section
    # Посторонние банки, не-лазейки и URL текущих находок в раздел не попадают.
    assert "known-wallet" not in section
    assert "example.test/other" not in section
    assert "example.test/current" not in section


@pytest.mark.asyncio
async def test_search_blocked_until_read_and_reopens_on_exhaustion(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools
    from bank_audit.loophole.run_budget import READ_NUDGE_AFTER

    ctx = context()
    search_calls = []

    async def network_search(*args, **kwargs):
        search_calls.append(True)
        return [{"title": "Зацепка", "url": f"https://example.test/serp/{len(search_calls)}"}]

    monkeypatch.setattr(tools, "run_blocking_network", network_search)
    search_tool = tools.AuditWebSearchTool(tools.ToolContext(
        "analyst", 1, None, budget=ctx.budget, fetched_sources=ctx.fetched_sources,
    ))
    for _ in range(READ_NUDGE_AFTER):
        result = json.loads(await search_tool.execute(f"запрос {len(search_calls)}"))
        assert isinstance(result, list)

    # Гейт: модель физически не может продолжать поиск без чтения страниц.
    blocked = json.loads(await search_tool.execute("очередной кластер"))
    assert blocked["error"] == "read_required"
    assert blocked["unread_sources"][0]["url"] == "https://example.test/serp/1"
    assert "audit_web_fetch" in blocked["next_step"]
    assert "очередной кластер" not in ctx.budget.search_cache
    assert len(search_calls) == READ_NUDGE_AFTER  # сеть не тратится на заблокированные

    async def network_fetch(*args):
        return {"url": "https://example.test/serp/1", "final_url": "https://example.test/serp/1",
                "title": "Прочитанная страница", "excerpt": "Непустой извлечённый текст"}

    monkeypatch.setattr(tools, "run_blocking_network", network_fetch)
    fetch_tool = tools.AuditWebFetchTool(tools.ToolContext(
        "analyst", 1, None, query=ctx.query, budget=ctx.budget,
        fetched_sources=ctx.fetched_sources,
    ))
    await fetch_tool.execute("https://example.test/serp/1")
    assert ctx.budget.searches_since_read == 0

    monkeypatch.setattr(tools, "run_blocking_network", network_search)
    recovered = json.loads(await search_tool.execute("новый кластер после чтения"))
    assert isinstance(recovered, list)

    # Второй гейт открывается, когда весь непрочитанный пул провалился:
    # deadlock невозможен, поиск продолжается легитимно.
    ctx.budget.searches_since_read = 0
    for index in range(READ_NUDGE_AFTER):
        result = json.loads(await search_tool.execute(f"шторм {index}"))
        assert isinstance(result, list)
    blocked_again = json.loads(await search_tool.execute("снова шторм"))
    assert blocked_again["error"] == "read_required"
    for source in ctx.budget.search_results:
        url = source.get("url")
        if url and url not in ctx.fetched_sources:
            ctx.budget.source_failures[url] = "source_unavailable"
    reopened = json.loads(await search_tool.execute("после исчерпания пула"))
    assert isinstance(reopened, list)
    # Гейт открылся (сброс) и легитимный поиск учтён в счётчике заново.
    assert ctx.budget.searches_since_read == 1


def test_model_state_reports_pages_and_unread_with_next_step():
    from bank_audit.loophole.run_budget import READ_NUDGE_AFTER

    ctx = context()
    ctx.budget.search_results = [
        {"title": "Непрочитанный материал", "url": "https://example.test/unread"},
    ]
    ctx.budget.searches_since_read = READ_NUDGE_AFTER
    managed = ManagedAgent(ctx, SimpleNamespace(), "")
    messages = []
    managed._update_model_state(SimpleNamespace(messages=messages))
    content = messages[0]["content"]
    state = json.loads(
        content.split("Учитывай кандидатов в отчёте; они требуют проверки аудитора.\n", 1)[1]
    )
    assert state["pages_read"] == 0
    assert state["pages_limit"] == 100
    assert state["unread_sources"][0]["url"] == "https://example.test/unread"
    assert "next_step" in state


@pytest.mark.asyncio
async def test_sse_triaged_subagent_items_persist_to_catalog(monkeypatch, session):
    from sqlalchemy import text

    from bank_audit.loophole.chat import graph
    from bank_audit.loophole.chat.subagents import ResearchSubagents
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)

    class Factory:
        def create(self, supplied, **kwargs):
            from dataclasses import replace

            ctx = replace(supplied, budget=ResearchBudget(timeout_seconds=300))
            subagents = ResearchSubagents(ctx.budget)
            subagents.triaged.append({
                "url": "https://example.ru/triaged", "title": "Зацепка подзадачи",
                "snippet": "Сниппет со признаками схемы", "category": "fraud",
                "content_type": "post", "reason": "Признаки обмана",
            })
            ctx = replace(ctx, subagents=subagents)

            class Bot:
                async def stream(self, prompt, **kwargs):
                    yield SimpleNamespace(type="test.iteration")

                async def aclose(self):
                    pass

            return ManagedAgent(ctx, Bot(), "")

    monkeypatch.setattr(graph, "AgentFactory", Factory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)
    [e async for e in graph.stream_chat({
        "query": "Найди лазейки за 2026 год", "workspace_id": 1, "user_id": "analyst",
        "clarification_verified": True,
    }, session=session)]
    row = session.execute(text(
        "SELECT status, classification, verdict_model, content_status "
        "FROM loophole_record WHERE url = 'https://example.ru/triaged'"
    )).mappings().one_or_none()
    assert row is not None
    assert row["status"] == "preliminary"
    assert row["classification"] == "fraud_scheme"
    assert row["verdict_model"] == "subagent_triage"
    assert row["content_status"] == "legacy"
