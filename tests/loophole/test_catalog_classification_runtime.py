"""Реальный JSX и HTTP API с SQLite: категории переживают смену фильтра и reload."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from tests.loophole import test_final_layout_runtime as runtime
from tests.loophole import test_record_verdict_authorization as access
from tests.loophole.test_preliminary_research_source_import import _create_import_schema

session = access.session
client = access.client
browser = runtime.browser


class _FakeLLM:
    async def ainvoke(self, messages):
        class _Answer:
            content = "Суть: механизм и кто теряет."
        return _Answer()


@pytest.mark.parametrize("width", [1440, 900])
def test_browser_marks_filters_and_reloads_with_real_api(browser, client, session, width, tmp_path,
                                                        monkeypatch):
    from bank_audit.loophole import summary as summary_mod

    monkeypatch.setattr(summary_mod, "_default_llm", lambda: _FakeLLM())
    access._access(session, role="ccks_expert")
    _create_import_schema(session)
    for key, category in [("Первый кейс", "vulnerability"), ("Второй кейс", "fraud_scheme")]:
        repo.insert_record(LoopholeRecord(
            sha256=key, title=key, snippet="Прочитанные условия", is_loophole=True,
            classification=category,
        ), session=session)
    # База, очередь, карточки, суть, права и маркировка — настоящий API; прочие экраны
    # (история исследований) остаются на заглушке стенда.
    html = runtime._runtime_html().replace(
        "<head>", "<head><script>window.__nativeFetch = window.fetch.bind(window);</script>",
    ).replace('<script type="text/babel">', """
        <script>
          const fixtureFetch = window.fetch;
          window.fetch = (input, init) => {
            const path = String(input);
            return path.includes('/catalog') || path.includes('/records/') || path.includes('/queue')
              || path.endsWith('/contexts')
              ? window.__nativeFetch(input, init) : fixtureFetch(input, init);
          };
        </script><script type="text/babel">
    """)
    page = browser.new_page(viewport={"width": width, "height": 1000})
    page.set_default_timeout(15_000)
    page.clock.install()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def respond(route):
        request = route.request
        path = request.url.removeprefix("http://catalog.test")
        if path.startswith("/api/"):
            response = client.request(
                request.method, path, headers={**access._HEADERS, "Content-Type": "application/json"},
                content=request.post_data,
            )
            route.fulfill(status=response.status_code, content_type="application/json", body=response.text)
        else:
            route.fulfill(content_type="text/html", body=html)

    def show(kind):
        page.get_by_role("group", name="Тип записи").get_by_role(
            "button", name=re.compile("^" + kind + r"(?! и)")).click()

    def open_record(title):
        items.filter(has_text=title).click()
        expect(page.locator(".lp-rd .lp-rd-title")).to_have_text(title)

    def change_verdict(kind, comment):
        page.locator(".lp-rd").get_by_role("button", name="Изменить вердикт").click()
        dialog = page.get_by_role("dialog", name="Изменить вердикт")
        dialog.get_by_role("radio", name=re.compile("^" + kind)).click()
        dialog.get_by_label("Комментарий эксперта").fill(comment)
        dialog.get_by_role("button", name="Сохранить").click()
        expect(dialog).to_have_count(0)

    page.route("http://catalog.test/**", respond)
    try:
        page.goto("http://catalog.test/")
        items = page.locator("#lp-panel-catalog .lp-c")
        expect(items).to_have_count(2)
        show("Уязвимости")
        expect(items).to_have_count(1)
        expect(items).to_contain_text("Первый кейс")
        # Находка модели без решения эксперта решается в очереди, с отменой 10 секунд.
        open_record("Первый кейс")
        page.locator(".lp-rd").get_by_role("button", name="Решить в очереди").click()
        expect(page.locator(".lp-qcard .lp-rd-title")).to_have_text("Первый кейс")
        page.get_by_role("radio", name=re.compile("^Мошенническая схема")).click()
        page.get_by_label("Комментарий участника ЦК").fill("Схема обмана, не лазейка")
        page.get_by_role("button", name="Сохранить решение").click()
        page.clock.run_for(10_500)
        expect(page.locator(".lp-qi")).to_have_count(1)
        page.get_by_role("tab", name="База").click()
        show("Уязвимости")
        expect(page.get_by_text("Ничего не нашлось")).to_be_visible()
        show("Схемы")
        expect(items).to_have_count(2)
        open_record("Первый кейс")
        expect(page.locator(".lp-rd")).to_contain_text("Проверено экспертом")
        expect(page.locator(".lp-rd .lp-kind")).to_have_text("Мошенническая схема")
        # Проверенную запись эксперт перемаркирует прямо в базе.
        change_verdict("Не подтверждено", "Повторная проверка: признаков нет")
        show("Не подтверждено")
        expect(items).to_have_count(1)
        expect(items).to_contain_text("Первый кейс")
        open_record("Первый кейс")
        page.screenshot(path=str(tmp_path / f"classification-reader-{width}.png"), full_page=True)
        change_verdict("Уязвимость", "Всё же лазейка в условиях")
        page.reload()
        expect(items).to_have_count(2)
        # После перезагрузки — фильтр по умолчанию: только находки.
        expect(page.get_by_role("group", name="Тип записи").get_by_role(
            "button", name=re.compile("^Уязвимости и схемы"))).to_have_attribute("aria-pressed", "true")
        expect(items.filter(has_text="Первый кейс").locator(".lp-kind")).to_have_text("Уязвимость")
        page.screenshot(path=str(tmp_path / f"classification-catalog-{width}.png"), full_page=True)
        assert not errors
    finally:
        page.close()
