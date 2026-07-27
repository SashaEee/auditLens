"""Тесты content_fetch: лимитирование и серверный fetch полного контента."""
from __future__ import annotations

from unittest.mock import MagicMock

from bank_audit.loophole import content_fetch
from bank_audit.loophole.config import LoopholeSettings


def _page(text: str):
    p = MagicMock()
    p.text = text
    return p


def test_limit_content_full():
    c = content_fetch.limit_content("текст страницы", max_chars=100)
    assert c.status == content_fetch.STATUS_FULL
    assert c.text == "текст страницы"
    assert c.length == len("текст страницы")
    assert c.truncated is False


def test_limit_content_exact_limit_not_truncated():
    c = content_fetch.limit_content("x" * 100, max_chars=100)
    assert c.status == content_fetch.STATUS_FULL
    assert c.truncated is False


def test_limit_content_over_limit_truncated():
    c = content_fetch.limit_content("x" * 101, max_chars=100)
    assert c.status == content_fetch.STATUS_TRUNCATED
    assert c.text == "x" * 100
    assert c.length == 100
    assert c.truncated is True


def test_limit_content_empty_and_none():
    assert content_fetch.limit_content("", max_chars=10).status == content_fetch.STATUS_EMPTY
    assert content_fetch.limit_content("  \n ", max_chars=10).status == content_fetch.STATUS_EMPTY
    assert content_fetch.limit_content(None, max_chars=10).status == content_fetch.STATUS_EMPTY


def test_fetch_full_content_ok(monkeypatch):
    monkeypatch.setattr(
        content_fetch.fetch_decorator, "fetch_and_parse",
        lambda url, _fetch_impl=None: _page("полный текст страницы"),
    )
    c = content_fetch.fetch_full_content("https://x.ru/a")
    assert c.status == content_fetch.STATUS_FULL
    assert c.text == "полный текст страницы"


def test_fetch_full_content_forwards_impl(monkeypatch):
    seen = {}

    def spy(url, _fetch_impl=None):
        seen["url"] = url
        seen["impl"] = _fetch_impl
        return _page("abc")

    monkeypatch.setattr(content_fetch.fetch_decorator, "fetch_and_parse", spy)
    marker = object()
    c = content_fetch.fetch_full_content("https://x.ru/a", _fetch_impl=marker)
    assert seen == {"url": "https://x.ru/a", "impl": marker}
    assert c.status == content_fetch.STATUS_FULL


def test_fetch_full_content_none_page(monkeypatch):
    monkeypatch.setattr(
        content_fetch.fetch_decorator, "fetch_and_parse",
        lambda url, _fetch_impl=None: None,
    )
    c = content_fetch.fetch_full_content("https://x.ru/404")
    assert c.status == content_fetch.STATUS_FAILED
    assert c.text is None


def test_fetch_full_content_exception(monkeypatch):
    def boom(url, _fetch_impl=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(content_fetch.fetch_decorator, "fetch_and_parse", boom)
    c = content_fetch.fetch_full_content("https://x.ru/err")
    assert c.status == content_fetch.STATUS_FAILED
    assert c.text is None


def test_fetch_full_content_respects_settings_limit(monkeypatch):
    monkeypatch.setattr(
        content_fetch.fetch_decorator, "fetch_and_parse",
        lambda url, _fetch_impl=None: _page("y" * 500),
    )
    settings = LoopholeSettings(raw_text_max_chars=200)
    c = content_fetch.fetch_full_content("https://x.ru/big", settings=settings)
    assert c.status == content_fetch.STATUS_TRUNCATED
    assert c.length == 200


def test_settings_default_limit():
    assert LoopholeSettings().raw_text_max_chars == 200_000


def test_settings_limit_from_env(monkeypatch):
    monkeypatch.setenv("LOOPHOLE_RAW_TEXT_MAX_CHARS", "50000")
    assert LoopholeSettings.load().raw_text_max_chars == 50_000
