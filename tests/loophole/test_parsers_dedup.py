"""Тест dedup: normalize_target (дедуп источников) и page_text_sha256 (дедуп записей)."""
from __future__ import annotations

from bank_audit.loophole.parsers import dedup


# ── normalize_target ─────────────────────────────────────────────────────────
def test_scheme_and_www_insensitive():
    assert dedup.normalize_target("https://www.Example.com/page") == \
        dedup.normalize_target("http://example.com/page") == "example.com/page"


def test_trailing_slash_and_root():
    assert dedup.normalize_target("https://a.ru/") == "a.ru/"
    assert dedup.normalize_target("https://a.ru/page/") == "a.ru/page"


def test_tracking_params_removed():
    assert dedup.normalize_target("https://a.ru/p?utm_source=x&b=2&utm_medium=y") == "a.ru/p?b=2"
    assert dedup.normalize_target("https://a.ru/p?gclid=z") == "a.ru/p"
    assert dedup.normalize_target("https://a.ru/p?yclid=z&fbclid=q") == "a.ru/p"


def test_fragment_removed_and_query_keeps_semantics():
    assert dedup.normalize_target("https://a.ru/p?x=1#sec") == "a.ru/p?x=1"
    assert dedup.normalize_target("https://a.ru/p?page=2") != dedup.normalize_target("https://a.ru/p?page=3")


def test_default_ports_removed():
    assert dedup.normalize_target("https://a.ru:443/p") == "a.ru/p"
    assert dedup.normalize_target("http://a.ru:8080/p") == "a.ru:8080/p"


def test_telegram_forms_equal():
    assert dedup.normalize_target("@bank_secrets") == "t.me/bank_secrets"
    assert dedup.normalize_target("https://t.me/bank_secrets") == "t.me/bank_secrets"
    assert dedup.normalize_target("t.me/Bank_Secrets") == "t.me/bank_secrets"


def test_empty_and_garbage():
    assert dedup.normalize_target("") == ""
    assert dedup.normalize_target("   ") == ""


def test_malformed_url_falls_back_to_lowercase():
    # urlsplit падает (битый IPv6) → возвращается lowercase-строка как есть.
    assert dedup.normalize_target("HTTP://[::1/p") == "http://[::1/p"


def test_invalid_port_dropped():
    # Порт вне диапазона → ValueError в parts.port → порт отбрасывается.
    assert dedup.normalize_target("https://a.ru:99999/p") == "a.ru/p"


# ── page_text_sha256 ─────────────────────────────────────────────────────────
def test_text_sha_normalizes_whitespace():
    a = dedup.page_text_sha256("  много\n\nпробелов   текст ")
    b = dedup.page_text_sha256("много пробелов текст")
    assert a is not None and a == b


def test_text_sha_fallback_to_snippet():
    assert dedup.page_text_sha256(None, "сниппет") == dedup.page_text_sha256("сниппет", None)


def test_text_sha_none_when_empty():
    assert dedup.page_text_sha256(None, None) is None
    assert dedup.page_text_sha256("", "   ") is None
