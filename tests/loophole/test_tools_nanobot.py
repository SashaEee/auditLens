import json
from datetime import date
from types import SimpleNamespace

import pytest

from bank_audit.loophole.chat.tools_nanobot import (
    NANOBOT_TOOLS,
    _tool_result,
    load_prompt,
    web_fetch,
    web_search,
)


def test_nanobot_tools_have_unique_names():
    names = [cls().name for cls in NANOBOT_TOOLS]
    assert len(names) == len(set(names))
    assert "audit_web_search" in names
    assert "audit_db_query" in names
    assert "audit_save_loophole" in names


def test_web_search_returns_empty_for_empty_query():
    assert web_search("") == []


def test_web_fetch_with_bad_url_returns_none(monkeypatch):
    monkeypatch.setattr(
        "bank_audit.loophole.adapters.fetch_decorator.fetch_and_parse",
        lambda *a, **k: None,
    )
    assert web_fetch("http://bad.url") is None


def test_web_fetch_returns_source_publication_timestamp(monkeypatch):
    page = SimpleNamespace(
        url="https://example.test/source",
        final_url="https://example.test/source",
        status=200,
        title="Источник",
        excerpt="Текст",
        via="http",
        published_at="2026-08-27T09:25:00+03:00",
    )
    monkeypatch.setattr(
        "bank_audit.loophole.adapters.fetch_decorator.fetch_and_parse",
        lambda *args, **kwargs: page,
    )

    assert web_fetch("https://example.test/source")["published_at"] == page.published_at


def test_nanobot_prompt_forbids_widening_a_hard_publication_period():
    prompt = load_prompt("07_nanobot_system")

    assert "published_at" in prompt
    assert "не расширяй период" in prompt
    assert "не подставляй Сбербанк" in prompt
    assert "закрытый календарный интервал" in prompt
    assert "службы внутреннего аудита Сбербанка" not in prompt


def test_nanobot_prompt_requires_wide_separate_fraud_research():
    from bank_audit.loophole.chat.nanobot_agent import load_system_prompt

    prompt = load_prompt("07_nanobot_system")

    assert "независимым кластерам запросов" in prompt
    assert "разным площадкам" in prompt
    assert "дедуплицируй URL" in prompt
    assert "финальный URL для исключения повторов" in prompt
    assert "насколько позволяет агентский контекст" in prompt
    assert "В пределах доступного лимита итераций" in prompt
    assert "до покрытия нескольких независимых кластеров и разных доступных площадок" in prompt
    assert "честно отметь это ограничение" in prompt
    assert "Мошеннические схемы" in prompt
    assert "релевантного проверенного источника" in prompt
    assert "не объявляй форум первоисточником" in prompt
    assert "не передавай мошеннические материалы" in prompt
    assert "audit_extract_loopholes" in prompt
    assert "published_at" in prompt
    assert "не расширяй период" in prompt
    assert load_system_prompt() == prompt


def test_publication_window_understands_user_month_constraint():
    from bank_audit.loophole.chat import tools_nanobot

    assert tools_nanobot._publication_window(
        "Найди лазейки по продукту кредитная карта за август 2026 года. "
        "Если дата поста раньше августа 2026 года, не выводи её."
    ) == (date(2026, 8, 1), date(2026, 9, 1))


@pytest.mark.asyncio
async def test_web_fetch_tool_hides_source_outside_requested_publication_month(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot

    page = SimpleNamespace(
        url="https://example.test/july",
        final_url="https://example.test/july",
        status=200,
        title="Июльский источник",
        excerpt="Схема",
        via="http",
        published_at="2026-07-31T23:59:00+03:00",
    )
    monkeypatch.setattr(tools_nanobot.fetch_decorator, "fetch_and_parse", lambda *a, **k: page)
    context = tools_nanobot.ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=object(),
        query="Найди лазейки по кредитной карте за август 2026 года",
    )

    result = json.loads(
        await tools_nanobot.AuditWebFetchTool(context=context).execute(page.url)
    )

    assert result["error"] == "source_outside_publication_period"
    assert result["published_at"] == page.published_at
    assert "excerpt" not in result


