"""Тест collector: мок search → мок fetch → мок classify → проверка записей в БД."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from bank_audit.loophole import collector
from bank_audit.loophole import keywords as kw_mod
from bank_audit.loophole import repository as repo
from bank_audit.loophole.adapters import search_decorator
from bank_audit.loophole.config import LoopholeSettings


@pytest.fixture
def session(sqlite_session):
    return sqlite_session


def _search_impl_factory(results):
    def _impl(query, *, max_results=10, site_filter=None, region="ru-ru"):
        return results
    return _impl


def _fetch_impl_factory(text="текст документа со скрытой комиссией"):
    result = MagicMock()
    result.content = text.encode("utf-8")
    result.final_url = "https://example.ru/doc"
    result.status = 200
    result.content_type = "text/html"
    result.via = "http"
    def _impl(url, prefer_browser=False):
        return result
    return _impl


def _llm_mock(verdict):
    msg = MagicMock()
    msg.content = json.dumps(verdict, ensure_ascii=False)
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


@pytest.mark.asyncio
async def test_collect_once_inserts_records(session):
    kw_mod.seed_keywords(session=session)
    # Ограничим одним ключевым словом для детерминизма.
    kws = repo.list_keywords(session=session)
    for k in kws[1:]:
        repo.set_keyword_active(k["keyword_id"], False, session=session)

    search_results = [
        {"title": "лазейка в договоре Сбербанка", "url": "https://sberbank.ru/doc1",
         "snippet": "скрытая комиссия", "domain": "sberbank.ru"},
    ]
    settings = LoopholeSettings(trust_min=0.0)
    n = await collector.collect_once(
        settings=settings,
        llm=_llm_mock({"is_loophole": True, "confidence": 0.9, "reason": "ок"}),
        session=session,
        search_impl=_search_impl_factory(search_results),
        fetch_impl=_fetch_impl_factory(),
    )
    assert n >= 1
    records = session.execute(
        __import__("sqlalchemy").text("SELECT count(*) FROM loophole_record")
    ).scalar()
    assert records == 1


@pytest.mark.asyncio
async def test_collect_once_dedup(session):
    kw_mod.seed_keywords(session=session)
    kws = repo.list_keywords(session=session)
    for k in kws[1:]:
        repo.set_keyword_active(k["keyword_id"], False, session=session)

    results = [
        {"title": "лазейка", "url": "https://example.ru/x",
         "snippet": "скрытая комиссия", "domain": "example.ru"},
    ]
    settings = LoopholeSettings(trust_min=0.0)
    llm = _llm_mock({"is_loophole": True, "confidence": 0.9, "reason": "ок"})
    await collector.collect_once(
        settings=settings, llm=llm, session=session,
        search_impl=_search_impl_factory(results), fetch_impl=_fetch_impl_factory(),
    )
    # Второй запуск — дедуп по sha256, новых записей 0.
    n2 = await collector.collect_once(
        settings=settings, llm=llm, session=session,
        search_impl=_search_impl_factory(results), fetch_impl=_fetch_impl_factory(),
    )
    assert n2 == 0
    records = session.execute(
        __import__("sqlalchemy").text("SELECT count(*) FROM loophole_record")
    ).scalar()
    assert records == 1


@pytest.mark.asyncio
async def test_collect_once_seeds_if_empty(session):
    """Если ключевых слов нет — collect_once сеет и продолжает."""
    search_results = [
        {"title": "лазейка", "url": "https://example.ru/y",
         "snippet": "комиссия", "domain": "example.ru"},
    ]
    settings = LoopholeSettings(trust_min=0.0)
    n = await collector.collect_once(
        settings=settings,
        llm=_llm_mock({"is_loophole": False, "confidence": 0.1, "reason": "не лазейка"}),
        session=session,
        search_impl=_search_impl_factory(search_results),
        fetch_impl=_fetch_impl_factory(),
        max_per_keyword=1,
    )
    assert n >= 1


@pytest.mark.asyncio
async def test_collect_once_saves_full_content(session):
    """Коллектор сохраняет полный текст страницы (не excerpt) + статус контента."""
    kw_mod.seed_keywords(session=session)
    kws = repo.list_keywords(session=session)
    for k in kws[1:]:
        repo.set_keyword_active(k["keyword_id"], False, session=session)

    results = [
        {"title": "лазейка", "url": "https://example.ru/full",
         "snippet": "короткий сниппет", "domain": "example.ru"},
    ]
    settings = LoopholeSettings(trust_min=0.0)
    await collector.collect_once(
        settings=settings,
        llm=_llm_mock({"is_loophole": True, "confidence": 0.9, "reason": "ок"}),
        session=session,
        search_impl=_search_impl_factory(results),
        fetch_impl=_fetch_impl_factory(
            text="полный текст страницы, заметно длиннее сниппета"
        ),
    )
    row = session.execute(
        __import__("sqlalchemy").text(
            "SELECT raw_text, content_status, raw_text_len FROM loophole_record LIMIT 1"
        )
    ).mappings().first()
    assert "полный текст страницы" in (row["raw_text"] or "")
    assert row["content_status"] == "full"
    assert row["raw_text_len"] == len(row["raw_text"])


def _fetch_impl_by_url(mapping):
    """Инъекция fetch: на каждый URL — свой контент; None имитирует неуспех."""
    def _impl(url, prefer_browser=False):
        body = mapping[url]
        if body is None:
            return None
        result = MagicMock()
        result.content = body.encode("utf-8")
        result.final_url = url
        result.status = 200
        result.content_type = "text/html"
        result.via = "http"
        return result
    return _impl


def _only_first_keyword(session):
    """Сеет ключевые слова и оставляет активным только первое."""
    kw_mod.seed_keywords(session=session)
    kws = repo.list_keywords(session=session)
    for k in kws[1:]:
        repo.set_keyword_active(k["keyword_id"], False, session=session)


@pytest.mark.asyncio
async def test_collect_once_persists_visible_date_as_published_at(session):
    """Видимая дата страницы доходит до loophole_record.published_at (NOT NULL)."""
    # Кеш поиска — на процесс: без сброса подтянутся результаты чужих тестов.
    search_decorator.clear_cache()
    _only_first_keyword(session)
    results = [{
        "title": "лазейка", "url": "https://example.ru/dated",
        "snippet": "скрытая комиссия", "domain": "example.ru",
    }]
    settings = LoopholeSettings(trust_min=0.0)
    n = await collector.collect_once(
        settings=settings,
        llm=_llm_mock({"is_loophole": True, "confidence": 0.9, "reason": "ок"}),
        session=session,
        search_impl=_search_impl_factory(results),
        fetch_impl=_fetch_impl_factory(
            text="Опубликовано 9 сентября 2026 года. Скрытая комиссия в договоре."
        ),
    )
    assert n == 1
    # Pydantic приводит ISO-строку к datetime, SQLite хранит её с пробелом.
    row = session.execute(
        text("SELECT published_at FROM loophole_record WHERE url = 'https://example.ru/dated'")
    ).scalar_one()
    assert row is not None
    assert str(row).startswith("2026-09-09")


@pytest.mark.asyncio
async def test_collect_once_keeps_published_at_null_without_dates(session):
    """Без дат в разметке/тексте или при неуспешном fetch published_at остаётся NULL."""
    search_decorator.clear_cache()
    _only_first_keyword(session)
    results = [
        {"title": "лазейка", "url": "https://example.ru/undated",
         "snippet": "скрытая комиссия", "domain": "example.ru"},
        {"title": "лазейка", "url": "https://example.ru/failed",
         "snippet": "другая комиссия", "domain": "example.ru"},
    ]
    settings = LoopholeSettings(trust_min=0.0)
    n = await collector.collect_once(
        settings=settings,
        llm=_llm_mock({"is_loophole": True, "confidence": 0.9, "reason": "ок"}),
        session=session,
        search_impl=_search_impl_factory(results),
        fetch_impl=_fetch_impl_by_url({
            "https://example.ru/undated": "<html><body><p>текст без дат</p></body></html>",
            "https://example.ru/failed": None,
        }),
    )
    assert n == 2
    rows = session.execute(
        text("SELECT url, published_at FROM loophole_record ORDER BY url")
    ).mappings().all()
    by_url = {row["url"]: row["published_at"] for row in rows}
    assert by_url["https://example.ru/undated"] is None
    assert by_url["https://example.ru/failed"] is None
