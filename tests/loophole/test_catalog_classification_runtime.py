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


@pytest.mark.parametrize("width", [1440, 900])
def test_browser_marks_filters_and_reloads_with_real_api(browser, client, session, width, tmp_path):
    access._access(session, role="ccks_expert")
    _create_import_schema(session)
    for key, category in [("Первый кейс", "vulnerability"), ("Второй кейс", "fraud_scheme")]:
        repo.insert_record(LoopholeRecord(
            sha256=key, title=key, snippet="Прочитанные условия", is_loophole=True,
            classification=category,
        ), session=session)
    # Каталог, права и маркировка используют настоящий API; прочие экраны не участвуют.
    html = runtime._runtime_html().replace(
        "<head>", "<head><script>window.__nativeFetch = window.fetch.bind(window);</script>",
    ).replace('<script type="text/babel">', """
        <script>
          const fixtureFetch = window.fetch;
          window.fetch = (input, init) => {
            const path = String(input);
            return path.includes('/catalog') || path.endsWith('/records/verdict')
              || path.endsWith('/contexts')
              ? window.__nativeFetch(input, init) : fixtureFetch(input, init);
          };
        </script><script type="text/babel">
    """)
    page = browser.new_page(viewport={"width": width, "height": 1000})
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

    page.route("http://catalog.test/**", respond)
    try:
        page.goto("http://catalog.test/")
        rows = page.locator("#lp-panel-catalog tbody tr:not(.lp-expanded-row)")
        expect(rows).to_have_count(2)
        record_filter = page.get_by_label("Тип записи", exact=True)
        record_filter.select_option("vulnerability")
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text("Первый кейс")
        page.get_by_title("Изменить вердикт").click()
        dialog = page.get_by_role("dialog", name="Вердикт записи")
        dialog.get_by_role("button", name=re.compile("^Мошенническая схема")).click()
        expect(page.get_by_text("Нет записей по выбранным фильтрам.")).to_be_visible()
        record_filter.select_option("fraud_scheme")
        expect(rows).to_have_count(2)
        expect(rows.first.locator(".lp-verdict-chip")).to_have_text("мошенническая схема")
        rows.filter(has_text="Первый кейс").get_by_title("Изменить вердикт").click()
        dialog.get_by_role("button", name="Ни то ни другое").click()
        expect(rows).to_have_count(1)
        record_filter.select_option("not_confirmed")
        expect(rows).to_have_count(1)
        expect(rows).to_contain_text("Первый кейс")
        page.get_by_title("Изменить вердикт").click()
        page.screenshot(path=str(tmp_path / f"classification-dialog-{width}.png"), full_page=True)
        dialog.get_by_role("button", name=re.compile("^Уязвимость")).click()
        expect(page.get_by_text("Нет записей по выбранным фильтрам.")).to_be_visible()
        page.reload()
        expect(rows).to_have_count(2)
        expect(record_filter).to_have_value("all")
        expect(rows.filter(has_text="Первый кейс").locator(".lp-verdict-chip")).to_have_text("уязвимость")
        page.screenshot(path=str(tmp_path / f"classification-catalog-{width}.png"), full_page=True)
        assert not errors
    finally:
        page.close()
