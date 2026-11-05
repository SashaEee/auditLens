"""«Пульс» в браузере: общие проверки любой версии страницы и форма фикстур стенда.

Стенд — tests/pulse_ui/harness.py (настоящая страница, API из tests/fixtures/pulse_v2).
Проверки первой группы верны и для старого «Пульса» (8 вкладок), и для нового
(6 разделов): страница отрисовывается без ошибок, пункт меню есть у владельца и коллеги
с доступом, нет у остальных, вкладки переключаются, горизонтальной прокрутки нет.
Ожидания новой оболочки — в test_v2_shell_*: они включаются, когда оболочка готова
(V2_SHELL_READY = True или переменная окружения PULSE_V2_SHELL=1).
"""
from __future__ import annotations

import json
import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.pulse_ui import harness as H
from tests.pulse_ui.harness import (
    assert_no_hscroll,
    assert_page_ok,
    load_fixture,
    no_emoji,
    open_pulse,
)

V2_SHELL_READY = False
v2_shell = pytest.mark.skipif(not (V2_SHELL_READY or os.getenv("PULSE_V2_SHELL") == "1"),
                              reason="новая оболочка «Пульса» ещё не включена (V2_SHELL_READY)")

PULSE = '[data-page="pulse"]'


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b

TABS = f'{PULSE} [role="tablist"] [role="tab"]'


# ── форма фикстур — без браузера ─────────────────────────────────────────────
def test_fixture_shapes_follow_backend(monkeypatch):
    """Каждый ключ, который отдаёт сервер, есть в фикстуре: стенд не расходится с API."""
    from bank_audit.web import telemetry as T
    monkeypatch.setattr(T, "_rows", lambda sql, params=None: [])
    monkeypatch.setattr(T, "_scalar", lambda sql, params=None: 0)
    m = T.metrics(14, [])
    fx = load_fixture("admin_pulse.json")
    assert set(m) <= set(fx), set(m) - set(fx)
    for k in ("today", "features", "segments", "ai_feedback", "retention", "mail", "persona",
              "ingest", "collect", "search", "news_quality", "personalization", "topics"):
        if isinstance(m[k], dict):
            assert set(m[k]) <= set(fx[k]), (k, set(m[k]) - set(fx[k]))
    d = T.users_directory(30, [])
    ux = load_fixture("admin_users.json")
    assert set(d) <= set(ux), set(d) - set(ux)
    # строка человека — все псевдонимы колонок _DIRECTORY и поля, что дописывает Python
    head = T._DIRECTORY.split("FROM app_user au")[0]
    cols = set(re.findall(r"\bAS\s+([a-z_]+)", head)) | {"username"}
    assert len(cols) > 15, cols
    for row in ux["users"]:
        assert cols | {"online", "today", "excluded"} <= set(row), cols - set(row)
    card = load_fixture("admin_user_card.akovaleva.json")
    assert {"user", "profile", "by_day", "pages", "questions", "reports", "ratings", "errors",
            "trail"} == set(card)


def test_fixtures_carry_contract_fields():
    """Новые поля CONTRACT §6 — в фикстурах, чтобы фронт проверялся и с ними."""
    m = load_fixture("admin_pulse.json")
    h = m["health"]
    assert len(h["days"]) == 14 and h["days"][-1] == "2026-11-05" and h["off"] == [1, 2, 8, 9, 12]
    for k in ("server", "digest", "reviews", "news", "slices", "rsearch", "web", "ingest"):
        assert len(h[k]) == 14 and set(h[k]) <= {"o", "w", "b", "n"}, k
    assert all("off" in x for x in m["dau"])
    assert {"ai_total_prev", "reports_prev"} <= set(m["features"])
    assert sum(x["good"] for x in m["ai_feedback"]["by_day"]) == m["ai_feedback"]["likes"]
    assert sum(x["bad"] for x in m["ai_feedback"]["by_day"]) == m["ai_feedback"]["dislikes"]
    seg = m["retention"]["segments"]
    assert set(m["retention"]["seg_of"].values()) <= {"core", "regular", "once", "churned"}
    assert len(m["retention"]["seg_of"]) == seg["core"] + seg["regular"] + seg["once"] + seg["churned"]
    u = load_fixture("admin_users.json")
    assert len(u["act_days"]) == 14 and u["act_off"] == h["off"]
    assert all(re.fullmatch(r"[0-3]{14}", r["act14"]) for r in u["users"])
    # сюжет макета: 64 человека, 23 сегодня, 41 вопрос, 3 обращения ждут ответа
    t = m["today"]
    assert (t["users_total"], t["active"], t["ai"], m["inbox"]["unread"]) == (64, 23, 41, 3)
    assert u["total"] == 64 and u["today"] == 23


def test_harness_serves_index_like_server():
    """Стенд отдаёт ту же страницу, что сервер: app.js при совпадении сборки, иначе Babel."""
    from bank_audit.web import app as A
    html, mode = H.index_html()
    assert html == A._index_html_with_bust()
    assert mode == ("built" if '<script src="/static/app.js?v=' in html else "babel")


def test_fixture_json_is_valid_utf8():
    for p in sorted(H.FIXTURES.glob("*.json")):
        json.loads(p.read_text(encoding="utf-8"))


