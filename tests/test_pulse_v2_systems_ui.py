"""«Пульс» v2 в браузере: раздел «Системы».

Стенд — tests/pulse_ui/harness.py. Проверяется: восемь служб строками, строки со сбоями
раскрыты, раскрытие показывает подробности прежних вкладок «Данные» и «Техника», прежний
ключ #pulse?tab=tech ведёт сюда, плашка «Турбулентность» — только владельцу (из меню «⋯»
с прокруткой к ней, POST включения), ширины 1208 светлая и 390 тёмная без ошибок и
горизонтальной прокрутки.

Снимки: PULSE_SHOTS=<каталог> python -m pytest tests/test_pulse_v2_systems_ui.py
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.pulse_ui import harness as H
from tests.pulse_ui.harness import (
    api_calls,
    assert_no_hscroll,
    assert_page_ok,
    no_emoji,
    open_pulse,
)

PANEL = '[data-page="pulse"] #pu2-panel'
SHOTS = os.getenv("PULSE_SHOTS")


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b


def _shot(page, name: str) -> None:
    if SHOTS:
        H.shot(page, os.path.join(SHOTS, name + ".png"), full_page=True)


def _expanded(page) -> dict:
    return page.eval_on_selector_all(
        f"{PANEL} .pu2-sy-hd",
        "els => Object.fromEntries(els.map(b => [b.closest('[id^=\"pu2-sys-\"]').id.slice(8),"
        " b.getAttribute('aria-expanded') === 'true']))")


def test_systems_rows_and_details(browser):
    page = open_pulse(browser, tab="systems", wait="#pu2-sys")
    try:
        meta = page.inner_text(f"{PANEL} #pu2-sys .pu2-ch-meta")
        # по фикстурам сегодня: сайт (2 ошибки), выпуск (раздел не собран), площадки (просел) — не в норме
        assert meta.startswith("5 из 8 в норме"), meta
        exp = _expanded(page)
        assert list(exp) == ["server", "digest", "reviews", "news", "slices", "rsearch", "web", "ingest"]
        assert {k for k, v in exp.items() if v} == {"server", "digest", "reviews"}
        # лента: 14 ячеек, последняя (сегодня) обведена
        assert page.locator(f"{PANEL} #pu2-sys-news .pu2-sy-hd .pu2-rib i").count() == 14
        server = page.inner_text(f"{PANEL} #pu2-sd-server")
        assert "сегодня 09:41" in server and "×2 · 2 чел." in server
        assert "Тарифы" in page.inner_text(f"{PANEL} #pu2-sd-digest")
        rv = page.inner_text(f"{PANEL} #pu2-sd-reviews")
        assert "Отзовик" in rv and "просел" in rv and "внешний корпус" in rv
        # раскрытие строки: подробности новостного потока (судья, отдача, клики)
        page.click(f"{PANEL} #pu2-sys-news .pu2-sy-hd")
        news = page.inner_text(f"{PANEL} #pu2-sd-news")
        assert "Выпуск: независимый судья" in news and "Отдача источников" in news
        assert "Банки снижают ставки" in news
        # срезы грузятся своим запросом только по раскрытию
        assert not api_calls(page, "/api/admin/pulse/slices")
        page.click(f"{PANEL} #pu2-sys-slices .pu2-sy-hd")
        page.wait_for_selector(f"{PANEL} #pu2-sd-slices table")
        sl = page.inner_text(f"{PANEL} #pu2-sd-slices")
        assert "Зарплатные проекты" in sl and "Канарейка" in sl and "Точность сигналов" in sl
        for k in ("web", "ingest", "rsearch"):
            page.click(f"{PANEL} #pu2-sys-{k} .pu2-sy-hd")
        assert "выручил запасной fleet" in page.inner_text(f"{PANEL} #pu2-sd-web")
        assert "капча" in page.inner_text(f"{PANEL} #pu2-sd-ingest")
        assert "навязали страховку" in page.inner_text(f"{PANEL} #pu2-sd-rsearch")
        # повторное нажатие сворачивает
        page.click(f"{PANEL} #pu2-sys-web .pu2-sy-hd")
        assert page.get_attribute(f"{PANEL} #pu2-sys-web .pu2-sy-hd", "aria-expanded") == "false"
        assert "reviews/search" in page.inner_text(f"{PANEL} #pu2-latency")
        assert_page_ok(page)
        assert no_emoji(page)
        assert_no_hscroll(page)
        _shot(page, "systems-1208-light")
    finally:
        page.close()


def test_banner_owner_menu_and_post(browser):
    page = open_pulse(browser, tab="tech", wait="#pu2-sys")      # прежний ключ → «Системы»
    try:
        assert page.get_attribute('[data-page="pulse"] #pu2-tab-systems', "aria-selected") == "true"
        page.click('[data-page="pulse"] .pu2-band .rv-ib')
        page.locator('[data-page="pulse"] .pu2-band .rv-menu-pop').get_by_text("Плашка «Турбулентность»").click()
        page.wait_for_selector(f"{PANEL} #pu2-banner .btn")
        assert "Выключена" in page.inner_text(f"{PANEL} #pu2-banner")
        page.get_by_role("button", name="На 3 дня").click()
        page.wait_for_function(
            "() => document.querySelector('#pu2-banner').innerText.includes('Включена до')")
        post = [p for p in page.posts if p["url"].startswith("/api/admin/banner")]
        assert post and post[-1]["body"] == {"active": True, "days": 3}
        assert "8 ноября" in page.inner_text(f"{PANEL} #pu2-banner")
        assert_page_ok(page)
    finally:
        page.close()


def test_pulse_user_without_banner_and_narrow_dark(browser):
    page = open_pulse(browser, tab="systems", me="pulse_user", width=390, dark=True, wait="#pu2-sys")
    try:
        assert page.locator(f"{PANEL} #pu2-banner").count() == 0
        assert not api_calls(page, "/api/admin/banner")
        # на телефоне лента — внутри раскрытой строки
        assert page.locator(f"{PANEL} #pu2-sd-server .pu2-sy-ribm .pu2-rib").is_visible()
        assert not page.locator(f"{PANEL} #pu2-sys-server .pu2-sy-hd .pu2-rib").is_visible()
        assert_page_ok(page)
        assert no_emoji(page)
        assert_no_hscroll(page)
        _shot(page, "systems-390-dark")
    finally:
        page.close()
