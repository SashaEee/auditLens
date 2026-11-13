"""«Пульс» v2 в браузере: раздел «Качество ИИ».

Стенд — tests/pulse_ui/harness.py. Проверяется: проверочный набор (ответ/отчёт, итог и
изменение к прошлому прогону той же модели), прогоны и оценки ответов, «Что не прошло
проверку» (только непрошедшие, провал первым, строка раскрывает ответ) и история прогонов,
«Запустить прогон» — только владельцу, жалобы одной лентой, вопросы без шанса на результат
за период страницы, отчёты и ответы (вид, «С жалобами», поиск); прежний ключ
#pulse?tab=ai ведёт сюда; ширины 1208 светлая и 390 тёмная без ошибок и прокрутки вбок.

Снимки: PULSE_SHOTS=<каталог> python -m pytest tests/test_pulse_v2_quality_ui.py
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


def test_quality_owner_blocks_and_actions(browser):
    page = open_pulse(browser, tab="quality", wait="#pu2-cases")
    try:
        ev = page.inner_text(f"{PANEL} #pu2-eval")
        assert "81,5" in ev and "из 100" in ev and "к прогону 26 октября" in ev
        # карточка прогонов — по заголовку: с 11.11 перед ней «Отчёт: задачи и шаги»
        runs = page.locator(f"{PANEL} .pu2-grid > section", has_text="Прогоны ИИ-помощника")
        assert "из 303" in runs.inner_text()
        rt = page.inner_text(f"{PANEL} #pu2-ratings")
        assert "Хорошо" in rt and "21" in rt and "Плохо" in rt and "Доли не считаем" in rt
        # только непрошедшие вопросы, провал первым; строка раскрывает ответ агента
        rows = page.locator(f"{PANEL} #pu2-cases table").first.locator("tbody tr")
        assert rows.count() == 4
        assert rows.first.locator(".pu2-badge").inner_text() == "провал"
        rows.first.click()
        page.wait_for_selector(f"{PANEL} #pu2-cases .pu2-qa-ans")
        assert "Прогоны" in page.inner_text(f"{PANEL} #pu2-cases")
        page.get_by_role("button", name="Запустить прогон").click()
        page.wait_for_selector(f"{PANEL} #pu2-cases [role=status]")
        post = [p for p in page.posts if p["url"].startswith("/api/admin/agent-eval")]
        assert post and post[-1]["body"] == {"engine": "quick"}
        page.locator(f'{PANEL} [aria-label="Режим ИИ-помощника"] .seg-btn', has_text="Отчёт").click()
        page.wait_for_function("() => document.querySelector('#pu2-eval').innerText.includes('из 100')")
        assert api_calls(page, "/api/admin/agent-eval?engine=deep")
        # жалобы и вопросы без шанса — за период страницы
        cmp = page.inner_text(f"{PANEL} #pu2-complaints")
        assert "Дмитрий Орлов" in cmp and "Открыть отчёт" in cmp
        hp = page.inner_text(f"{PANEL} #pu2-hopeless")
        assert "45" in hp and "из 312" in hp and "Слишком общие" in hp and "Аудит уязвимостей" in hp
        days = page.evaluate("localStorage.getItem('al-pulse-days')") or "14"
        assert api_calls(page, f"/api/admin/pulse/outcomes?days={days}")
        assert api_calls(page, f"/api/admin/complaints?days={days}")
        # отчёты и ответы: вид, поиск, «С жалобами» — запрос с only_bad
        rep = f"{PANEL} #pu2-reports"
        assert page.locator(f"{rep} .pu2-lnk", has_text="Показать все · 105").count() == 1
        page.locator(f'{rep} [aria-label="Что показывать"] .seg-btn', has_text="Только отчёты").click()
        kinds = page.locator(f"{rep} tbody tr td:nth-child(3)").all_inner_texts()
        assert kinds and set(kinds) == {"отчёт"}
        page.fill(f"{rep} input[type=search]", "вклад")
        assert "вклад" in page.inner_text(f"{rep} tbody").lower()
        page.locator(f"{rep} .chip", has_text="С жалобами").click()
        page.wait_for_function("() => [...document.querySelectorAll('#pu2-reports .chip')].some(b => b.getAttribute('aria-pressed') === 'true')")
        assert [u for u in page.calls if u.startswith("/api/admin/reports") and "only_bad=true" in u]
        page.fill(f"{rep} input[type=search]", "")
        assert_page_ok(page)
        assert no_emoji(page)
        assert_no_hscroll(page)
        _shot(page, "quality-1208-light")
    finally:
        page.close()


def test_quality_pulse_user_old_key_narrow_dark(browser):
    page = open_pulse(browser, tab="ai", me="pulse_user", width=390, dark=True, wait="#pu2-cases")
    try:
        assert page.get_attribute(f"{PULSE} #pu2-tab-quality", "aria-selected") == "true"
        assert page.get_by_role("button", name="Запустить прогон").count() == 0
        page.wait_for_selector(f"{PANEL} #pu2-reports tbody tr")
        assert_page_ok(page)
        assert no_emoji(page)
        assert_no_hscroll(page)
        _shot(page, "quality-390-dark")
    finally:
        page.close()
