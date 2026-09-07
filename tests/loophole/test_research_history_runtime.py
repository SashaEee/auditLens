"""История AI-аналитика: реальные React/Babel и браузер без внешних сервисов."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Browser, expect, sync_playwright

from tests.loophole.test_final_layout_runtime import _runtime_html


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def _open_history(
    browser, *, query="", read_only=True, empty=False, width=1440, initial_list_failure=False,
    first_name="Тарифы ВТБ"
):
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.set_default_timeout(10_000)
    stub = """
      const originalFetch = window.fetch;
      window.__historyRequests = [];
      window.__createCount = 0;
      window.__historyDelay = {};
      window.__historyFailure = {};
      window.__listFailure = LIST_FAILURE;
      window.__holdNextList = false;
      window.__releaseHeldList = null;
      window.__deleteFailure = false;
      window.__createFailure = false;
      window.__directReports = [];
      window.__chatDelay = 0;
      window.__workspaces = EMPTY ? [] : [
        {workspace_id: 2, name: FIRST_NAME, created_at: "2026-09-06T10:00:00Z",
         last_active_at: "2026-09-07T10:00:00Z"},
        {workspace_id: 1, name: "Кредитные карты", created_at: "2026-09-05T10:00:00Z",
         last_active_at: "2026-09-06T10:00:00Z"}
      ];
      const json = (value, status = 200) => new Response(JSON.stringify(value), {
        status, headers: {"Content-Type": "application/json"}
      });
      const historyPayload = (id, readOnly = false) => ({
        workspace: window.__workspaces.find(w => w.workspace_id === id)
          || {workspace_id: id, name: "Исследование коллеги"},
        read_only: readOnly,
        messages: id > 2 ? [] : [
          {message_id: id * 10, role: "user", content: "Вопрос исследования " + id},
          {message_id: id * 10 + 1, role: "assistant", content: "Ответ исследования " + id,
           report_id: id * 100}
        ],
        reports: id > 2 ? [] : [
          {report_id: id * 100 + 1, query: "Старый отчёт " + id,
           result: "Сохранённый прежний вывод " + id, created_at: "2026-09-06T10:00:00Z"},
          {report_id: id * 100, query: "Запрос " + id, result: "Ответ исследования " + id,
           created_at: "2026-09-07T10:00:00Z"}
        ]
      });
      window.fetch = async (input, init = {}) => {
        const url = String(input);
        const method = init.method || "GET";
        window.__historyRequests.push({url, method});
        if (url.endsWith("/workspaces")) {
          const response = window.__listFailure ? json({}, 503) : json({workspaces: window.__workspaces});
          if (window.__holdNextList) {
            window.__holdNextList = false;
            await new Promise(resolve => { window.__releaseHeldList = resolve; });
          }
          return response;
        }
        if (url.endsWith("/workspace") && method === "POST") {
          window.__createCount += 1;
          if (window.__createFailure) return json({}, 503);
          const workspace_id = 2 + window.__createCount;
          window.__workspaces.unshift({workspace_id, name: "Новое исследование"});
          return json({workspace_id});
        }
        const history = url.match(/\\/history\\/(\\d+)$/);
        if (history) {
          const id = Number(history[1]);
          await new Promise(resolve => setTimeout(resolve, window.__historyDelay[id] || 0));
          return window.__historyFailure[id]
            ? json({}, window.__historyFailure[id]) : json(historyPayload(id));
        }
        if (url.includes("/shared/")) {
          return url.endsWith("/missing") ? json({}, 404) : json(historyPayload(2, READ_ONLY));
        }
        if (url.endsWith("/share")) {
          return json({share_url: "/static/loophole/loophole.html?share=shared-token"});
        }
        const deletion = url.match(/\\/workspace\\/(\\d+)$/);
        if (deletion && method === "DELETE") {
          if (window.__deleteFailure) return json({}, 503);
          window.__workspaces = window.__workspaces.filter(w => w.workspace_id !== Number(deletion[1]));
          return json({deleted: true});
        }
        if (url.endsWith("/chat")) {
          await new Promise(resolve => setTimeout(resolve, window.__chatDelay));
          if (window.__directReports.length) {
            const report = window.__directReports.shift();
            const body = JSON.parse(init.body);
            window.__chatBodies.push(body);
            const newline = String.fromCharCode(10);
            const events = [
              ["phase", {phase: "execute"}],
              ["token", {text: "Ответ на " + body.message}],
              ["report", {report_id: report}],
              ["done", {}]
            ];
            return new Response(events.map(entry => "event: " + entry[0] + newline
              + "data: " + JSON.stringify(entry[1])).join(newline + newline) + newline + newline,
              {status: 200, headers: {"Content-Type": "text/event-stream"}});
          }
        }
        return originalFetch(input, init);
      };
    """.replace("EMPTY", json.dumps(empty)).replace("READ_ONLY", json.dumps(read_only))
    stub = stub.replace("FIRST_NAME", json.dumps(first_name))
    stub = stub.replace("LIST_FAILURE", json.dumps(initial_list_failure))
    html = _runtime_html().replace(
        '<script type="text/babel">', f'<script>{stub}</script><script type="text/babel">'
    )
    page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
    page.goto("http://loophole.test/static/loophole/loophole.html" + query)
    page.get_by_role("tab", name="Общая база").wait_for()
    if not query:
        page.get_by_role("tab", name="AI-исследования").click()
    return page


def test_latest_research_restores_messages_and_legacy_reports_without_creating(browser):
    page = _open_history(browser)
    try:
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 2")
        expect(page.get_by_role("heading", name="История загружена")).to_be_visible()
        expect(page.get_by_label("Сохранённый результат")).to_have_value("200")
        assert page.evaluate("window.__createCount") == 0
        page.get_by_label("Сохранённый результат").select_option("201")
        expect(page.locator(".lp-research-evidence")).to_contain_text("Сохранённый прежний вывод 2")
        page.get_by_role("button", name="Открыть исследование Кредитные карты").click()
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 1")
        expect(page.get_by_label("Сообщение аналитику")).to_be_enabled()
        page.get_by_label("Сообщение аналитику").fill("Продолжить исследование")
        page.get_by_role("button", name="Отправить сообщение").click()
        page.wait_for_function("window.__chatBodies.length === 1")
        body = page.evaluate("window.__chatBodies[0]")
        assert body["workspace_id"] == 1
        assert body["history"][0]["content"] == "Вопрос исследования 1"
        assert body["history"][1]["content"] == "Ответ исследования 1"
    finally:
        page.close()


def test_history_is_left_rail_with_date_limited_title_and_delete_cross(browser):
    full_name = "Исследование условий кредитных карт с длинным названием для проверки списка истории"
    displayed_name = full_name[:63] + "…"
    assert len(displayed_name) == 64
    page = _open_history(browser, first_name=full_name)
    try:
        history = page.locator(".lp-research-history")
        content = page.locator(".lp-research-content")
        item = page.locator(".lp-research-history-item").first
        title = item.locator(".lp-research-history-open strong")
        delete = page.get_by_role("button", name=f"Удалить исследование {full_name} из истории")

        expect(title).to_have_text(displayed_name)
        expect(title).to_have_attribute("title", full_name)
        expect(item.locator("time")).to_have_attribute("datetime", "2026-09-07T10:00:00Z")

        history_box = history.bounding_box()
        content_box = content.bounding_box()
        item_box = item.bounding_box()
        delete_box = delete.bounding_box()
        assert history_box and content_box and item_box and delete_box
        assert history_box["x"] < content_box["x"]
        assert delete_box["x"] > item_box["x"] + item_box["width"] - delete_box["width"] - 8
        assert delete_box["y"] <= item_box["y"] + 8

        delete.click()
        dialog = page.get_by_role("dialog", name="Удалить исследование из истории?")
        dialog.get_by_role("button", name="Удалить", exact=True).click()
        expect(dialog).to_have_count(0)
        assert page.evaluate("window.__workspaces.map(workspace => workspace.workspace_id)") == [1]
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 1")
    finally:
        page.close()


def test_new_research_clears_clarification_draft_and_report(browser):
    page = _open_history(browser)
    try:
        composer = page.get_by_label("Сообщение аналитику")
        expect(composer).to_be_enabled()
        composer.fill("Новый вопрос")
        page.get_by_role("button", name="Отправить сообщение").click()
        expect(composer).to_have_attribute("placeholder", "Ответ на уточняющий вопрос…")
        composer.fill("Черновик уточнения")
        page.get_by_role("button", name="Новое исследование", exact=True).click()
        expect(composer).to_have_value("")
        expect(composer).to_have_attribute("placeholder", "Сообщение аналитику…")
        assert page.locator(".lp-bubble").count() == 0
        assert page.get_by_label("Сохранённый результат").count() == 0
        assert page.evaluate("window.__createCount") == 1
    finally:
        page.close()


@pytest.mark.parametrize("read_only", [True, False])
def test_shared_link_restores_history_and_respects_author_rights(browser, read_only):
    page = _open_history(browser, query="?share=shared-token", read_only=read_only)
    try:
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 2")
        assert page.evaluate("window.__createCount") == 0
        assert page.get_by_label("Сообщение аналитику").count() == (0 if read_only else 1)
        assert page.get_by_role("button", name="Поделиться", exact=True).count() == (0 if read_only else 1)
        assert page.locator(".lp-research-history-delete").count() == 2
        if read_only:
            expect(page.get_by_text("Исследование доступно только для чтения.", exact=True)).to_be_visible()
            assert page.get_by_role("button", name="Добавить в общую базу").count() == 0
            page.get_by_role("button", name="Новое исследование", exact=True).click()
            expect(page.get_by_label("Сообщение аналитику")).to_be_enabled()
            assert "share=" not in page.url
    finally:
        page.close()


def test_share_has_absolute_copyable_fallback(browser):
    page = _open_history(browser)
    try:
        page.get_by_role("button", name="Поделиться", exact=True).click()
        expect(page.get_by_label("Ссылка на исследование")).to_have_value(
            "http://loophole.test/static/loophole/loophole.html?share=shared-token"
        )
    finally:
        page.close()


def test_delete_keeps_item_on_failure_and_restores_remaining_on_success(browser):
    page = _open_history(browser)
    try:
        page.evaluate("window.__deleteFailure = true")
        page.get_by_role("button", name="Удалить исследование Тарифы ВТБ из истории").click()
        dialog = page.get_by_role("dialog", name="Удалить исследование из истории?")
        expect(dialog.get_by_role("button", name="Отмена")).to_be_focused()
        expect(dialog).to_contain_text("Данные сохранятся в системе")
        dialog.get_by_role("button", name="Удалить", exact=True).click()
        expect(dialog.get_by_role("alert")).to_be_visible()
        expect(page.get_by_role("button", name="Открыть исследование Тарифы ВТБ")).to_be_visible()
        page.evaluate("window.__deleteFailure = false")
        dialog.get_by_role("button", name="Удалить", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 1")
        assert page.get_by_role("button", name="Открыть исследование Тарифы ВТБ").count() == 0
        expect(page.get_by_role("tab", name="AI-исследования")).to_be_focused()
    finally:
        page.close()


def test_late_history_response_cannot_replace_newer_selection(browser):
    page = _open_history(browser)
    try:
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 2")
        page.evaluate("window.__historyDelay[1] = 600")
        page.get_by_role("button", name="Открыть исследование Кредитные карты").click()
        page.get_by_role("button", name="Открыть исследование Тарифы ВТБ").click()
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 2")
        page.wait_for_timeout(750)
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 2")
    finally:
        page.close()


def test_delayed_list_response_cannot_restore_deleted_research(browser):
    page = _open_history(browser)
    try:
        composer = page.get_by_label("Сообщение аналитику")
        expect(composer).to_be_enabled()
        page.evaluate("window.__directReports = [301]; window.__holdNextList = true")
        composer.fill("Запрос перед удалением")
        page.get_by_role("button", name="Отправить сообщение").click()
        page.wait_for_function("window.__releaseHeldList !== null")
        expect(composer).to_be_enabled()
        expect(page.get_by_text("Загрузка списка…", exact=True)).to_be_visible()
        page.get_by_role("button", name="Удалить исследование Тарифы ВТБ из истории").click()
        dialog = page.get_by_role("dialog", name="Удалить исследование из истории?")
        dialog.get_by_role("button", name="Удалить", exact=True).click()
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 1")
        deleted_item = page.get_by_role("button", name="Открыть исследование Тарифы ВТБ")
        expect(deleted_item).to_have_count(0)
        loading_after_delete = page.get_by_text("Загрузка списка…", exact=True).is_visible()
        assert page.evaluate("window.__workspaces.map(workspace => workspace.workspace_id)") == [1]
        page.evaluate("""async () => {
          window.__releaseHeldList();
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""")
        expect(deleted_item).to_have_count(0)
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 1")
        assert loading_after_delete is False
        expect(page.get_by_text("Загрузка списка…", exact=True)).to_have_count(0)
    finally:
        page.close()


def test_busy_chat_prevents_switch_and_creation(browser):
    page = _open_history(browser)
    try:
        composer = page.get_by_label("Сообщение аналитику")
        expect(composer).to_be_enabled()
        page.evaluate("window.__chatDelay = 800")
        composer.fill("Новый вопрос")
        page.get_by_role("button", name="Отправить сообщение").click()
        expect(page.get_by_role("button", name="Новое исследование", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Открыть исследование Кредитные карты")).to_be_disabled()
    finally:
        page.close()


def test_history_error_has_retry_and_no_editable_stale_messages(browser):
    page = _open_history(browser)
    try:
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 2")
        page.evaluate("window.__historyFailure[1] = 503")
        page.get_by_role("button", name="Открыть исследование Кредитные карты").click()
        expect(page.get_by_role("button", name="Повторить загрузку исследования")).to_be_visible()
        assert page.locator(".lp-bubble").count() == 0
        page.evaluate("window.__historyFailure[1] = 0")
        page.get_by_role("button", name="Повторить загрузку исследования").click()
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 1")
    finally:
        page.close()


def test_deleted_shared_link_has_explicit_error_and_new_research_action(browser):
    page = _open_history(browser, query="?share=missing")
    try:
        expect(page.get_by_role("button", name="Повторить загрузку исследования")).to_be_visible()
        expect(page.locator(".lp-research-history-error")).to_contain_text("недоступно")
        assert page.get_by_label("Сообщение аналитику").count() == 0
        page.get_by_role("button", name="Новое исследование", exact=True).click()
        expect(page.get_by_label("Сообщение аналитику")).to_be_enabled()
    finally:
        page.close()


def test_initial_list_failure_retries_without_creating_empty_research(browser):
    page = _open_history(browser, initial_list_failure=True)
    try:
        retry = page.get_by_role("button", name="Повторить загрузку истории")
        expect(retry).to_be_visible()
        assert page.evaluate("window.__createCount") == 0
        assert page.get_by_label("Сообщение аналитику").count() == 0
        page.evaluate("window.__listFailure = false")
        retry.click()
        expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ исследования 2")
        assert page.evaluate("window.__createCount") == 0
    finally:
        page.close()


def test_empty_history_creates_once_and_delete_last_creates_fresh_workspace(browser):
    page = _open_history(browser, empty=True)
    try:
        expect(page.get_by_label("Сообщение аналитику")).to_be_enabled()
        assert page.evaluate("window.__createCount") == 1
        page.get_by_role("tab", name="Общая база").click()
        page.get_by_role("tab", name="AI-исследования").click()
        assert page.evaluate("window.__createCount") == 1
        page.get_by_role("button", name="Удалить исследование Новое исследование из истории").click()
        dialog = page.get_by_role("dialog", name="Удалить исследование из истории?")
        dialog.get_by_role("button", name="Удалить", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(page.get_by_label("Сообщение аналитику")).to_be_enabled()
        assert page.evaluate("window.__createCount") == 2
        assert page.evaluate("window.__workspaces.map(w => w.workspace_id)") == [4]
    finally:
        page.close()


def test_failed_creation_retry_repeats_creation_instead_of_opening_old_history(browser):
    page = _open_history(browser)
    try:
        expect(page.get_by_label("Сообщение аналитику")).to_be_enabled()
        page.evaluate("window.__createFailure = true")
        page.get_by_role("button", name="Новое исследование", exact=True).click()
        retry = page.get_by_role("button", name="Повторить загрузку исследования")
        expect(retry).to_be_visible()
        assert page.evaluate("window.__createCount") == 1
        page.evaluate("window.__createFailure = false")
        retry.click()
        expect(page.locator(".lp-bubble")).to_have_count(0)
        expect(page.get_by_label("Сообщение аналитику")).to_be_enabled()
        assert page.evaluate("window.__createCount") == 2
    finally:
        page.close()


def test_selecting_earlier_live_report_restores_its_original_query(browser):
    page = _open_history(browser)
    try:
        composer = page.get_by_label("Сообщение аналитику")
        expect(composer).to_be_enabled()
        page.evaluate("window.__directReports = [301, 302]")
        for query in ["Первый новый запрос", "Второй новый запрос"]:
            composer.fill(query)
            page.get_by_role("button", name="Отправить сообщение").click()
            expect(page.locator(".lp-bubble-content").last).to_have_text("Ответ на " + query)
            expect(composer).to_be_enabled()
        page.get_by_label("Сохранённый результат").select_option("301")
        expect(page.locator(".lp-research-evidence")).to_contain_text("Ответ на Первый новый запрос")
        expect(page.get_by_role("region", name="Текущий запрос")).to_contain_text("Первый новый запрос")
        expect(page.get_by_role("region", name="Текущий запрос")).not_to_contain_text("Второй новый запрос")
    finally:
        page.close()


@pytest.mark.parametrize("width", [1440, 992, 390])
@pytest.mark.parametrize("dark", [False, True])
def test_shared_history_layout_and_report_selector_remain_accessible(browser, width, dark):
    page = _open_history(browser, query="?share=shared-token", width=width)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        expect(page.get_by_label("Сохранённый результат")).to_have_value("200")
        page.evaluate("dark => document.documentElement.classList.toggle('dark', dark)", dark)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_label("Сохранённый результат").select_option("201")
        expect(page.locator(".lp-research-evidence")).to_contain_text("Сохранённый прежний вывод 2")
        if width < 1100:
            page.get_by_role("button", name="Открыть чат").click()
        expect(page.locator(".lp-bubble-role").first).to_have_text("Автор")
        assert page.get_by_label("Сообщение аналитику").count() == 0
        assert not errors
    finally:
        page.close()
