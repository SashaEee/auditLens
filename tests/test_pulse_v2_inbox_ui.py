"""«Пульс» v2, раздел «Обращения» в браузере (стенд tests/pulse_ui, API — фикстуры).

Проверяется (CONTRACT §9–§10, прежние PuInbox/PuTicket/PuSliceFlags):
- список (по умолчанию «Открытые», ждущие первого ответа — сверху) и карточка; ссылка
  &ticket=118 открывает обращение, его снимки, контекст и переписку;
- ответ автору: шаблон, Ctrl+Enter, пустое сохранение — подсказка; после ответа список
  и «Пульс» перезапрашиваются (бейдж раздела);
- статус «Все» и тип — параметры запроса;
- «Отметки срезов»: view=flags в адресе, «Убрать» — POST с решением, 409 — сообщение;
  коллеге (403) вида нет;
- телефон: сначала список, обращение — отдельно, «Назад к списку»; без прокрутки вбок.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.pulse_ui import harness as H
from tests.pulse_ui.harness import api_calls, assert_no_hscroll, assert_page_ok, no_emoji, open_pulse

PANEL = '[data-page="pulse"] #pu2-panel'
CARD = f"{PANEL} #pu2-inbox"
LIST = f"{CARD} .pu2-ib-list"
DET = f"{CARD} #pu2-ticket"


@pytest.fixture(scope="module")
def browser():
    with H.browser_session() as b:
        yield b


def _open(browser, **kw):
    page = open_pulse(browser, tab="inbox", width=kw.pop("width", 1440), **kw)
    page.wait_for_selector(f"{CARD}", timeout=10000)
    return page


def _wait(page, prefix: str, n: int = 1, method: str | None = None) -> list:
    for _ in range(100):
        got = api_calls(page, prefix, method)
        if len(got) >= n:
            return got
        page.wait_for_timeout(50)
    return api_calls(page, prefix, method)


def test_list_and_ticket_deep_link(browser):
    page = _open(browser, query="ticket=118")
    page.wait_for_selector(f"{DET} .pu2-ib-shots img")
    assert "/api/admin/inbox?status=open" in page.calls
    assert _wait(page, "/api/admin/inbox/118")
    det = page.locator(DET).inner_text()
    assert "№ 118" in det and "фильтр по продукту сбрасывается" in det and "Приложено автоматически" in det
    assert page.locator(f"{DET} .pu2-ib-shots img").count() == 2
    assert page.locator(f"{LIST} button[data-ticket='118']").get_attribute("aria-current") == "true"
    # ждущие первого ответа — сверху
    items = page.locator(f"{LIST} .pu2-ib-ti")
    marks = ["Нужен первый ответ" in items.nth(i).inner_text() for i in range(items.count())]
    assert marks == sorted(marks, reverse=True) and marks[0], marks
    assert "&ticket=118" in page.evaluate("location.hash")
    page.locator(f"{DET} .pu2-ib-x").click()
    page.wait_for_selector(f"{DET} .pu2-ib-ph")
    assert "ticket=" not in page.evaluate("location.hash")
    assert_page_ok(page)
    page.context.close()


def test_reply_with_template_and_shortcut(browser):
    page = _open(browser, query="ticket=118")
    page.wait_for_selector(f"{DET} textarea")
    page.locator(f"{DET} button", has_text="Сохранить статус").click()
    assert "Выберите статус или напишите ответ" in page.locator(f"{DET} [role=alert]").inner_text()
    page.locator(f"{DET} .pu2-ib-tpl", has_text="Взяли в работу").click()
    ta = page.locator(f"{DET} textarea")
    assert "взяли в работу" in ta.input_value()
    lists0 = len(api_calls(page, "/api/admin/inbox?status=open"))
    pulse0 = len(api_calls(page, "/api/admin/pulse"))
    ta.focus()
    page.keyboard.press("Control+Enter")
    post = _wait(page, "/api/admin/inbox/118", method="POST")
    assert post and post[0]["body"]["reply"].startswith("Спасибо, взяли в работу")
    assert post[0]["body"]["status"] is None
    page.wait_for_selector(f"{DET} .pu2-ib-msg.team")
    assert _wait(page, "/api/admin/inbox?status=open", lists0 + 1)
    assert len(_wait(page, "/api/admin/pulse", pulse0 + 1)) > pulse0
    page.wait_for_selector("text=Ответ ушёл автору")
    page.context.close()


def test_status_and_kind_filters(browser):
    page = _open(browser)
    page.wait_for_selector(f"{LIST} .pu2-ib-ti")
    page.locator(f"{CARD} .seg-btn", has_text="Все").click()
    assert _wait(page, "/api/admin/inbox?status=all")
    page.wait_for_function(f"document.querySelectorAll('{LIST} .pu2-ib-ti').length === 18")
    page.select_option(f"{CARD} .pu2-ib-bar select", "bug")
    assert _wait(page, "/api/admin/inbox?status=all&kind=bug")
    # без выбранного обращения — подсказка, а не «пустая» карточка
    assert page.locator(f"{DET} .pu2-ib-ph").count() == 1
    page.context.close()


def test_slice_flags_view(browser):
    page = _open(browser, query="view=flags",
                 overrides={"POST /api/reviews/slice-flags/72": (409, {"detail": "уже решено"})})
    page.wait_for_selector(f"{CARD} #pu2-flags tbody tr")
    n = len(H.load_fixture("reviews_slice_flags.json")["items"])
    assert page.locator(f"{CARD} #pu2-flags tbody tr").count() == n
    page.locator(f"{CARD} tr[data-flag='71'] button", has_text="Убрать").click()
    post = _wait(page, "/api/reviews/slice-flags/71", method="POST")
    assert post[0]["body"] == {"decision": "excluded"}
    page.wait_for_selector("text=Жалоба убрана из среза")
    page.locator(f"{CARD} tr[data-flag='72'] button", has_text="Оставить").click()
    page.wait_for_selector("text=По этой отметке уже решили — список обновлён")
    page.context.close()
    # коллеге отметки не положены (403) — вида нет
    page = _open(browser, me="pulse_user", query="view=flags")
    page.wait_for_selector(f"{LIST} .pu2-ib-ti")
    assert page.locator(f"{CARD} .seg-btn", has_text="Отметки срезов").count() == 0
    assert page.locator(f"{CARD} #pu2-flags").count() == 0
    page.context.close()


def test_phone_list_then_ticket(browser):
    page = _open(browser, width=390, dark=True)
    page.wait_for_selector(f"{LIST} .pu2-ib-ti")
    assert page.locator(f"{CARD} .pu2-ib-det").first.is_hidden()
    page.locator(f"{LIST} button[data-ticket='118']").click()
    page.wait_for_selector(f"{DET} textarea")
    assert page.locator(LIST).is_hidden()
    assert_no_hscroll(page)
    no_emoji(page)
    page.locator(f"{DET} button", has_text="Назад к списку").click()
    page.wait_for_selector(f"{LIST} .pu2-ib-ti")
    assert page.locator(LIST).is_visible()
    assert_page_ok(page)
    page.context.close()
