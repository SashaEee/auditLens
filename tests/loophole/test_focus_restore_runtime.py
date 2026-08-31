"""Runtime-регрессия доступного перехода к read-only каталогу парсеров."""

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
        if (url.endsWith("/contexts")) return json({contexts: [
          {id: "catalog", title: "Общая база"},
          {id: "sources", title: "Добавить источник"},
        ]});
        if (url.endsWith("/workspace")) return json({workspace_id: 1});
        if (url.endsWith("/banks")) return json({banks: []});
        if (url.includes("/catalog")) return json({records: []});
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


def test_sources_tab_keeps_focus_after_opening_read_only_catalog():
    """После открытия вкладки фокус остаётся на доступной стабильной вкладке."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(10_000)
            page.set_default_navigation_timeout(10_000)
            page.set_content(_runtime_html(), wait_until="load")
            sources_tab = page.get_by_role("tab", name="Добавить источник")
            sources_tab.wait_for(state="visible")
            sources_tab.click()
            page.wait_for_function(
                """() => {
                  const active = document.activeElement;
                  return active instanceof HTMLButtonElement
                    && active.getAttribute("role") === "tab"
                    && active.textContent.includes("Добавить источник")
                    && !active.disabled;
                }"""
            )
        finally:
            browser.close()
