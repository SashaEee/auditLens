"""Offline-проверки общего бюджета, остановки исследования и безопасного прогресса."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bank_audit.loophole.config import LoopholeSettings
from bank_audit.loophole.run_budget import ResearchBudget, requested_finding_count


def _context(*, count=1, timeout=0.06):
    from bank_audit.loophole.agent import AgentRunContext

    return AgentRunContext(
        "analyst", 1, "Найди 1 лазейку по кредитным картам за 2026 год", "budget-test",
        budget=ResearchBudget(timeout_seconds=timeout, requested_count=count),
    )


def _add_candidate(context, *, quote="Цитата о механизме", published_at="2026-03-01T10:00:00+03:00"):
    url = "https://example.test/source"
    context.fetched_sources[url] = {
        "url": url, "title": "Источник", "extracted_text": "Цитата о механизме. Остальной текст.",
        "published_at": published_at,
    }
    context.pending_records.append({
        "title": "Кандидат", "url": url, "snippet": quote, "evidence_quote": quote,
        "description": "Описание механизма", "published_at": published_at, "is_loophole": True,
        "raw_text": context.fetched_sources[url]["extracted_text"],
    })


@pytest.mark.parametrize(("query", "expected"), [
    ("Найди 1 лазейку по кредитным картам за 2026 год", 1),
    ("Найди одну лазейку", 1),
    ("Найди 3 проверенные лазейки", 3),
    ("Найди лазейки за 2026 год", None),
    ("Сравни кредитные карты", None),
    ("Найди более 1 лазейки", None),
    ("Найди не менее 5 лазеек", None),
    ("Найди минимум 1 лазейку", None),
    ("Найди 1 лазейку, но не ограничивайся этим; нужен широкий поиск", None),
    ("Уже есть 1 лазейка; продолжи широкий поиск", None),
])
def test_requested_count_is_explicit(query, expected):
    assert requested_finding_count(query) == expected


def test_budget_configuration(monkeypatch):
    monkeypatch.delenv("LOOPHOLE_AGENT_TIMEOUT_SECONDS", raising=False)
    assert LoopholeSettings.load().agent_timeout_seconds == 0
    monkeypatch.setenv("LOOPHOLE_AGENT_TIMEOUT_SECONDS", "45")
    assert LoopholeSettings.load().agent_timeout_seconds == 45


@pytest.mark.parametrize("value", ["-1", "nan", "forever"])
def test_budget_configuration_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("LOOPHOLE_AGENT_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="timeout"):
        LoopholeSettings.load()


def test_expired_budget_rejects_late_results():
    budget = ResearchBudget(timeout_seconds=1, started_at=time.monotonic() - 2)
    with pytest.raises(TimeoutError):
        budget.ensure_active()


def test_count_prompt_stops_after_evidence_without_changing_broad_queries():
    from bank_audit.loophole.chat.nanobot_agent import build_prompt

    narrow = build_prompt("Найди 1 лазейку по кредитным картам за 2026 год")
    broad = build_prompt("Найди лазейки по кредитным картам за 2026 год")
    assert "требуется AI-кандидатов — 1" in narrow
    assert "Ограничение текущего запроса:" not in broad
    assert "нескольких независимых кластеров" in broad


@pytest.mark.parametrize(("quote", "published_at", "expected"), [
    ("Цитата о механизме", "2026-03-01T10:00:00+03:00", 1),
    ("Фраза только из SERP", "2026-03-01T10:00:00+03:00", 0),
    ("Цитата о механизме", "2025-03-01T10:00:00+03:00", 0),
    # Без подтверждённой даты — допуск с пометкой неподтверждённой даты (CAP-4).
    ("Цитата о механизме", None, 1),
])
def test_quantity_counts_only_read_evidence_in_period(quote, published_at, expected):
    from bank_audit.loophole.agent import eligible_findings

    context = _context()
    _add_candidate(context, quote=quote, published_at=published_at)
    assert len(eligible_findings(context)) == expected


def test_quantity_rejects_description_fallback_and_duplicate_quote():
    from bank_audit.loophole.agent import eligible_findings

    context = _context()
    _add_candidate(context)
    context.pending_records.append(dict(context.pending_records[0]))
    assert len(eligible_findings(context)) == 1
    for finding in context.pending_records:
        finding.pop("evidence_quote")
    assert eligible_findings(context) == []


@pytest.mark.parametrize("quote", [
    "Комиссия по карте user@ не списывается.",
    "Клиент [PHONE_1] вернул комиссию.",
])
def test_evidence_uses_identical_redaction_for_quote_and_source(quote):
    from bank_audit.loophole.agent import eligible_findings

    context = _context()
    _add_candidate(context, quote=quote)
    context.fetched_sources["https://example.test/source"]["extracted_text"] = quote
    assert len(eligible_findings(context)) == 1


@pytest.mark.asyncio
async def test_stream_deadline_closes_runner_and_emits_only_safe_progress(monkeypatch):
    from bank_audit.loophole import agent
    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    monkeypatch.setattr(agent, "_PROGRESS_INTERVAL_SECONDS", 0.01)
    closed = []

    class Bot:
        async def stream(self, prompt, **kwargs):
            try:
                await kwargs["hooks"][0].on_stream(None, "Контакт user@example.test")
                await asyncio.sleep(30)
                raise AssertionError("Ответ после дедлайна недопустим")
                yield
            finally:
                closed.append("runner")

        async def aclose(self):
            closed.append("bot")

    context = _context()
    hook = AuditHook()
    started = time.monotonic()
    managed = agent.ManagedAgent(context, Bot(), "")
    events = [event async for event in managed.stream("query", hook=hook)]
    assert time.monotonic() - started < 0.5
    assert closed == ["runner", "bot"]
    assert hook.stop_reason == "time_budget"
    assert "time_budget" in hook.tool_errors
    assert "user@example.test" not in hook.final_answer
    assert len(events) > 1
    for event in events:
        mapped = _map_event(event, hook)
        assert mapped["event"] == "phase"
        assert set(mapped["data"]) == {"phase", "stage", "elapsed_seconds", "message"}


@pytest.mark.asyncio
async def test_run_deadline_is_partial_and_external_cancel_stays_cancelled():
    from bank_audit.loophole.agent import ManagedAgent

    closed = []

    class Bot:
        async def run(self, prompt, **kwargs):
            await asyncio.sleep(30)

        async def aclose(self):
            closed.append(True)

    result = await ManagedAgent(_context(), Bot(), "").run()
    assert result.stop_reason == "time_budget"
    assert result.partial and "общий бюджет" in result.answer
    context = _context(timeout=30)
    managed = ManagedAgent(context, Bot(), "")
    task = asyncio.create_task(managed.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.budget.cancelled
    assert closed == [True, True]


@pytest.mark.asyncio
async def test_quantity_hook_stops_before_next_model_call():
    from bank_audit.loophole.agent import ManagedAgent
    from bank_audit.loophole.chat.hooks import AuditHook

    context = _context(timeout=5)

    class Bot:
        async def stream(self, prompt, **kwargs):
            _add_candidate(context)
            await kwargs["hooks"][1].after_iteration(SimpleNamespace(iteration=0))
            raise AssertionError("Новый вызов модели после нужного числа кандидатов запрещён")
            yield

        async def aclose(self):
            pass

    hook = AuditHook()
    awaitable = ManagedAgent(context, Bot(), "").stream("query", hook=hook)
    _events = [event async for event in awaitable]
    assert hook.stop_reason == "requested_count"
    assert not hook.tool_errors
    assert "AI-кандидаты" in hook.final_answer
    assert "Цитата о механизме" in hook.final_answer
    assert "https://example.test/source" in hook.final_answer


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_consumer", [False, True])
async def test_real_nanobot_stream_cancels_provider_at_total_deadline(
    monkeypatch, tmp_path, pause_consumer,
):
    from bank_audit.loophole.agent import ManagedAgent
    from bank_audit.loophole.chat.hooks import AuditHook
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot

    started = asyncio.Event()
    cancelled = asyncio.Event()
    bot, config_path = create_nanobot(workspace=tmp_path / "nanobot")

    async def provider_wait(**kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    monkeypatch.setattr(bot._loop.provider, "chat_stream_with_retry", provider_wait)
    context = _context(timeout=1.0)
    managed = ManagedAgent(context, bot, config_path)
    hook = AuditHook()
    stream = managed.stream("проверка", hook=hook)
    if pause_consumer:
        await anext(stream)  # Безопасный progress до запуска runner.
        await anext(stream)  # Настоящее событие run.started от SDK.
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(context.budget.remaining_seconds() + 0.05)
        assert cancelled.is_set(), "SSE backpressure не должен продлевать работу провайдера"
    _events = [event async for event in stream]
    assert started.is_set() and cancelled.is_set()
    assert hook.stop_reason == "time_budget"
    assert time.monotonic() - context.budget.started_at < 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_sse_preserves_completed_candidate_on_deadline_but_not_external_cancel(
    monkeypatch, session, cancel,
):
    from sqlalchemy import text

    from bank_audit.loophole.agent import ManagedAgent
    from bank_audit.loophole.chat import graph
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(session)
    started = asyncio.Event()
    messages = []

    class Factory:
        def create(self, context, **kwargs):
            context = replace(context, budget=ResearchBudget(
                timeout_seconds=0.06, requested_count=2,
            ))

            class Bot:
                async def stream(self, prompt, **kwargs):
                    _add_candidate(context)
                    await kwargs["hooks"][0].on_stream(None, "Проверка: user@example.test")
                    started.set()
                    await asyncio.sleep(30)
                    yield

                async def aclose(self):
                    pass

            return ManagedAgent(context, Bot(), "")

    monkeypatch.setattr(graph, "AgentFactory", Factory)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(graph.repo, "add_chat_message", lambda *args, **kwargs: messages.append(args))

    async def collect():
        return [event async for event in graph.stream_chat({
            "query": "Найди 2 лазейки по кредитным картам за 2026 год",
            "workspace_id": 1, "user_id": "analyst", "clarification_verified": True,
        }, session=session)]

    task = asyncio.create_task(collect())
    if cancel:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not messages
        expected_candidates = 0
    else:
        events = await task
        assert any(event["event"] == "records" for event in events)
        assert any(event["data"].get("stop_reason") == "time_budget"
                   for event in events if event["event"] == "phase")
        assert len(messages) == 1
        answer = messages[0][2]
        assert "общий бюджет" in answer and "AI-кандидаты" in answer
        assert answer.count("Найденные AI-кандидаты") == 1
        assert "user@example.test" not in answer
        expected_candidates = 1
    # При дедлайне подтверждённый кандидат автоимпортируется в общий каталог,
    # при внешней отмене — нет.
    assert session.execute(
        text("SELECT count(*) FROM loophole_record")
    ).scalar_one() == expected_candidates
    assert session.execute(
        text("SELECT count(*) FROM loophole_research_candidate")
    ).scalar_one() == expected_candidates


@pytest.mark.asyncio
async def test_cleanup_timeout_with_paused_consumer_stays_partial(monkeypatch):
    from bank_audit.loophole import agent
    from bank_audit.loophole.chat.hooks import AuditHook

    monkeypatch.setattr(agent, "_CLEANUP_TIMEOUT_SECONDS", 0.02)
    closed = []

    class Bot:
        async def stream(self, prompt, **kwargs):
            try:
                yield SimpleNamespace(type="run.started")
                await asyncio.sleep(30)
            finally:
                try:
                    await asyncio.sleep(30)
                finally:
                    closed.append("runner")

        async def aclose(self):
            closed.append("bot")

    managed = agent.ManagedAgent(_context(timeout=0.03), Bot(), "")
    hook = AuditHook()
    stream = managed.stream("query", hook=hook)
    await anext(stream)
    await anext(stream)
    await asyncio.sleep(0.09)
    _events = [event async for event in stream]
    assert hook.stop_reason == "time_budget"
    assert set(hook.tool_errors) == {"time_budget", "cleanup_timeout"}
    assert closed == ["runner", "bot"]
