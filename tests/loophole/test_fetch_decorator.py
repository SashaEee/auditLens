"""Тест fetch_decorator: обёртка делегирует в rag.fetcher.fetch + parse_auto и добавляет excerpt."""
from __future__ import annotations

from unittest.mock import MagicMock

from bank_audit.loophole.adapters import fetch_decorator


def _fetch_impl_ok():
    result = MagicMock()
    result.content = "<html><body><h1>договор</h1><p>скрытая комиссия 500 руб</p></body></html>".encode("utf-8")
    result.final_url = "https://example.ru/doc"
    result.status = 200
    result.content_type = "text/html"
    result.via = "http"
    def _impl(url, prefer_browser=False):
        return result
    return _impl


def test_fetch_and_parse_returns_page():
    page = fetch_decorator.fetch_and_parse(
        "https://example.ru/doc", _fetch_impl=_fetch_impl_ok()
    )
    assert page is not None
    assert page.url == "https://example.ru/doc"
    assert page.status == 200
    assert page.via == "http"
    assert len(page.excerpt) > 0


def test_fetch_and_parse_excerpt_len():
    page = fetch_decorator.fetch_and_parse(
        "https://example.ru/doc", excerpt_len=10, _fetch_impl=_fetch_impl_ok()
    )
    assert len(page.excerpt) <= 10


def test_fetch_and_parse_none_on_error():
    def impl(url, prefer_browser=False):
        raise RuntimeError("network error")
    page = fetch_decorator.fetch_and_parse("http://x", _fetch_impl=impl)
    assert page is None


def test_fetch_and_parse_none_on_none_result():
    def impl(url, prefer_browser=False):
        return None
    page = fetch_decorator.fetch_and_parse("http://x", _fetch_impl=impl)
    assert page is None
