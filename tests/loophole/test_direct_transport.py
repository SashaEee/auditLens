from __future__ import annotations

import asyncio

from bank_audit.loophole.direct_transport import async_client, child_env, chromium_args, sync_client


def test_direct_http_clients_ignore_proxy_env(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    sync = sync_client()
    async_http = async_client()
    try:
        assert sync.trust_env is False
        assert async_http.trust_env is False
    finally:
        sync.close()
        import asyncio

        asyncio.run(async_http.aclose())


def test_child_env_removes_proxy_without_mutating_process(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    env = child_env()
    assert "HTTP_PROXY" not in env
    assert "HTTP_PROXY" in __import__("os").environ


def test_chromium_args_disable_proxy_server():
    assert "--no-proxy-server" in chromium_args()


def test_browser_collector_applies_direct_chromium_policy_locally():
    from bank_audit.collectors.browser import BrowserCollector

    regular = BrowserCollector()
    direct = BrowserCollector(direct=True)

    assert "--no-proxy-server" not in regular._launch_args
    assert "--no-proxy-server" in direct._launch_args
    assert "--proxy-bypass-list=<-loopback>" in direct._launch_args


def test_pdf_export_passes_direct_chromium_options(monkeypatch):
    import playwright.async_api

    from bank_audit.loophole import pdf_export

    captured = {}

    class Page:
        async def set_content(self, *_args, **_kwargs):
            return None

        async def pdf(self, **_kwargs):
            return b"%PDF-test"

    class Browser:
        async def new_page(self):
            return Page()

        async def close(self):
            return None

    class Chromium:
        async def launch(self, **kwargs):
            captured.update(kwargs)
            return Browser()

    class Playwright:
        chromium = Chromium()

    class PlaywrightContext:
        async def __aenter__(self):
            return Playwright()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: PlaywrightContext())

    assert asyncio.run(pdf_export.export_pdf([])) == b"%PDF-test"
    assert "--no-proxy-server" in captured["args"]
