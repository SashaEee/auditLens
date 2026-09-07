"""Браузер отображает обе стадии публикации из реального ответа каталога."""
from __future__ import annotations

from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.web import list_catalog
from tests.loophole import test_final_layout_runtime as runtime
from tests.loophole.test_preliminary_research_source_import import _create_import_schema

chromium_browser = runtime.browser


def test_catalog_renders_preliminary_and_published_findings(chromium_browser, session, monkeypatch):
    """Повторный фильтр в JSX или потеря подписи preliminary скрывают результат."""
    _create_import_schema(session)
    for status, title in [
        ("preliminary", "Находка аналитика"),
        ("published", "Подтверждённый кейс"),
    ]:
        repo.insert_record(
            LoopholeRecord(sha256=status, title=title, status=status, is_loophole=True),
            session=session,
        )
    monkeypatch.setattr(runtime, "RECORDS", list_catalog(session=session)["records"])

    page = runtime._open(chromium_browser)
    try:
        rows = page.locator("#lp-panel-catalog tbody tr")
        assert rows.count() == 2
        assert rows.filter(has_text="Находка аналитика").locator(".lp-status").inner_text() == (
            "предварительно"
        )
        assert rows.filter(has_text="Подтверждённый кейс").locator(".lp-status").inner_text() == (
            "подтверждено"
        )
    finally:
        page.close()
