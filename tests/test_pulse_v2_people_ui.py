"""«Пульс» v2, раздел «Люди» в браузере (стенд tests/pulse_ui, API — фикстуры).

Проверяется (CONTRACT §9, макет docs/design/pulse/pulse-v2.html, README «Куда переехал»):
- «Кто с нами»: пять групп с числами из retention.segments, точка на человека по seg_of
  (служебные не показываются), точка открывает лист человека;
- «Перестали заходить» (якорь pu2-risk) и «Ушли»: первые три + «Показать всех», «Текст
  письма» копирует личный текст и сообщает об этом; «Все, кто молчит» включает отбор;
- «Новички возвращаются?»: «4 из 6», процент в подсказке, «рано»;
- «Все люди»: отборы с живыми счётчиками, поиск (ё = е), порядок по заголовку (aria-sort),
  16 строк + «Показать всех», строка открывает лист по Enter, служебные — в конце;
- коллеге не приходят адреса — нет колонки «Почта» и отбора «Без почты»;
- ошибка загрузки списка — «Повторить», числа групп остаются; 390 px тёмная — без
  горизонтальной прокрутки и эмодзи.
"""
from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.pulse_ui import harness as H
from tests.pulse_ui.harness import assert_no_hscroll, assert_page_ok, no_emoji, open_pulse

PANEL = '[data-page="pulse"] #pu2-panel'
LIFE = f"{PANEL} #pu2-pp-life"
RISK = f"{PANEL} #pu2-risk"
COH = f"{PANEL} #pu2-pp-coh"
TBL = f"{PANEL} #pu2-people"
DIALOG = ".rv-ovl-card[role=dialog]"


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b


def _open(browser, **kw):
    page = open_pulse(browser, tab="people", width=kw.pop("width", 1440), **kw)
    page.wait_for_selector(f"{TBL} tbody tr", timeout=10000)
    return page


def _text(page, sel: str) -> str:
    return re.sub(r"\s+", " ", page.locator(sel).inner_text().replace(" ", " ")).strip()


def _users() -> list[dict]:
    return H.load_fixture("admin_users.json")["users"]


def test_groups_dots_risk_and_cohorts(browser):
    page = _open(browser)
    pulse = H._pulse({"days": "14"}, "owner")
    seg, seg_of = pulse["retention"]["segments"], pulse["retention"]["seg_of"]
    nums = [_text(page, f"{LIFE} [data-seg={k}] > b") for k in ("core", "regular", "once", "churned", "long")]
    long_n = pulse["today"]["users_total"] - sum(seg[k] for k in ("core", "regular", "once", "churned"))
    assert nums == [str(seg["core"]), str(seg["regular"]), str(seg["once"]), str(seg["churned"]), str(long_n)]
    # точка — человек: служебных и исключённых нет, без группы — «давно не заходят»
    people = [u for u in _users() if not u["excluded"]]
    for k in ("core", "long"):
        want = sum(1 for u in people if seg_of.get(u["username"], "long") == k)
        assert page.locator(f"{LIFE} [data-seg={k}] .pu2-pp-u").count() == want, k
    excluded = {u["name"] for u in _users() if u["excluded"]}
    tips = set(page.locator(f"{LIFE} .pu2-pp-u").evaluate_all("els => els.map(e => e.dataset.tip)"))
    assert not tips & excluded
    page.locator(f"{LIFE} [data-seg=core] .pu2-pp-u").first.click()
    page.wait_for_selector(DIALOG)
    page.keyboard.press("Escape")
    page.wait_for_selector(DIALOG, state="detached")

    risk = pulse["retention"]["at_risk"]
    assert page.locator(f"{RISK} ul[aria-label='Перестали заходить'] li").count() == len(risk)
    gone = page.locator(f"{RISK} ul[aria-label='Ушли'] li")
    assert gone.count() == 3
    page.locator(f"{RISK} button", has_text="Показать всех").click()
    assert gone.count() == len(pulse["retention"]["churned"])

    first = pulse["retention"]["cohorts"][0]
    cell = page.locator(f"{COH} tbody tr").first.locator(".pu2-pp-cell").first
    assert cell.inner_text().strip() == f"{first['w2']} из {first['w2_ready']}"
    pct = round(first["w2"] * 100 / first["w2_ready"])
    assert cell.get_attribute("data-tip") == f"{first['w2']} из {first['w2_ready']} — {pct}%"
    assert page.locator(f"{COH} .pu2-pp-cell.wait").count() >= 1
    assert page.locator(f"{PANEL} #pu2-pp-heat .pu2-pp-hc[data-tip]").count() >= 5 * 13
    assert page.locator(f"{PANEL} #pu2-pp-pages .pu2-hbar").count() == len(pulse["pages"])
    assert "подключили почту" in _text(page, f"{PANEL} #pu2-pp-mail")
    assert_page_ok(page)
    page.context.close()


