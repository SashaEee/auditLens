"""Тест search_decorator: обёртка делегирует в rag.web_search.search и добавляет кеш/нормализацию."""
from __future__ import annotations

from bank_audit.loophole.adapters import search_decorator


def _impl(query, *, max_results=10, site_filter=None, region="ru-ru"):
    return [
        {"title": f"result for {query}", "url": "http://x.ru/1",
         "snippet": "snippet", "domain": "x.ru"},
    ]


def test_search_delegates_to_impl():
    results = search_decorator.search("лазейка", _impl=_impl)
    assert len(results) == 1
    assert "лазейка" in results[0]["title"]


def test_search_normalizes_fields():
    long_title = "x" * 500
    def impl(q, **kw):
        return [{"title": long_title, "url": "http://x", "snippet": "s", "domain": "d"}]
    results = search_decorator.search("q", _impl=impl)
    assert len(results[0]["title"]) <= 300


def test_search_caches():
    calls = []
    def impl(q, **kw):
        calls.append(q)
        return [{"title": "x", "url": "http://x", "snippet": "s", "domain": "d"}]
    search_decorator.clear_cache()
    search_decorator.search("q1", _impl=impl)
    search_decorator.search("q1", _impl=impl)
    assert len(calls) == 1, "повторный вызов должен взять из кеша"


def test_search_empty_query_returns_empty():
    assert search_decorator.search("", _impl=_impl) == []
    assert search_decorator.search("   ", _impl=_impl) == []


def test_clear_cache():
    search_decorator.search("q", _impl=_impl)
    search_decorator.clear_cache()
    assert not search_decorator._CACHE
