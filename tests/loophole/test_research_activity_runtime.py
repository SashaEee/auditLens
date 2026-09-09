"""Прогресс ожидания модели виден до ответа и не остаётся после завершения."""
from __future__ import annotations

import pytest
from playwright.sync_api import Browser, expect, sync_playwright

from tests.loophole.test_final_layout_runtime import _open


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.mark.parametrize("ending", ["answer", "error", "disconnect"])
def test_live_activity_is_safe_updates_and_clears(browser: Browser, ending: str):
    """Поток показывает stage/elapsed до token; финал и обрыв убирают старый статус."""
    page = _open(browser)
    page.set_default_timeout(5_000)
    try:
        page.evaluate("""() => {
          const originalFetch = window.fetch;
          const encoder = new TextEncoder();
          window.fetch = async (input, init = {}) => {
            if (String(input).endsWith('/chat') && init.method === 'POST') {
              return new Response(new ReadableStream({
                start(controller) {
                  window.__activityStreamCount = (window.__activityStreamCount || 0) + 1;
                  window.__activityStream = controller;
                  window.__activityEvent = (event, data) => controller.enqueue(encoder.encode(
                    `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
                }
              }), {headers: {'Content-Type': 'text/event-stream'}});
            }
            return originalFetch(input, init);
          };
        }""")
        page.get_by_role("tab", name="AI-исследования").click()
        composer = page.get_by_label("Сообщение аналитику")
        composer.fill("Найди 1 лазейку по кредитным картам за 2026 год")
        page.get_by_role("button", name="Отправить сообщение").click()
        page.wait_for_function("() => !!window.__activityEvent")
        message = '<img src=x onerror="window.__unsafeProgress=1">Ожидание ответа модели'
        page.evaluate("payload => window.__activityEvent('phase', payload)", {
            "phase": "execute", "stage": "waiting_model", "elapsed_seconds": 5,
            "message": message,
        })
        activity = page.get_by_role("status", name="Текущий этап исследования")
        expect(activity).to_contain_text(message)
        expect(activity).to_contain_text("5 с")
        assert activity.locator("img").count() == 0
        assert page.evaluate("() => window.__unsafeProgress === undefined")
        page.evaluate("payload => window.__activityEvent('phase', payload)", {
            "phase": "execute", "stage": "research_tools", "elapsed_seconds": 12,
            "message": "Проверка источников",
        })
        expect(activity).to_have_text("Проверка источников · 12 с")
        if ending == "disconnect":
            page.evaluate("() => window.__activityStream.error(new Error('Поток прерван'))")
        else:
            page.evaluate("phase => window.__activityEvent('phase', {phase})", ending)
            expect(activity).to_have_count(0)
            page.evaluate("() => window.__activityStream.close()")
        expect(activity).to_have_count(0)
        expect(composer).to_be_enabled()
        composer.fill("Повторный запрос")
        page.get_by_role("button", name="Отправить сообщение").click()
        page.wait_for_function("() => window.__activityStreamCount === 2")
        expect(activity).to_have_count(0)
        page.evaluate("() => window.__activityStream.close()")
    finally:
        page.close()
