"""Тест collector: мок search → мок fetch → мок classify → проверка записей в БД."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bank_audit.loophole import collector
from bank_audit.loophole import repository as repo
from bank_audit.loophole import keywords as kw_mod
from bank_audit.loophole.config import LoopholeSettings
from bank_audit.hashing import sha256_text


from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


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