@pytest.mark.asyncio
async def test_extract_tool_denies_unverified_or_outside_source_date(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot

    async def forbidden_extract(_text):
        raise AssertionError("внепериодный источник не должен попасть в LLM extraction")

    monkeypatch.setattr(tools_nanobot, "extract_loopholes", forbidden_extract)
    source_url = "https://example.test/july"
    context = tools_nanobot.ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=object(),
        query="Найди лазейки по кредитной карте за август 2026 года",
        source_publication_dates={source_url: "2026-07-31T23:59:00+03:00"},
    )

    result = json.loads(
        await tools_nanobot.AuditExtractLoopholesTool(context=context).execute(
            "Текст источника",
            source_url=source_url,
        )
    )

    assert result == {"error": "source_outside_publication_period"}


@pytest.mark.asyncio
async def test_table_load_tool_applies_month_from_original_query(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot

    captured = {}
    monkeypatch.setattr(tools_nanobot, "_context_owns_workspace", lambda _context: True)
    monkeypatch.setattr(
        tools_nanobot,
        "table_load",
        lambda **kwargs: captured.update(kwargs) or [],
    )
    context = tools_nanobot.ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=object(),
        query="Найди лазейки по кредитной карте за август 2026 года",
    )

    await tools_nanobot.AuditTableLoadTool(context=context).execute()

    assert captured["period_from"] == date(2026, 8, 1)
    assert captured["period_to"] == date(2026, 8, 31)


@pytest.mark.asyncio
async def test_extract_loopholes_returns_empty_on_empty_text():
    from bank_audit.loophole.chat.tools_nanobot import extract_loopholes

    assert await extract_loopholes("") == []


@pytest.mark.asyncio
async def test_extract_tool_queues_confirmed_finding_for_server_persistence(monkeypatch, session):
    """Инструмент извлечения передаёт подтверждённую находку серверу, не записывая БД сам."""
    from bank_audit.loophole.chat import tools_nanobot

    async def fake_extract(_text):
        return [
            {
                "title": "Обход комиссии",
                "description": "Описание механизма",
                "category": "Комиссии",
                "severity": "high",
                "evidence_quote": "Подтверждающая цитата",
                "is_loophole": True,
            },
            {
                "title": "Не лазейка",
                "description": "Штатная функция",
                "category": "",
                "severity": "low",
                "evidence_quote": "",
                "is_loophole": False,
            },
        ]

    monkeypatch.setattr(tools_nanobot, "extract_loopholes", fake_extract)
    context = tools_nanobot.ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=session,
        fetched_sources={
            "https://example.ru/source": {
                "url": "https://example.ru/source",
                "title": "Проверенный источник",
                "extracted_text": "Текст источника",
                "published_at": "2026-08-27T09:25:00+03:00",
            }
        },
    )

    result = await tools_nanobot.AuditExtractLoopholesTool(context=context).execute(
        "Текст источника",
        source_url="https://example.ru/source",
        bank_slug="sberbank",
    )

    assert json.loads(result)[0]["title"] == "Обход комиссии"
    assert context.pending_records == [
        {
            "title": "Обход комиссии",
            "url": "https://example.ru/source",
            "snippet": "Подтверждающая цитата",
            "bank_slug": "sberbank",
            "raw_text": "Текст источника",
            "source_title": "Проверенный источник",
            "published_at": "2026-08-27T09:25:00+03:00",
            "description": "Описание механизма",
            "category": "Комиссии",
            "severity": "high",
            "is_loophole": True,
        }
    ]


def test_tool_result_serializes_non_strings():
    assert _tool_result("plain") == "plain"
    assert json.loads(_tool_result([{"a": 1}])) == [{"a": 1}]
    assert json.loads(_tool_result({"b": 2})) == {"b": 2}
    assert _tool_result(None) == "null"


@pytest.mark.asyncio
async def test_save_loophole_persists_record(session):
    from bank_audit.loophole.chat.tools_nanobot import save_loophole

    result = save_loophole(
        title="скрытая комиссия",
        url="https://example.ru/offer",
        snippet="комиссия за досрочное погашение",
        bank_slug="sberbank",
        keyword="комиссия",
        session=session,
    )
    assert result["is_new"] is True
    assert result["record_id"] is not None
    # Повторный вызов дедуп
    result2 = save_loophole(
        title="скрытая комиссия",
        url="https://example.ru/offer",
        snippet="комиссия за досрочное погашение",
        session=session,
    )
    assert result2["is_new"] is False
    assert result2["record_id"] == result["record_id"]
    assert result2["sha256"] == result["sha256"]


