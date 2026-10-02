"""Браузер отображает обе стадии публикации из реального ответа каталога."""
from __future__ import annotations

from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.web import list_catalog
from tests.loophole import test_final_layout_runtime as runtime
from tests.loophole.test_preliminary_research_source_import import _create_import_schema

chromium_browser = runtime.browser


def test_catalog_renders_preliminary_and_published_findings(chromium_browser, session, monkeypatch):
    """Повторный фильтр в JSX по статусу скрыл бы часть результата."""
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
        # Статус публикации не фильтруется в JSX: в списке обе записи. Метку
        # «предварительно» список не выводит — на проде так помечена каждая запись.
        items = page.locator("#lp-panel-catalog .lp-c")
        assert items.count() == 2
        assert items.filter(has_text="Находка аналитика").count() == 1
        assert items.filter(has_text="Подтверждённый кейс").count() == 1
    finally:
        page.close()
