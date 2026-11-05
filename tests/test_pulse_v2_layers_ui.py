"""«Пульс» v2, слои: лист человека, отчёта и диалога (стенд tests/pulse_ui, API — фикстуры).

Проверяется (прежние PuUserCard/PuReportView/PuSessionView, CONTRACT §9.6):
- лист человека по ссылке &person=: имя, @логин, четыре числа, 14 дней, вкладки
  «Вопросы ИИ | Отчёты | Оценки | Ошибки | Хронология» со счётчиками;
- отчёт из листа человека ложится сверху: Esc закрывает только его, потом — лист;
- диалог: вопрос и ответ (разметка), «служебный доступ»;
- владелец помечает учётку служебной (POST, «Пульс» перезапрашивается); коллеге кнопки нет;
- «Текст письма» — у тех, кто перестал заходить; ошибка загрузки — «Повторить»;
- телефон, тёмная тема: без эмодзи и прокрутки вбок.
"""
from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.pulse_ui import harness as H
from tests.pulse_ui.harness import api_calls, assert_no_hscroll, assert_page_ok, no_emoji, open_pulse

DLG = ".rv-ovl-card[role=dialog]"


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b


def _person(browser, login="akovaleva", **kw):
    page = open_pulse(browser, tab="people", query=f"person={login}", width=kw.pop("width", 1440), **kw)
    page.wait_for_selector(f"{DLG} .pu2-ly, {DLG} .pu2-err", timeout=10000)
    return page


def _wait(page, prefix, n=1, method=None):
    for _ in range(100):
        got = api_calls(page, prefix, method)
        if len(got) >= n:
            return got
        page.wait_for_timeout(50)
    return api_calls(page, prefix, method)


def _text(loc) -> str:
    return re.sub(r"\s+", " ", loc.inner_text().replace(" ", " ")).strip()


def test_person_sheet_and_stacked_report(browser):
    page = _person(browser)
    card = H.load_fixture("admin_user_card.akovaleva.json")
    dlg = page.locator(DLG)
    t = _text(dlg)
    assert "Анна Ковалёва" in t and "@akovaleva" in t and "онлайн" in t
    assert _wait(page, "/api/admin/users/akovaleva?days=14")
    assert dlg.locator(".pu2-ly-k > div").count() == 4
    assert dlg.locator(".pu2-ly-day").count() == 14
    tabs = [_text(dlg.locator(".seg-btn").nth(i)) for i in range(5)]
    assert tabs == [f"Вопросы ИИ {len(card['questions'])}", f"Отчёты {len(card['reports'])}",
                    f"Оценки {len(card['ratings'])}", f"Ошибки {len(card['errors'])}",
                    f"Хронология {len(card['trail'])}"], tabs
    assert "akovaleva" in page.evaluate("location.hash")
    dlg.locator(".seg-btn", has_text="Отчёты").click()
    dlg.locator(".pu2-ly-row button", has_text="Открыть").first.click()
    page.wait_for_function("document.querySelectorAll('.rv-ovl-card[role=dialog]').length === 2")
    top = page.locator(DLG).last
    top.locator(".pu2-ly-md").wait_for()
    assert "служебный доступ" in _text(top)
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelectorAll('.rv-ovl-card[role=dialog]').length === 1")
    assert "@akovaleva" in _text(page.locator(DLG))
    page.keyboard.press("Escape")
    page.wait_for_selector(DLG, state="detached")
    assert "person=" not in page.evaluate("location.hash")
    assert_page_ok(page)
    page.context.close()


def test_session_sheet_from_rating(browser):
    card = H.load_fixture("admin_user_card.akovaleva.json")
    card["ratings"] = [{"kind": "ai_answer", "verdict": -1, "item_key": "msg:1", "at": "04.11 11:20",
                        "question": "Сколько жалоб на кредитные карты?", "comment": "цифры старые",
                        "title": None, "report_id": None, "session_id": "8790", "reasons": []}]
    page = _person(browser, overrides={"/api/admin/users/akovaleva": card})
    dlg = page.locator(DLG)
    dlg.locator(".seg-btn", has_text="Оценки").click()
    row = dlg.locator(".pu2-ly-row.bad")
    assert "«цифры старые»" in _text(row) and "не понравилось" in _text(row)
    row.locator("button", has_text="Диалог").click()
    page.wait_for_function("document.querySelectorAll('.rv-ovl-card[role=dialog]').length === 2")
    top = page.locator(DLG).last
    top.locator(".pu2-ly-msg.assistant").wait_for()
    assert top.locator(".pu2-ly-msg.assistant strong, .pu2-ly-msg.assistant b").count() >= 1
    assert "служебный доступ" in _text(top) and "Вопрос" in _text(top)
    page.context.close()


def test_owner_marks_service_account(browser):
    page = _person(browser)
    pulse0 = len(api_calls(page, "/api/admin/pulse"))
    page.locator(DLG).locator("button", has_text="Пометить служебной").click()
    post = _wait(page, "/api/admin/users/akovaleva/hidden", method="POST")
    assert post and post[0]["body"] == {"hidden": True}
    page.locator(DLG).locator(".pu2-badge", has_text="служебная").wait_for()
    assert len(_wait(page, "/api/admin/pulse", pulse0 + 1)) > pulse0
    page.wait_for_selector(f"{DLG} button:has-text('Снять пометку')")
    page.context.close()
    page = _person(browser, me="pulse_user")
    assert page.locator(DLG).locator("button", has_text="Пометить служебной").count() == 0
    page.context.close()


def test_winback_for_lost_person_and_error_state(browser):
    page = _person(browser, login="pmorozov")
    assert page.locator(DLG).locator("button", has_text="Текст письма").count() == 1
    page.context.close()
    page = _person(browser, overrides={"/api/admin/users/akovaleva": (500, {"detail": "x"})})
    assert page.locator(DLG).locator(".pu2-err button", has_text="Повторить").count() == 1
    page.context.close()


def test_person_sheet_phone_dark(browser):
    page = _person(browser, width=390, dark=True)
    assert_no_hscroll(page)
    no_emoji(page, DLG)
    assert_page_ok(page)
    page.context.close()
