"""Стабильность агента «Лазейки»: ретраи транзиентов, таймауты, мягкий period-фильтр."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bank_audit.loophole.run_budget import ResearchBudget


def _context():
    from bank_audit.loophole.agent import AgentRunContext

    return AgentRunContext("analyst", 1, "Лазейки за 2026 год", "stability-test",
                           budget=ResearchBudget(timeout_seconds=300))


# ── CAP-1: транзиентный сбой основной модели ретраится SDK ──────────────────
@pytest.mark.asyncio
async def test_main_model_transient_error_retried_and_answer_delivered(
    monkeypatch, tmp_path, caplog,
):
    from bank_audit.loophole.agent import ManagedAgent
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot
    from nanobot.providers.base import LLMResponse

    ctx = _context()
    calls = []
    bot, path = create_nanobot(workspace=tmp_path)

    async def provider(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            return LLMResponse(content="Error calling llm: connection error secret",
                               finish_reason="error")
        return LLMResponse(content="Итоговый отчёт")

    async def heartbeat(delay, **kwargs):
        return None

    monkeypatch.setattr(bot._loop.provider, "_safe_chat", provider)
    monkeypatch.setattr(bot._loop.provider, "_safe_chat_stream", provider)
    monkeypatch.setattr(bot._loop.provider, "_sleep_with_heartbeat", heartbeat)
    result = await ManagedAgent(ctx, bot, path).run()
    assert calls == [True, True]
    assert not result.partial and result.answer == "Итоговый отчёт"
    assert "giving up" not in caplog.text
    assert "secret" not in result.answer


# ── CAP-2: connect-timeout независим от read-таймаута ───────────────────────
@pytest.mark.asyncio
async def test_main_model_client_connect_and_read_timeouts(tmp_path):
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot

    bot, path = create_nanobot(workspace=tmp_path, connect_timeout_seconds=10,
                               read_timeout_seconds=600)
    try:
        bot._loop.provider._build_client()
        timeout = bot._loop.provider._client.timeout
        assert timeout.connect == 10
        assert timeout.read == 600
    finally:
        await bot.aclose()
        Path(path).unlink(missing_ok=True)

    # Явный 0 (disable_model_timeouts): read отключён, connect сохраняется.
    bot, path = create_nanobot(workspace=tmp_path, disable_model_timeouts=True,
                               connect_timeout_seconds=10)
    try:
        bot._loop.provider._build_client()
        timeout = bot._loop.provider._client.timeout
        assert timeout.connect == 10
        assert timeout.read is None
    finally:
        await bot.aclose()
        Path(path).unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("env_value,read,disabled", [("600", 600, False), ("0", None, True)])
async def test_factory_passes_connect_and_read_timeouts(
    monkeypatch, tmp_path, env_value, read, disabled,
):
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
    monkeypatch.setenv("LOOPHOLE_MODEL_TIMEOUT_SECONDS", env_value)
    managed = agent.AgentFactory().create(
        agent.AgentRunContext("u", 1, "карты", f"timeout-wiring-{env_value}"))
    try:
        assert captured["connect_timeout_seconds"] == 10
        assert captured["read_timeout_seconds"] == read
        assert captured["disable_model_timeouts"] is disabled
    finally:
        await managed.aclose()


# ── CAP-3: HTTP read-таймаут классификатора subagent и ретрай порции ────────
def _label_bot(items=None, error=None):
    runs = []

    class Bot:
        async def run(self, *args, **kwargs):
            runs.append(True)
            if error is not None:
                raise error
            return SimpleNamespace(content=json.dumps({"items": items}))

        async def aclose(self):
            pass

    return Bot(), runs


@pytest.mark.asyncio
async def test_subagent_classifier_has_http_read_timeout_below_batch_deadline(
    monkeypatch, tmp_path,
):
    from bank_audit.loophole.chat import subagents as sub

    monkeypatch.setenv("LLM_MODEL_FAST", "junior")
    monkeypatch.delenv("LOOPHOLE_SUBAGENT_READ_TIMEOUT_SECONDS", raising=False)
    captured = {}

    async def search(*args, **kwargs):
        return [{"url": "https://example.ru/a", "title": "Материал", "snippet": "Описание"}]

    bot, _runs = _label_bot(items=[{
        "id": 0, "category": "loophole", "content_type": "article", "reason": "Обоснование",
    }])
    config = tmp_path / "child.json"
    config.write_text("{}")

    def factory(**kwargs):
        captured.update(kwargs)
        return bot, str(config)

    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr("bank_audit.loophole.chat.nanobot_agent.create_nanobot", factory)
    result = await sub.ResearchSubagents(ResearchBudget()).research(["карты"], max_results=1)
    assert result["subagents"][0]["status"] == "completed"
    assert captured["connect_timeout_seconds"] == 10
    assert captured["read_timeout_seconds"] == 60  # дефолт < дедлайна порции 180 с


@pytest.mark.asyncio
async def test_stalled_classifier_batch_is_retried_before_timeout_code(
    monkeypatch, tmp_path,
):
    from bank_audit.loophole.chat import subagents as sub

    monkeypatch.setenv("LLM_MODEL_FAST", "junior")

    async def search(*args, **kwargs):
        return [{"url": "https://example.ru/a", "title": "Материал", "snippet": "Описание"}]

    bot, runs = _label_bot(error=TimeoutError("read timeout secret"))
    config = tmp_path / "child.json"
    config.write_text("{}")
    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr("bank_audit.loophole.chat.nanobot_agent.create_nanobot",
                        lambda **kwargs: (bot, str(config)))
    result = await sub.ResearchSubagents(ResearchBudget()).research(["карты"], max_results=1)
    event = result["subagents"][0]
    assert event["status"] == "failed" and event["error_code"] == "timeout"
    assert len(runs) == 3  # порция ретраинута recovery-цепочкой
    assert "secret" not in json.dumps(result)


# ── CAP-4: иерархия даты публикации exact → оценочная → допуск ──────────────
@pytest.mark.parametrize("url,expected", [
    ("https://example.ru/2026/09/09/post", "2026-09-09"),
    ("https://example.ru/2026/09/post", "2026-09-01"),
    ("https://example.ru/news/2026-09-09-slug", "2026-09-09"),
    ("https://example.ru/post", None),
])
def test_estimated_date_from_url(url, expected):
    from bank_audit.loophole.adapters.fetch_decorator import estimate_published_date

    assert estimate_published_date(url) == expected


@pytest.mark.parametrize("text,expected", [
    ("Опубликовано 9 сентября 2026 года", "2026-09-09"),
    ("Дата: 09.09.2026", "2026-09-09"),
    ("2026-09-09T10:00:00", "2026-09-09"),
    ("Без даты", None),
])
def test_estimated_date_from_text(text, expected):
    from bank_audit.loophole.adapters.fetch_decorator import estimate_published_date

    assert estimate_published_date("https://example.ru/post", text) == expected


def test_period_filter_hierarchy_exact_estimated_none():
    from bank_audit.loophole.chat import tools_nanobot as tools

    query = "Найди лазейки по кредитной карте за август 2026 года"
    url = "https://example.test/post"

    def context(published, estimated):
        return tools.ToolContext("analyst", 1, object(), query=query,
                                 source_publication_dates={url: published},
                                 source_estimated_dates={url: estimated})

    # Оценочная дата из URL/текста в окне — источник участвует.
    assert tools._source_publication_period_error(context(None, "2026-08-12"), url) is None
    # Оценка вне окна — отклонение.
    assert tools._source_publication_period_error(
        context(None, "2026-07-12"), url) == "source_outside_publication_period"
    # Подтверждённая дата важнее оценочной и по-прежнему fail-closed вне окна.
    assert tools._source_publication_period_error(
        context("2026-07-31T23:59:00+03:00", "2026-08-12"), url,
    ) == "source_outside_publication_period"
    # Подтверждённая дата в окне — допуск.
    assert tools._source_publication_period_error(
        context("2026-08-01T00:00:00+03:00", None), url) is None
    # Без любой даты — допуск с пометкой неподтверждённой даты.
    assert tools._source_publication_period_error(context(None, None), url) is None
    # Naive-дата без timezone тоже подтверждённая: в окне — допуск.
    assert tools._source_publication_period_error(context("2026-08-01", None), url) is None
    # Naive-дата вне окна — отклонение.
    assert tools._source_publication_period_error(
        context("2026-07-01", None), url) == "source_outside_publication_period"


@pytest.mark.asyncio
async def test_fetch_source_with_estimated_date_participates_in_extraction(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    page = SimpleNamespace(
        url="https://example.test/2026/08/scheme",
        final_url="https://example.test/2026/08/scheme",
        status=200,
        title="Августовский источник",
        excerpt="Схема",
        via="http",
        published_at=None,
        estimated_published_at="2026-08-12",
    )
    monkeypatch.setattr(tools.fetch_decorator, "fetch_and_parse", lambda *a, **k: page)
    context = tools.ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=object(),
        query="Найди лазейки по кредитной карте за август 2026 года",
    )
    result = json.loads(await tools.AuditWebFetchTool(context=context).execute(page.url))
    assert result["excerpt"] == "Схема" and "error" not in result
    assert context.source_estimated_dates[page.url] == "2026-08-12"


# ── CAP-5: цепочка резолва subagent-модели до LLM_MODEL_NAME ────────────────
@pytest.mark.asyncio
async def test_subagent_uses_main_model_name_as_last_resort(monkeypatch, tmp_path):
    from bank_audit.loophole.chat import subagents as sub

    monkeypatch.delenv("LOOPHOLE_SUBAGENT_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_FAST", raising=False)
    monkeypatch.setenv("LLM_MODEL_NAME", "main-model")
    seen = {}

    async def search(*args, **kwargs):
        return [{"title": "Пост", "url": "https://example.ru/post", "snippet": "Описание"}]

    bot, _runs = _label_bot(items=[{
        "id": 0, "category": "irrelevant", "content_type": "post", "reason": "Обычная жалоба",
    }])

    def factory(**kwargs):
        seen.update(kwargs)
        path = tmp_path / "child.json"
        path.write_text("{}")
        return bot, str(path)

    monkeypatch.setattr(sub, "run_blocking_network", search)
    monkeypatch.setattr("bank_audit.loophole.chat.nanobot_agent.create_nanobot", factory)
    result = await sub.ResearchSubagents(ResearchBudget()).research(["карты"], max_results=1)
    assert seen["model"] == "main-model"
    assert result["subagents"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_subagent_empty_model_chain_stays_fail_closed(monkeypatch):
    from bank_audit.loophole.chat import subagents as sub

    monkeypatch.delenv("LOOPHOLE_SUBAGENT_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_FAST", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    result = await sub.ResearchSubagents(ResearchBudget()).research(["карты"])
    assert result["error"] == "subagent_model_not_configured"


# ── CAP-6: ретраи транзиентов инструментов до fail-closed кодов ─────────────
@pytest.mark.asyncio
async def test_web_fetch_transient_error_retried_and_source_not_marked(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    monkeypatch.setattr(tools, "_TOOL_RETRY_DELAYS", (0, 0))
    calls = []
    page = {"url": "https://example.test/ok", "excerpt": "Текст", "published_at": None,
            "estimated_published_at": None}

    async def network(fn, *args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ConnectionError("connection reset secret")
        return page

    monkeypatch.setattr(tools, "run_blocking_network", network)
    budget = ResearchBudget(timeout_seconds=300)
    context = tools.ToolContext("analyst", 1, object(), budget=budget)
    result = json.loads(await tools.AuditWebFetchTool(context).execute("https://example.test/ok"))
    assert calls == [True, True]
    assert result["excerpt"] == "Текст"
    assert budget.source_failures == {}
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_web_search_transient_error_retried(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    monkeypatch.setattr(tools, "_TOOL_RETRY_DELAYS", (0, 0))
    calls = []

    async def network(fn, *args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise ConnectionError("connection reset secret")
        return [{"title": "Материал", "url": "https://example.test/lead"}]

    monkeypatch.setattr(tools, "run_blocking_network", network)
    budget = ResearchBudget(timeout_seconds=300)
    context = tools.ToolContext("analyst", 1, object(), budget=budget)
    result = json.loads(await tools.AuditWebSearchTool(context).execute("карты"))
    assert calls == [True, True]
    assert result[0]["url"] == "https://example.test/lead"
    assert budget.search_cache["карты"] == result


@pytest.mark.asyncio
async def test_extraction_transient_error_retried(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    monkeypatch.setattr(tools, "_EXTRACTION_RETRY_DELAYS", (0, 0))
    calls = []

    class LLM:
        async def ainvoke(self, messages):
            calls.append(True)
            if len(calls) == 1:
                raise ConnectionError("connection reset secret")
            return SimpleNamespace(content='{"loopholes": []}')

    findings = await tools.extract_loopholes("Текст статьи", llm=LLM())
    assert findings == [] and calls == [True, True]


@pytest.mark.asyncio
async def test_extraction_non_transient_error_not_retried(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot as tools

    monkeypatch.setattr(tools, "_EXTRACTION_RETRY_DELAYS", (0, 0))
    calls = []

    class LLM:
        async def ainvoke(self, messages):
            calls.append(True)
            raise ValueError("bad request secret")

    with pytest.raises(RuntimeError, match="extraction_failed"):
        await tools.extract_loopholes("Текст", llm=LLM())
    assert calls == [True]


# ── CAP-1 (clarify): ретраи транзиентов до fail-closed ──────────────────────
def _clarify_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


@pytest.mark.asyncio
async def test_clarify_transient_error_retried_before_answer(monkeypatch):
    from bank_audit.loophole.chat import clarify

    monkeypatch.setattr(clarify, "_CLARIFY_RETRY_DELAYS", (0, 0))
    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise ConnectionError("connection error secret")
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"complete": true, "reason": "ok"}'))])

    monkeypatch.setattr(clarify, "_client", lambda: _clarify_client(Completions()))
    result = await clarify.generate_clarifications("лазейки по картам")
    assert calls == [True, True]
    assert result["complete"] is True


@pytest.mark.asyncio
async def test_clarify_exhausted_retries_stay_fail_closed(monkeypatch):
    from bank_audit.loophole.chat import clarify

    monkeypatch.setattr(clarify, "_CLARIFY_RETRY_DELAYS", (0, 0))
    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(True)
            raise ConnectionError("connection error secret")

    monkeypatch.setattr(clarify, "_client", lambda: _clarify_client(Completions()))
    result = await clarify.generate_clarifications("лазейки по картам")
    assert calls == [True, True, True]
    assert result["reason"] == "clarification_unavailable"


@pytest.mark.asyncio
async def test_factory_default_model_timeout_is_600(monkeypatch, tmp_path):
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
    monkeypatch.delenv("LOOPHOLE_MODEL_TIMEOUT_SECONDS", raising=False)
    managed = agent.AgentFactory().create(
        agent.AgentRunContext("u", 1, "карты", "timeout-default-600"))
    try:
        assert captured["read_timeout_seconds"] == 600
        assert captured["disable_model_timeouts"] is False
    finally:
        await managed.aclose()


@pytest.mark.asyncio
async def test_subagent_classifier_transient_error_retried_and_labels_parsed(monkeypatch):
    from nanobot.providers.base import LLMResponse
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    from bank_audit.loophole.chat import subagents as sub

    calls = []

    async def response(self, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return LLMResponse(content="Error calling llm: connection error",
                               finish_reason="error")
        return LLMResponse(content=json.dumps({"items": [{
            "id": 0, "category": "loophole", "content_type": "article",
            "reason": "Схема с грейс-периодом"}]}))

    async def heartbeat(self, delay, **kwargs):
        return None

    monkeypatch.setattr(OpenAICompatProvider, "chat", response)
    monkeypatch.setattr(OpenAICompatProvider, "_sleep_with_heartbeat", heartbeat)
    result = await sub.ResearchSubagents(ResearchBudget())._classify([
        {"url": "https://example.test", "title": "Источник", "snippet": "Схема с картой"},
    ], "test-model")
    assert calls == [True, True]
    assert result[0]["category"] == "loophole"