# ── любая версия «Пульса» ────────────────────────────────────────────────────
def _nav_pulse(page):
    return page.locator(".rail .nav-item", has_text=re.compile(r"^\s*Пульс\s*$"))


def test_owner_opens_pulse_from_hash(browser):
    page = open_pulse(browser)
    assert_page_ok(page)
    nav = _nav_pulse(page)
    assert nav.count() == 1 and "active" in (nav.get_attribute("class") or "")
    assert any(c.startswith("/api/admin/pulse?") and "days=" in c for c in page.calls), page.calls
    assert page.locator(f'{PULSE} [role="tablist"]').is_visible()
    assert page.locator(TABS).count() >= 6
    assert page.locator(f'{PULSE} [role="tabpanel"]').count() == 1
    page.context.close()


def test_pulse_user_opens_pulse_without_owner_requests(browser):
    page = open_pulse(browser, me="pulse_user")
    assert_page_ok(page)
    assert _nav_pulse(page).count() == 1
    # плашка «Турбулентность» — только владельцу: коллеге её состояние не запрашивается
    assert not H.api_calls(page, "/api/admin/banner")
    page.context.close()


def test_no_pulse_menu_without_rights(browser):
    me = {**load_fixture("me.pulse_user.json"), "can_pulse": False, "is_admin": False}
    page = open_pulse(browser, overrides={"/api/me": me}, wait=None)
    page.wait_for_function("() => document.querySelectorAll('.rail .nav-item').length > 3")
    assert _nav_pulse(page).count() == 0
    assert not page.errors, page.errors
    page.context.close()


@pytest.mark.parametrize("width", H.WIDTHS)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_every_tab_renders_without_hscroll(browser, width, dark):
    """Каждая вкладка отрисовывается без ошибок и без горизонтальной прокрутки."""
    page = open_pulse(browser, width=width, dark=dark)
    assert page.evaluate("document.documentElement.classList.contains('dark')") is dark
    n = page.locator(TABS).count()
    for i in range(n):
        tab = page.locator(TABS).nth(i)
        tab.scroll_into_view_if_needed()
        tab.click()
        page.wait_for_function(
            "(i) => document.querySelectorAll('[data-page=\"pulse\"] [role=\"tablist\"] [role=\"tab\"]')[i]"
            ".getAttribute('aria-selected') === 'true'", arg=i)
        page.wait_for_timeout(300)                   # блоки со своей загрузкой
        label = tab.inner_text().splitlines()[0]
        assert_page_ok(page)
        try:
            assert_no_hscroll(page)
        except AssertionError as e:
            raise AssertionError(f"вкладка «{label}», {width}px: {e}") from None
    page.context.close()


# ── новая оболочка (CONTRACT §1, §3, §4) — включается следующим этапом ───────
V2_TABS = ["Сводка", "Люди", "Качество ИИ", "Обращения", "Системы", "Итоги"]


def _selected(page) -> str:
    return page.locator(f'{TABS}[aria-selected="true"]').get_attribute("id") or ""


@v2_shell
def test_v2_shell_sections_and_aliases(browser):
    page = open_pulse(browser)
    labels = [t.splitlines()[0].strip() for t in page.locator(TABS).all_inner_texts()]
    assert labels == V2_TABS
    assert [page.locator(TABS).nth(i).get_attribute("id") for i in range(6)] == \
        [f"pu2-tab-{k}" for k in H.PULSE_TABS_V2]
    assert _selected(page) == "pu2-tab-overview"
    # бейдж «Обращений» — m.inbox.unread
    assert page.locator("#pu2-tab-inbox").inner_text().splitlines()[1].strip() == "3"
    aliases = {"people": "people", "outcomes": "results", "reach": "results", "reports": "quality",
               "ai": "quality", "inbox": "inbox", "data": "systems", "tech": "systems",
               "quality": "quality", "systems": "systems", "results": "results",
               "overview": "overview", "nonsense": "overview"}
    for old, new in aliases.items():
        page = open_pulse(page, tab=old)
        assert _selected(page) == f"pu2-tab-{new}", old
        assert page.evaluate("localStorage.getItem('al-pulse-tab')") in (new, None)
    assert_page_ok(page)
    page.context.close()


@v2_shell
def test_v2_shell_brief_footer_and_no_emoji(browser):
    page = open_pulse(browser)
    brief = page.locator(f"{PULSE} h1.pu2-brief").inner_text()
    assert "23 человека сегодня, 41 вопрос ИИ-помощнику." in brief
    assert "Три сбоя и четыре дела ждут вас." in brief
    foot = page.locator(f"{PULSE} .pu2-foot").inner_text()
    assert "без вас и без 2 служебных учёток" in foot and "Открытие чужого отчёта пишется в журнал" in foot
    for k in H.PULSE_TABS_V2:
        page.locator(f"#pu2-tab-{k}").click()
        page.wait_for_timeout(300)
        no_emoji(page)
    assert_page_ok(page)
    page.context.close()


@v2_shell
def test_v2_shell_period_only_in_people_and_quality(browser):
    page = open_pulse(browser)
    period = page.locator(f'{PULSE} .rv-tabs-r [role="group"]', has_text="14")
    for k in H.PULSE_TABS_V2:
        page.locator(f"#pu2-tab-{k}").click()
        page.wait_for_timeout(200)
        assert period.count() == (1 if k in ("people", "quality") else 0), k
    page.context.close()
