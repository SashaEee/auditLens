"""Проверки прав ручной маркировки на реальном JSX в Chromium."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "bank_audit" / "loophole" / "static"
VENDOR = ROOT / "src" / "bank_audit" / "web" / "static" / "vendor"


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def _open(browser: Browser, *, capability=None, protected_context=None, verdict_status=200):
    contexts = [{"id": "catalog", "title": "Общая база"}]
    if protected_context:
        contexts.append(protected_context)
    authorization = {"contexts": contexts}
    if capability is not None:
        authorization["capabilities"] = {"can_mark_verdict": capability}
    stub = """
      window.__verdictRequests = [];
      window.__record = {
        record_id: 1, title: "Проверяемая лазейка", bank_slug: "sber",
        is_loophole: true, status: "published", verdict_confidence: 0.9,
      };
      window.fetch = async (input, init = {}) => {
        const url = String(input);
        const json = (value, status = 200) => new Response(JSON.stringify(value), {
          status, headers: {"Content-Type": "application/json"},
        });
        if (url.endsWith("/contexts")) return json(AUTHORIZATION);
        if (url.endsWith("/workspace")) return json({workspace_id: 1});
        if (url.endsWith("/banks")) return json({banks: ["sber"]});
        if (url.includes("/catalog") || url.endsWith("/queue")) {
          return json({records: [window.__record]});
        }
        if (url.endsWith("/records/verdict")) {
          const body = JSON.parse(init.body);
          window.__verdictRequests.push(body);
          if (VERDICT_STATUS !== 200) {
            return json({detail: "Нет права изменять вердикт."}, VERDICT_STATUS);
          }
          window.__record.is_loophole = body.is_loophole;
          return json({updated: 1, skipped: []});
        }
        return json({});
      };
    """.replace("AUTHORIZATION", json.dumps(authorization, ensure_ascii=False)).replace(
        "VERDICT_STATUS", str(verdict_status)
    )
    scripts = "".join(
        f"<script>{(VENDOR / name).read_text(encoding='utf-8')}</script>"
        for name in ("react.min.js", "react-dom.min.js", "babel.min.js")
    )
    jsx = (STATIC / "loophole.jsx").read_text(encoding="utf-8")
    css = (STATIC / "loophole.css").read_text(encoding="utf-8")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.set_default_timeout(10_000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f'<style>{css}</style></head><body><div id="loophole-root"></div>{scripts}'
        f'<script>{stub}</script><script type="text/babel">{jsx}</script></body></html>',
        wait_until="load",
    )
    page.get_by_role("button", name="Проверяемая лазейка").wait_for(state="visible")
    assert not errors
    return page


@pytest.mark.parametrize("capability", [None, False, "true"])
def test_catalog_verdict_is_read_only_without_explicit_permission(browser, capability):
    """Отсутствующее, ложное и некорректное разрешение запрещают UI-маркировку."""
    page = _open(browser, capability=capability)
    try:
        assert page.locator(".lp-verdict-chip").inner_text() == "лазейка"
        assert page.get_by_title("Изменить вердикт").count() == 0
        assert page.locator("button.lp-verdict-chip").count() == 0
        page.locator(".lp-verdict-chip").click()
        assert page.get_by_role("dialog").count() == 0
        assert page.evaluate("window.__verdictRequests") == []
    finally:
        page.close()


@pytest.mark.parametrize(
    "protected_context",
    [
        {"id": "queue", "title": "Очередь верификации"},
        {"id": "admin", "title": "Управление доступом"},
    ],
    ids=["ccks_expert", "module_admin"],
)
def test_authorized_catalog_verdict_can_be_changed(browser, protected_context):
    """Эксперт и администратор сохраняют вердикт через выданное сервером разрешение."""
    page = _open(browser, capability=True, protected_context=protected_context)
    try:
        page.get_by_title("Изменить вердикт").click()
        dialog = page.get_by_role("dialog", name="Вердикт записи")
        dialog.get_by_label("Комментарий аудитора").fill("Проверено экспертом")
        dialog.get_by_role("button", name="Обычный запрос").click()
        page.get_by_role("status").filter(has_text="Вердикт сохранён.").wait_for()
        dialog.wait_for(state="detached")
        assert page.evaluate("window.__verdictRequests") == [{
            "record_ids": [1], "is_loophole": False, "comment": "Проверено экспертом",
        }]
    finally:
        page.close()


@pytest.mark.parametrize("verdict_status", [401, 403])
def test_revoked_permission_closes_dialog_and_removes_actions(browser, verdict_status):
    """Отказ сервера после загрузки страницы закрывает модаль и убирает действия."""
    page = _open(browser, capability=True, verdict_status=verdict_status)
    try:
        page.get_by_title("Изменить вердикт").click()
        dialog = page.get_by_role("dialog", name="Вердикт записи")
        dialog.get_by_role("button", name="Обычный запрос").click()
        page.get_by_role("alert").filter(has_text="Нет права изменять вердикт.").wait_for()
        assert dialog.count() == 0
        assert page.locator("button.lp-verdict-chip").count() == 0
        assert page.locator(".lp-verdict-chip").inner_text() == "лазейка"
        assert len(page.evaluate("window.__verdictRequests")) == 1
    finally:
        page.close()


def test_queue_does_not_grant_verdict_permission_by_itself(browser):
    """Наличие контекста очереди без разрешения не открывает модаль маркировки."""
    page = _open(
        browser,
        capability=False,
        protected_context={"id": "queue", "title": "Очередь верификации"},
    )
    try:
        page.get_by_role("tab", name="Очередь верификации").click()
        page.get_by_role("heading", name="Проверяемая лазейка").wait_for()
        assert page.get_by_role("button", name="Проверить вердикт").count() == 0
        assert page.evaluate("window.__verdictRequests") == []
    finally:
        page.close()