def test_save_loophole_accepts_legacy_page_without_text(monkeypatch, session):
    """Старый page-double без text использует безопасный excerpt fallback."""
    from bank_audit.loophole import content_fetch
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.chat.tools_nanobot import save_loophole

    legacy_page = SimpleNamespace(excerpt="Текст старого fetch double")
    monkeypatch.setattr(
        content_fetch.fetch_decorator,
        "fetch_and_parse",
        lambda *args, **kwargs: legacy_page,
    )

    result = save_loophole(
        title="legacy",
        url="https://example.ru/legacy",
        snippet="Текст старого fetch double",
        session=session,
    )

    assert result["is_new"] is True
    record = repo.get_record(result["record_id"], session=session)
    assert record["raw_text"] == "Текст старого fetch double"


@pytest.mark.asyncio
async def test_tool_executes_return_strings(monkeypatch):
    """Результаты кастомных tools должны быть строками, иначе nanobot сохранит
    list/dict в сессии и при следующем запросе сломает мультимодальный content."""
    from bank_audit.loophole.chat.tools_nanobot import (
        AuditDbQueryTool,
        AuditExportTool,
        AuditExtractLoopholesTool,
        AuditSaveLoopholeTool,
        AuditTableLoadTool,
        AuditWebFetchTool,
        AuditWebSearchTool,
    )

    monkeypatch.setattr(
        "bank_audit.loophole.adapters.search_decorator.search",
        lambda *a, **k: [{"title": "t", "url": "u", "snippet": "s", "domain": "d"}],
    )
    monkeypatch.setattr(
        "bank_audit.loophole.adapters.fetch_decorator.fetch_and_parse",
        lambda *a, **k: None,
    )

    web_search_tool = AuditWebSearchTool()
    web_fetch_tool = AuditWebFetchTool()
    extract_tool = AuditExtractLoopholesTool()
    save_tool = AuditSaveLoopholeTool()
    db_query_tool = AuditDbQueryTool()
    table_load_tool = AuditTableLoadTool()
    export_tool = AuditExportTool()

    assert isinstance(await web_search_tool.execute("q"), str)
    assert isinstance(await web_fetch_tool.execute("http://x"), str)
    assert isinstance(await extract_tool.execute("text", "https://x"), str)
    assert isinstance(await save_tool.execute("t", "http://x", "s"), str)
    assert isinstance(await db_query_tool.execute("SELECT 1"), str)
    assert isinstance(await table_load_tool.execute(), str)
    assert isinstance(await export_tool.execute([{"id": 1}]), str)


# ── heal-tools: fetch_target / patch_parser ─────────────────────────────────
def test_fetch_target_success(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot

    class _FakeCollector:
        def __init__(self, **kw):
            pass

        def fetch(self, url):
            return 200, b"<html>page content</html>"

        def close(self):
            pass

    monkeypatch.setattr(tools_nanobot, "_http_collector", lambda **kw: _FakeCollector())
    out = tools_nanobot.fetch_target("https://a.ru")
    assert out["ok"] is True
    assert out["status"] == 200
    assert "page content" in out["excerpt"]


def test_fetch_target_failure(monkeypatch):
    from bank_audit.loophole.chat import tools_nanobot

    class _FailCollector:
        def __init__(self, **kw):
            pass

        def fetch(self, url):
            raise RuntimeError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr(tools_nanobot, "_http_collector", lambda **kw: _FailCollector())
    out = tools_nanobot.fetch_target("https://down.ru")
    assert out["ok"] is False
    assert "connection refused" in out["error"]


def test_patch_parser_validates_syntax(session):
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.chat import tools_nanobot

    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "p", "", session=session)
    out = tools_nanobot.patch_parser(pid, "def broken(:", session=session)
    assert out["patched"] is False
    assert "syntax" in out["error"]


def test_patch_parser_writes_atomically(session, tmp_path):
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.chat import tools_nanobot

    code = tmp_path / "parser_1_x.py"
    code.write_text("OLD = 1\n", encoding="utf-8")
    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "p", str(code), session=session)
    out = tools_nanobot.patch_parser(pid, "NEW = 2\n", session=session)
    assert out["patched"] is True
    assert code.read_text(encoding="utf-8") == "NEW = 2\n"
    assert not (tmp_path / "parser_1_x.py.tmp").exists()


def test_patch_parser_not_found(session):
    from bank_audit.loophole.chat import tools_nanobot
    out = tools_nanobot.patch_parser(9999, "x = 1\n", session=session)
    assert out["patched"] is False
