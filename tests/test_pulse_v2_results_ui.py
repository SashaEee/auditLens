"""«Пульс» v2 в браузере: раздел «Итоги».

Стенд — tests/pulse_ui/harness.py. Проверяется: свой период итогов (с 14 днями, хранится в
'al-pu-oc-days' — его же берёт пункт меню «Скачать итоги в Excel»), плитки с изменением к
прошлому периоду, охват по ТБ полностью, путь от вопроса к результату, банки, направления,
траты на модели, обратная связь; «Разметить новые» и загрузка штата — только владельцу;
состояния «штат не загружен», «учёт трат не подключён», ошибка с «Повторить»; ширины 1208
светлая и 390 тёмная без ошибок и горизонтальной прокрутки.

Снимки: PULSE_SHOTS=<каталог> python -m pytest tests/test_pulse_v2_results_ui.py
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

PULSE = '[data-page="pulse"]'
PANEL = f"{PULSE} #pu2-panel"
SHOTS = os.getenv("PULSE_SHOTS")


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b


def _shot(page, name: str) -> None:
    if SHOTS:
        H.shot(page, os.path.join(SHOTS, name + ".png"), full_page=True)


def test_results_owner_period_and_blocks(browser):
    page = open_pulse(browser, tab="results", wait="#pu2-funnel")
    try:
        assert api_calls(page, "/api/admin/pulse/outcomes?days=0")      # по умолчанию — всё время
        seg = page.locator(f'{PANEL} [aria-label="Период итогов"] .seg-btn')
        assert seg.all_inner_texts() == ["Всё время", "90 дн", "30 дн", "14 дн", "7 дн"]
        seg.nth(3).click()
        page.wait_for_selector(f"{PANEL} .pu2-kpi >> text=к прошлым 14 дн")
        assert api_calls(page, "/api/admin/pulse/outcomes?days=14")
        assert page.evaluate("localStorage.getItem('al-pu-oc-days')") == "14"
        href = page.get_attribute(f"{PANEL} .pu2-res-tools a[download]", "href")
        assert href.startswith("/api/admin/pulse/outcomes.xlsx?days=14")
        kpi = page.inner_text(f"{PANEL} .pu2-grid")
        assert "312" in kpi and "Годные вопросы" in kpi and "Аудит-дела" in kpi
        reach = page.inner_text(f"{PANEL} #pu2-reach")
        assert "ТБ" in reach and "СЗБ" in reach and "Итого" in reach and "Ведущий аудитор" in reach
        assert page.locator(f"{PANEL} #pu2-reach button", has_text="Обновить штат (.xlsx)").count() == 1
        # 13.11: «Заходили» вместо «Пользуются», период и часы с 3 октября, список «не нашлись»
        assert "Заходили" in reach and "Пользуются" not in reach and "с 22 июля" in reach
        assert "Часы — активное время с 3 октября" in reach and "считаются одним (таких 1)" in reach
        oth = page.locator(f"{PANEL} #pu2-reach .pu2-res-oth")
        assert oth.locator("summary").inner_text() == "Не нашлись в штате · 2"
        oth.locator("summary").click()
        assert oth.locator("li").count() == 2 and "последний заход 4 ноября · вопросов 12" in oth.inner_text()
        fun = page.inner_text(f"{PANEL} #pu2-funnel")
        assert "Ответ получен" in fun and "Материалы в аудит-делах" in fun and "Выгрузки отзывов в Excel" in fun
        assert page.inner_text(f"{PANEL} #pu2-banks .pu2-res-sber") == "Сбербанк"
        assert "Ипотека" in page.inner_text(f"{PANEL} #pu2-dirs")
        costs = page.inner_text(f"{PANEL} #pu2-costs")
        assert "данные облака на" in costs and "gpt-oss-120b" in costs and "По месяцам" in costs
        assert page.locator(f"{PANEL} #pu2-costs .pu2-res-bars > span").count() == 14
        assert "заявок на источники" in page.inner_text(f"{PANEL} #pu2-feedback")
        page.get_by_role("button", name="Разметить новые").click()
        page.wait_for_selector(f"{PANEL} .pu2-res-msg")
        assert "Разметка запущена: 12 запросов" in page.inner_text(f"{PANEL} .pu2-res-msg")
        assert api_calls(page, "/api/admin/pulse/outcomes/label", method="POST")
        assert_page_ok(page)
        assert no_emoji(page)
        assert_no_hscroll(page)
        _shot(page, "results-1208-light")
    finally:
        page.close()


def test_results_pulse_user_narrow_dark_states(browser):
    page = open_pulse(browser, tab="outcomes", me="pulse_user", width=390, dark=True, wait="#pu2-funnel",
                      overrides={"/api/admin/pulse/reach": {"loaded": False},
                                 "/api/admin/pulse/costs": {"configured": False}})
    try:
        assert page.get_attribute(f"{PULSE} #pu2-tab-results", "aria-selected") == "true"
        assert page.get_by_role("button", name="Разметить новые").count() == 0
        reach = page.inner_text(f"{PANEL} #pu2-reach")
        assert "Штат не загружен" in reach and "загружает владелец" in reach
        assert page.locator(f"{PANEL} #pu2-reach button").count() == 0
        assert "Учёт трат не подключён" in page.inner_text(f"{PANEL} #pu2-costs")
        assert_page_ok(page)
        assert no_emoji(page)
        assert_no_hscroll(page)
        _shot(page, "results-390-dark")
    finally:
        page.close()


def test_results_load_error_has_retry(browser):
    page = open_pulse(browser, tab="results", wait="#pu2-reach",
                      overrides={"/api/admin/pulse/outcomes": (500, {"detail": "x"})})
    try:
        page.wait_for_selector(f"{PANEL} .pu2-err")
        assert "Не удалось посчитать итоги" in page.inner_text(f"{PANEL} .pu2-err")
        assert page.locator(f"{PANEL} .pu2-err button", has_text="Повторить").count() == 1
        assert_page_ok(page)
    finally:
        page.close()


def test_results_reach_pulse_user_sees_no_unmatched_list(browser):
    """Коллега с доступом к «Пульсу» видит свод, но не поимённый список «Не нашлись в штате»."""
    page = open_pulse(browser, tab="results", me="pulse_user", wait="#pu2-reach table")
    try:
        assert "Заходили" in page.inner_text(f"{PANEL} #pu2-reach")
        assert page.locator(f"{PANEL} #pu2-reach .pu2-res-oth").count() == 0
        assert_page_ok(page)
    finally:
        page.context.close()
