"""Тест fetch_decorator: обёртка делегирует в rag.fetcher.fetch + parse_auto и добавляет excerpt."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

from bank_audit.loophole.adapters import fetch_decorator


def _fetch_impl_ok():
    result = MagicMock()
    result.content = "<html><body><h1>договор</h1><p>скрытая комиссия 500 руб</p></body></html>".encode()
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


def test_fetch_and_parse_requests_direct_transport_without_changing_default():
    received = {}
    result = MagicMock()
    result.content = "<html><body>текст</body></html>".encode()
    result.final_url = "https://example.ru/doc"
    result.status = 200
    result.content_type = "text/html"
    result.via = "http"

    def impl(url, *, prefer_browser=False, direct=False):
        received.update(prefer_browser=prefer_browser, direct=direct)
        return result

    page = fetch_decorator.fetch_and_parse(
        "https://example.ru/doc", prefer_browser=True, _fetch_impl=impl
    )

    assert page is not None
    assert received == {"prefer_browser": True, "direct": True}


def test_fetch_and_parse_keeps_legacy_injected_fetcher_working():
    result = MagicMock()
    result.content = "<html><body>текст</body></html>".encode()
    result.final_url = "https://example.ru/doc"
    result.status = 200
    result.content_type = "text/html"
    result.via = "http"

    def legacy_impl(url, prefer_browser=False):
        return result

    assert fetch_decorator.fetch_and_parse("https://example.ru/doc", _fetch_impl=legacy_impl)


def test_fetch_and_parse_does_not_retry_internal_typeerror_without_direct():
    calls = []

    def impl(url, *, prefer_browser=False, direct=False):
        calls.append(direct)
        raise TypeError("internal failure")

    assert fetch_decorator.fetch_and_parse("https://example.ru/doc", _fetch_impl=impl) is None
    assert calls == [True]


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


def test_normalize_to_utf8_from_meta_charset():
    html = (
        '<html><head><meta charset="windows-1251"></head>'
        "<body><p>договор</p></body></html>"
    ).encode("cp1251")
    out = fetch_decorator._normalize_to_utf8(html, "text/html")
    assert "договор".encode() in out


def test_normalize_to_utf8_from_content_type():
    text = "Привет, мир!".encode("cp1251")
    out = fetch_decorator._normalize_to_utf8(text, "text/plain; charset=windows-1251")
    assert out == "Привет, мир!".encode()


def test_normalize_to_utf8_keeps_utf8():
    data = "<html><body>уже utf-8</body></html>".encode()
    assert fetch_decorator._normalize_to_utf8(data, "text/html; charset=utf-8") == data


def test_normalize_to_utf8_unknown_charset_keeps_bytes():
    data = b"\x80\x81 binary"
    assert fetch_decorator._normalize_to_utf8(data, "text/html; charset=koi8-x") == data


def test_fetch_and_parse_decodes_cp1251():
    result = MagicMock()
    result.content = (
        '<html><head><meta charset="windows-1251"></head>'
        "<body><p>скрытая комиссия</p></body></html>"
    ).encode("cp1251")
    result.final_url = "https://example.ru/doc"
    result.status = 200
    result.content_type = "text/html"
    result.via = "http"

    def impl(url, prefer_browser=False):
        return result

    page = fetch_decorator.fetch_and_parse("https://example.ru/doc", _fetch_impl=impl)
    assert page is not None
    assert "скрытая комиссия" in page.text


def test_fetch_and_parse_exposes_exact_source_publication_timestamp():
    result = MagicMock()
    result.content = (
        '<html><head><meta property="article:published_time" '
        'content="2026-08-27T09:25:00+03:00"></head><body>текст</body></html>'
    ).encode()
    result.final_url = "https://example.ru/doc"
    result.status = 200
    result.content_type = "text/html"
    result.via = "http"

    page = fetch_decorator.fetch_and_parse(
        "https://example.ru/doc",
        _fetch_impl=lambda _url, prefer_browser=False: result,
    )

    assert page is not None
    assert page.published_at == "2026-08-27T09:25:00+03:00"


def test_fetch_and_parse_keeps_naive_publication_date_unknown():
    result = MagicMock()
    result.content = b'<script>{"datePublished":"2026-08-27"}</script>'
    result.final_url = "https://example.ru/doc"
    result.status = 200
    result.content_type = "text/html"
    result.via = "http"

    page = fetch_decorator.fetch_and_parse(
        "https://example.ru/doc",
        _fetch_impl=lambda _url, prefer_browser=False: result,
    )

    assert page is not None
    assert page.published_at is None


def test_sanitize_ca_bundle_env_drops_invalid(monkeypatch):
    monkeypatch.setenv("CA_BUNDLE_PATH", "/app/config/ca_bundle_combined.pem")
    fetch_decorator._sanitize_ca_bundle_env()
    assert "CA_BUNDLE_PATH" not in os.environ


def test_sanitize_ca_bundle_env_keeps_valid(monkeypatch, tmp_path):
    pem = tmp_path / "bundle.pem"
    pem.write_text("dummy")
    monkeypatch.setenv("CA_BUNDLE_PATH", str(pem))
    fetch_decorator._sanitize_ca_bundle_env()
    assert os.environ["CA_BUNDLE_PATH"] == str(pem)
