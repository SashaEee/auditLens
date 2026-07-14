import json

import pytest
from bank_audit.loophole.chat.tools_nanobot import (
    NANOBOT_TOOLS,
    _tool_result,
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


@pytest.mark.asyncio
async def test_extract_loopholes_returns_empty_on_empty_text():
    from bank_audit.loophole.chat.tools_nanobot import extract_loopholes

    assert await extract_loopholes("") == []


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
    assert isinstance(await extract_tool.execute("text"), str)
    assert isinstance(await save_tool.execute("t", "http://x", "s"), str)
    assert isinstance(await db_query_tool.execute("SELECT 1"), str)
    assert isinstance(await table_load_tool.execute(), str)
    assert isinstance(await export_tool.execute([{"id": 1}]), str)
