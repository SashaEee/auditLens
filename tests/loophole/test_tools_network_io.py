"""Отмена I/O, общий лимит потоков и принадлежность контекста event loop."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from bank_audit.loophole import network_io
from bank_audit.loophole.chat import tools_nanobot as tools
from bank_audit.loophole.run_budget import ResearchBudget


async def _until(predicate) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_search_runs_concurrently_without_blocking_heartbeat(monkeypatch):
    released = threading.Event()
    started: set[int] = set()
    owner = threading.get_ident()

    def search(query, **kwargs):
        assert threading.get_ident() != owner
        started.add(int(query))
        assert released.wait(2), "event loop не смог освободить сетевые чтения"
        return [{"title": query}]

    monkeypatch.setattr(tools, "web_search", search)
    tasks = [asyncio.create_task(tools.AuditWebSearchTool().execute(str(i))) for i in range(3)]
    try:
        await _until(lambda: len(started) == 3)
        assert not any(task.done() for task in tasks)
    finally:
        released.set()
        results = await asyncio.gather(*tasks)
    assert [json.loads(result)[0]["title"] for result in results] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_cancelled_workers_keep_global_slots_across_event_loops():
    released = threading.Event()
    started: set[int] = set()
    finished: set[int] = set()
    other_loop_started = threading.Event()
    other_loop_finished = threading.Event()
    other_loop_error: list[BaseException] = []

    def blocked(index):
        started.add(index)
        try:
            assert released.wait(3)
            return index
        finally:
            finished.add(index)

    async def other_call():
        other_loop_started.set()
        return await network_io.run_blocking_network(blocked, 99)

    def other_loop():
        try:
            asyncio.run(other_call())
        except BaseException as exc:  # noqa: BLE001 — передаём ошибку из второго loop в тест
            other_loop_error.append(exc)
        finally:
            other_loop_finished.set()

    tasks = [asyncio.create_task(network_io.run_blocking_network(blocked, i))
             for i in range(network_io.NETWORK_CONCURRENCY)]
    thread = threading.Thread(target=other_loop, daemon=True)
    try:
        await _until(lambda: len(started) == network_io.NETWORK_CONCURRENCY)
        for task in tasks:
            task.cancel()
        assert all(isinstance(value, asyncio.CancelledError)
                   for value in await asyncio.gather(*tasks, return_exceptions=True))
        thread.start()
        await _until(other_loop_started.is_set)
        await asyncio.sleep(0.03)
        assert 99 not in started, "отмена ожидания преждевременно освободила общий слот"
    finally:
        released.set()
        await _until(lambda: len(finished) >= network_io.NETWORK_CONCURRENCY)
        if thread.ident is not None:
            await _until(other_loop_finished.is_set)
            thread.join(timeout=0)
    assert not other_loop_error
    assert 99 in finished


@pytest.mark.asyncio
async def test_tool_timeout_cancels_wait_and_drops_late_fetch_result(monkeypatch):
    released = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(network_io, "NETWORK_TIMEOUT_SECONDS", 0.03)
    # Одиночная попытка: отмена ожидания и поздний результат не зависят от ретраев.
    monkeypatch.setattr(tools, "_TOOL_RETRY_DELAYS", ())

    def fetch(url):
        try:
            assert released.wait(2)
            return {"url": url, "excerpt": "Поздний источник", "published_at": None}
        finally:
            finished.set()

    monkeypatch.setattr(tools, "web_fetch", fetch)
    context = tools.ToolContext("analyst", 1, object())
    try:
        result = await tools.AuditWebFetchTool(context).execute("https://example.test/late")
        assert json.loads(result)["error"] == "source_unavailable"
        assert not finished.is_set()
    finally:
        released.set()
        await _until(finished.is_set)
    await asyncio.sleep(0)
    assert context.fetched_sources == {}
    assert context.source_publication_dates == {}
    assert context.pending_records == []


@pytest.mark.asyncio
async def test_completed_fetch_updates_context_only_in_owner_thread(monkeypatch):
    owner = threading.get_ident()

    class OwnerDict(dict):
        def __setitem__(self, key, value):
            assert threading.get_ident() == owner
            super().__setitem__(key, value)

    def fetch(url):
        assert threading.get_ident() != owner
        return {"url": url, "title": "Источник", "excerpt": "Прочитанный текст",
                "published_at": "2026-09-07T08:00:00+03:00"}

    monkeypatch.setattr(tools, "web_fetch", fetch)
    context = tools.ToolContext(
        "analyst", 1, object(), fetched_sources=OwnerDict(),
        source_publication_dates=OwnerDict(),
    )
    await tools.AuditWebFetchTool(context).execute("https://example.test/source")
    assert len(context.fetched_sources) == 1


@pytest.mark.asyncio
async def test_cancelled_fetch_may_fill_cache_using_its_own_worker_session(monkeypatch):
    """Служебный кэш независим от Session и жизненного цикла исследования."""
    from bank_audit import db
    from bank_audit.rag import cache

    released = threading.Event()
    finished = threading.Event()
    owner = threading.get_ident()
    started = threading.Event()
    events = []

    class CacheSession:
        def __init__(self):
            self.owner = threading.get_ident()
            assert self.owner != owner

        def execute(self, *args):
            assert threading.get_ident() == self.owner
            events.append("cache_write")

        def commit(self):
            assert threading.get_ident() == self.owner
            events.append("commit")

        def close(self):
            assert threading.get_ident() == self.owner
            events.append("close")

    def fetch(url):
        try:
            started.set()
            assert released.wait(2)
            cache.put("fetch", {"text": "Кэш"}, 60, url)
            return {"url": url, "excerpt": "Поздний источник", "published_at": None}
        finally:
            finished.set()

    monkeypatch.setattr(db, "_Session", CacheSession)
    monkeypatch.setattr(tools, "web_fetch", fetch)
    context = tools.ToolContext("analyst", 1, object())
    task = asyncio.create_task(tools.AuditWebFetchTool(context).execute("https://example.test"))
    try:
        await _until(started.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        released.set()
        await _until(finished.is_set)
    assert events == ["cache_write", "commit", "close"]
    assert context.fetched_sources == {}


@pytest.mark.parametrize("timestamp,estimated,error", [
    (None, None, None),
    ("2026-04-01", None, None),
    (None, "2026-06-15", None),
    (None, "2025-06-15", "source_outside_publication_period"),
    ("2025-12-31", None, "source_outside_publication_period"),
    ("2025-12-31T23:59:59+03:00", "2026-06-15", "source_outside_publication_period"),
    ("2027-01-01T00:00:00+03:00", None, "source_outside_publication_period"),
    ("2026-01-01T00:00:00+03:00", None, None),
    ("2026-12-31T23:59:59+03:00", None, None),
])
def test_requested_year_period_filter_date_hierarchy(timestamp, estimated, error):
    query = "Найди 1 лазейку по кредитным картам за 2026 год"
    context = tools.ToolContext("analyst", 1, object(), query=query,
                               source_publication_dates={"https://example.test": timestamp},
                               source_estimated_dates={"https://example.test": estimated})
    assert tools._publication_window(query) == (date(2026, 1, 1), date(2027, 1, 1))
    assert tools._source_publication_period_error(context, "https://example.test") == error


@pytest.mark.asyncio
async def test_extract_cannot_queue_result_after_budget_expires(monkeypatch):
    budget = ResearchBudget()
    source = {"url": "https://example.test", "extracted_text": "Цитата",
              "published_at": "2026-06-01T00:00:00+03:00", "title": "Источник"}
    context = tools.ToolContext("analyst", 1, object(), fetched_sources={source["url"]: source},
                               budget=budget)

    async def extract(text):
        budget.timeout_seconds = 1
        budget.started_at -= 2
        return [{"title": "Находка", "evidence_quote": "Цитата", "is_loophole": True}]

    monkeypatch.setattr(tools, "extract_loopholes", extract)
    with pytest.raises(TimeoutError):
        await tools.AuditExtractLoopholesTool(context).execute("Текст", source["url"])
    assert context.pending_records == []


@pytest.mark.asyncio
async def test_extraction_timeout_masks_input_and_propagates_external_cancellation(monkeypatch):
    seen = []
    cancelled = []

    class Llm:
        async def ainvoke(self, messages):
            seen.append(messages[-1].content)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    monkeypatch.setattr(tools, "_EXTRACTION_TIMEOUT_SECONDS", 0.02)
    # Одиночная попытка: маскирование и внешняя отмена не зависят от ретраев.
    monkeypatch.setattr(tools, "_EXTRACTION_RETRY_DELAYS", ())
    with pytest.raises(RuntimeError, match="extraction_failed"):
        await tools.extract_loopholes("Контакт auditor@example.test", llm=Llm())
    assert "auditor@example.test" not in seen[0]
    assert cancelled == [True]

    task = asyncio.create_task(tools.extract_loopholes("Текст", llm=Llm()))
    await _until(lambda: len(seen) == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_extraction_client_has_finite_transport_timeout_and_no_retries(monkeypatch):
    from bank_audit.loophole import direct_transport

    clients = {}

    def client(kind, **kwargs):
        clients[kind] = kwargs
        return object()

    monkeypatch.setattr(direct_transport, "sync_client", lambda **kw: client("sync", **kw))
    monkeypatch.setattr(direct_transport, "async_client", lambda **kw: client("async", **kw))
    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kwargs: SimpleNamespace(**kwargs))
    llm = tools._default_llm()
    assert llm.max_retries == 0
    assert llm.timeout == 45.0
    assert clients == {"sync": {"timeout": 45.0}, "async": {"timeout": 45.0}}


@pytest.mark.asyncio
async def test_extraction_rejects_source_replaced_while_llm_was_running(monkeypatch):
    source = {"url": "https://example.test", "extracted_text": "Прежний текст",
              "published_at": "2026-06-01T00:00:00+03:00", "title": "Источник"}
    context = tools.ToolContext("analyst", 1, object(), fetched_sources={source["url"]: source})

    async def extract(text):
        assert text == "Прежний текст"
        context.fetched_sources[source["url"]] = {**source, "extracted_text": "Новая версия"}
        return [{"title": "Находка", "evidence_quote": text, "is_loophole": True}]

    monkeypatch.setattr(tools, "extract_loopholes", extract)
    result = await tools.AuditExtractLoopholesTool(context).execute("Текст", source["url"])
    assert json.loads(result) == {"error": "source_changed_during_extraction"}
    assert context.pending_records == []


@pytest.mark.asyncio
async def test_extraction_uses_strict_boolean_and_closes_owned_http_clients(monkeypatch):
    closed = []

    class AsyncClient:
        async def aclose(self):
            closed.append("async")

    class Llm:
        http_async_client = AsyncClient()
        http_client = SimpleNamespace(close=lambda: closed.append("sync"))

        async def ainvoke(self, messages):
            return SimpleNamespace(content=json.dumps({"loopholes": [
                {"title": "Ложная находка", "is_loophole": "false"},
                {"title": "Находка", "is_loophole": True},
            ]}))

    monkeypatch.setattr(tools, "_default_llm", Llm)
    result = await tools.extract_loopholes("Текст источника")
    assert [item["is_loophole"] for item in result] == [False, True]
    assert closed == ["async", "sync"]
