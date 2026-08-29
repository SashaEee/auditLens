"""Runtime-регрессия focus restore после подтверждённого удаления парсера."""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "bank_audit" / "loophole" / "static"
VENDOR = ROOT / "src" / "bank_audit" / "web" / "static" / "vendor"


def _runtime_html() -> str:
    """Самодостаточная страница с реальным JSX и локальными React/Babel."""
    fetch_stub = """
      window.fetch = async (input, init = {}) => {
        const url = String(input);
        const method = (init.method || "GET").toUpperCase();
        const json = (value, status = 200) => new Response(JSON.stringify(value), {
          status,
          headers: {"Content-Type": "application/json"},
        });
        if (url.endsWith("/contexts")) return json({contexts: []});
        if (url.endsWith("/workspace")) return json({workspace_id: 1});
        if (url.endsWith("/banks")) return json({banks: []});
        if (url.endsWith("/records")) return json({records: []});
        if (url.endsWith("/parsers") && method === "GET") {
          return json({parsers: [{
            parser_id: 1, name: "Тестовый парсер", is_running: false,
            targets: [], records_count: 0, auto_enabled: false,
          }]});
        }
        if (url.endsWith("/parsers/1") && method === "DELETE") return json({});
        return json({});
      };
    """
    react = (VENDOR / "react.min.js").read_text(encoding="utf-8")
    react_dom = (VENDOR / "react-dom.min.js").read_text(encoding="utf-8")
    babel = (VENDOR / "babel.min.js").read_text(encoding="utf-8")
    jsx = (STATIC / "loophole.jsx").read_text(encoding="utf-8")
    return (
        "<!doctype html><html lang=\"ru\"><body><div id=\"loophole-root\"></div>"
        f"<script>{react}</script><script>{react_dom}</script>"
        f"<script>{babel}</script><script>{fetch_stub}</script>"
        f"<script type=\"text/babel\">{jsx}</script></body></html>"
    )


def test_confirmed_delete_restores_focus_to_enabled_parser_dialog_fallback():
    """После подтверждения delete исходная кнопка становится disabled, поэтому
    фокус должен оказаться на доступном контроле родительского диалога."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(10_000)
            page.set_default_navigation_timeout(10_000)
            page.set_content(_runtime_html(), wait_until="load")
            parser_button = page.locator('button[title="Управление парсерами"]')
            parser_button.wait_for(state="visible")
            page.wait_for_function(
                "() => !document.querySelector('button[title=\"Управление парсерами\"]').disabled"
            )
            parser_button.click()
            parser_row = page.locator(".lp-parser-row")
            parser_row.wait_for(state="visible")
            parser_row.get_by_role("button", name="Удалить").click()
            confirm = page.locator(".lp-confirm-dialog")
            confirm.wait_for(state="visible")
            confirm.get_by_role("button", name="Удалить").click()
            page.wait_for_function(
                """() => {
                  const active = document.activeElement;
                  return active instanceof HTMLButtonElement
                    && active.getAttribute("aria-label") === "Закрыть"
                    && !active.disabled
                    && Boolean(active.closest(".lp-parsers-dialog"));
                }"""
            )
        finally:
            browser.close()
