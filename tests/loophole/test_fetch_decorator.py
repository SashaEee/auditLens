"""Тест fetch_decorator: обёртка делегирует в rag.fetcher.fetch + parse_auto и добавляет excerpt."""
from __future__ import annotations

import os
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


def test_normalize_to_utf8_from_meta_charset():
    html = (
        '<html><head><meta charset="windows-1251"></head>'
        "<body><p>договор</p></body></html>"
    ).encode("cp1251")
    out = fetch_decorator._normalize_to_utf8(html, "text/html")
    assert "договор".encode("utf-8") in out


def test_normalize_to_utf8_from_content_type():
    text = "Привет, мир!".encode("cp1251")
    out = fetch_decorator._normalize_to_utf8(text, "text/plain; charset=windows-1251")
    assert out == "Привет, мир!".encode("utf-8")


def test_normalize_to_utf8_keeps_utf8():
    data = "<html><body>уже utf-8</body></html>".encode("utf-8")
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