def test_table_filters_search_sort_and_open(browser):
    page = _open(browser)
    people = [u for u in _users() if not u["excluded"]]
    chip = lambda name: page.locator(f"{TBL} .chip", has_text=name)  # noqa: E731
    assert chip("Все").locator("i").inner_text().strip() == str(len(people))
    rows = page.locator(f"{TBL} tbody tr")
    assert rows.count() == 16
    page.locator(f"{TBL} .pu2-pp-more button").click()
    assert rows.count() == len(_users())
    # служебные — в конце и бледные
    assert "pu2-dim" in (rows.last.get_attribute("class") or "")
    online = [u for u in people if u["online"]]
    chip("Онлайн").click()
    assert chip("Онлайн").get_attribute("aria-pressed") == "true"
    assert rows.count() == len(online) == int(chip("Онлайн").locator("i").inner_text())
    chip("Все").click()
    # поиск: ё = е, по фамилии и логину
    page.fill(f"{TBL} .pu2-pp-q", "ковалев")
    assert rows.count() == 1 and "Анна Ковалёва" in rows.first.inner_text()
    page.fill(f"{TBL} .pu2-pp-q", "")
    # порядок по заголовку: сначала по убыванию, повторно — по возрастанию
    th = page.locator(f"{TBL} thead th", has_text="Вопросы ИИ")
    th.locator("button").click()
    assert th.get_attribute("aria-sort") == "descending"
    top = max(people, key=lambda u: u["ai"])
    assert top["name"] in rows.first.inner_text()
    th.locator("button").click()
    assert th.get_attribute("aria-sort") == "ascending"
    # строка открывает лист человека по Enter
    rows.first.focus()
    login = rows.first.get_attribute("data-login")
    page.keyboard.press("Enter")
    page.wait_for_selector(DIALOG)
    assert f"@{login}" in page.locator(DIALOG).inner_text()
    assert_page_ok(page)
    page.context.close()


def test_winback_copy_and_silent_link(browser):
    page = _open(browser)
    page.evaluate("""() => { window.__copied = null;
      Object.defineProperty(navigator, 'clipboard', {configurable: true,
        value: {writeText: t => { window.__copied = t; return Promise.resolve(); }}}); }""")
    page.locator(f"{RISK} li").first.locator("button", has_text="Текст письма").click()
    page.wait_for_function("() => !!window.__copied")
    txt = page.evaluate("() => window.__copied")
    assert "Вы заходили в AuditLens" in txt and "m=winback" in txt
    page.wait_for_selector("text=Текст скопирован — отправьте его сами")
    page.locator(f"{RISK} button", has_text="Все, кто молчит").click()
    silent = page.locator(f"{TBL} .chip", has_text="Молчат 14 дней")
    assert silent.get_attribute("aria-pressed") == "true"
    want = [u for u in _users() if not u["excluded"] and not re.search("[1-3]", u.get("act14") or "")]
    assert page.locator(f"{TBL} tbody tr").count() == min(16, len(want))
    page.context.close()


def test_colleague_sees_mail_kind_without_addresses(browser):
    """Коллеге сервер отдаёт вид почты без адресов: колонка есть, адресов в подсказках нет;
    без mail.people (старый сервер) колонки и отбора «Без почты» нет."""
    page = _open(browser, me="pulse_user")
    heads = [t.strip() for t in page.locator(f"{TBL} thead th").all_inner_texts()]
    assert "Почта" in heads and "Профиль" in heads
    tips = page.locator(f"{TBL} tbody [data-tip]").evaluate_all("els => els.map(e => e.dataset.tip)")
    assert not [t for t in tips if "@" in t], tips
    page.context.close()
    fx = H._pulse({"days": "14"}, "owner")
    fx["mail"].pop("people")
    page = _open(browser, overrides={"/api/admin/pulse": fx})
    heads = [t.strip() for t in page.locator(f"{TBL} thead th").all_inner_texts()]
    assert "Почта" not in heads
    assert page.locator(f"{TBL} .chip", has_text="Без почты").count() == 0
    page.context.close()


def test_users_error_and_phone_dark(browser):
    page = open_pulse(browser, tab="people", width=390, dark=True,
                      overrides={"/api/admin/users": (500, {"detail": "boom"})})
    page.wait_for_selector(f"{TBL} .pu2-err")
    assert page.locator(f"{TBL} .pu2-err button", has_text="Повторить").count() == 1
    assert _text(page, f"{LIFE} [data-seg=core] > b").isdigit()
    page.context.close()
    page = _open(browser, width=390, dark=True)
    assert_no_hscroll(page)
    no_emoji(page)
    assert_page_ok(page)
    page.context.close()
