"""Карточки subagents в реальном React/Babel при управляемом SSE-потоке."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.loophole.test_final_layout_runtime import _open, _open_ai_chat


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_tool_activity_is_inside_message_and_long_report_fits(browser, width):
    page = _open(browser, width=width)
    page.set_default_timeout(5000)
    try:
        page.evaluate(r"""() => {
          const original = window.fetch;
          window.fetch = async (input, init = {}) => {
            if (String(input).endsWith('/chat') && init.method === 'POST') {
              return new Response(new ReadableStream({start(controller) {
                window.__fail = () => controller.error(new Error('Interrupted'));
                window.__emit = (event, data) => controller.enqueue(new TextEncoder().encode(
                  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
              }}), {headers: {'Content-Type': 'text/event-stream'}});
            }
            return original(input, init);
          };
        }""")
        _open_ai_chat(page, compact=width < 1100)
        page.get_by_label("Сообщение аналитику").fill("Проверка интерфейса")
        page.get_by_role("button", name="Отправить сообщение").click()
        page.wait_for_function("() => !!window.__emit")
        page.evaluate("""() => window.__emit('tool_call', {name: 'audit_web_fetch',
          args:'private-tool-arguments', result:'private-tool-result'})""")
        activity = page.locator(".lp-bubble-assistant .lp-tool-events")
        expect(activity).to_contain_text("Чтение источника")
        expect(activity).to_contain_text("Выполняется")
        expect(page.locator('body')).not_to_contain_text('private-tool-arguments')
        expect(page.locator('body')).not_to_contain_text('private-tool-result')
        expect(page.locator(".lp-typing-dots")).to_have_count(0)
        page.evaluate("() => window.__emit('tool_result', {name: 'audit_web_fetch', status:'failed'})")
        expect(activity).to_contain_text("Ошибка")
        page.evaluate("() => window.__emit('tool_call', {name: 'audit_web_search'})")
        page.evaluate("() => window.__fail()")
        expect(activity).to_contain_text("Прервано")
        page.get_by_label("Сообщение аналитику").fill("Продолжить")
        page.get_by_role("button", name="Отправить сообщение").click()
        expect(activity).not_to_contain_text("Выполняется")
        expect(page.locator(".lp-agent-activity")).to_have_count(1)
        page.evaluate("""() => {
          window.__emit('phase', {phase:'execute'});
          window.__emit('subagent', {id:'subagent-1', title:'Карты', status:'failed',
            total:1, completed:0, items:[], error_code:'timeout'});
          window.__emit('subagent', {id:'subagent-1-retry-1', retry_of:'subagent-1',
            title:'Карты', status:'completed', total:1, completed:1, items:[]});
        }""")
        expect(page.locator('.lp-subagent-card').last).to_contain_text("замена 1")
        if width == 1440:
            expect(page.get_by_text("Выполнено подзадач: 1 из 1")).to_be_visible()
        long_report = ("Подробный результат исследования. " * 100 + "\nhttps://example.test/"
                       + "длинныйпуть" * 100)
        page.evaluate("text => window.__emit('token', text)", long_report)
        expect(page.locator(".lp-bubble-content").last).to_contain_text("Подробный результат")
        assert page.evaluate("""() => [...document.querySelectorAll('.lp-bubble')].every(e =>
          e.scrollWidth <= e.clientWidth + 1 && e.scrollHeight <= e.clientHeight + 1)""")
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
        preview = Path("workspace/subagent-qa")
        preview.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(preview / f"recovery-chat-{width}.png"), full_page=True)
    finally:
        page.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_subagent_cards_stream_labels_and_interrupt_without_stale_state(browser, width):
    page = _open(browser, width=width)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_default_timeout(5000)
    try:
        page.evaluate("""() => {
          const original = window.fetch;
          window.fetch = async (input, init = {}) => {
            if (String(input).endsWith('/chat') && init.method === 'POST') {
              return new Response(new ReadableStream({start(controller) {
                window.__childStream = controller;
                window.__childEvent = data => controller.enqueue(new TextEncoder().encode(
                  `event: subagent\ndata: ${JSON.stringify(data)}\n\n`));
              }}), {headers: {'Content-Type': 'text/event-stream'}});
            }
            return original(input, init);
          };
        }""")
        _open_ai_chat(page, compact=width < 1100)
        composer = page.get_by_label("Сообщение аналитику")
        composer.fill("Исследуй кредитные карты")
        page.get_by_role("button", name="Отправить сообщение").click()
        page.wait_for_function("() => !!window.__childEvent")
        event = {"id": "subagent-1", "title": "Кредитные карты", "status": "queued",
                 "total": 0, "completed": 0, "items": []}
        page.evaluate("data => window.__childEvent(data)", event)
        cards = page.locator(".lp-subagent-card")
        expect(cards).to_have_count(1)
        expect(cards.first).to_contain_text("Ожидает свободного исследователя")
        expect(cards.first).to_have_attribute("aria-busy", "true")
        event.update(status="searching")
        page.evaluate("data => window.__childEvent(data)", event)
        expect(cards.first).to_contain_text("Поиск материалов")
        event.update(status="classifying", total=2)
        page.evaluate("data => window.__childEvent(data)", event)
        expect(cards.first).to_contain_text("Анализ описаний")
        expect(cards.first).to_have_attribute("aria-busy", "true")
        event.update(id="subagent-2")
        page.evaluate("data => window.__childEvent(data)", event)
        expect(cards).to_have_count(2)
        event.update(id="subagent-1", status="completed", completed=2, items=[
            {"title": "Обход ограничений", "url": "https://example.ru/post", "category": "loophole",
             "content_type": "post", "reason": "Пробел в правилах", "preliminary": True},
            {"title": '<img src=x onerror="window.__unsafeChild=1">', "url": "javascript:alert(1)",
             "category": "fraud", "content_type": "comment", "reason": "Признаки обмана",
             "preliminary": True},
        ])
        page.evaluate("data => window.__childEvent(data)", event)
        expect(cards.first).to_contain_text("Признаки мошенничества")
        expect(cards.first).to_contain_text("Лазейка")
        expect(cards.first).to_have_attribute("aria-busy", "false")
        assert cards.first.locator("img").count() == 0
        assert cards.first.locator('a[href^="javascript:"]').count() == 0
        assert page.evaluate("() => window.__unsafeChild === undefined")
        expect(page.get_by_text("Предварительный отбор по описаниям")).to_be_visible()
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
        # Чистое превью после проверки экранирования вредоносного заголовка.
        event["items"][1].update(title="Сообщение о подмене банковского сайта",
                                 url="https://example.ru/comment")
        page.evaluate("data => window.__childEvent(data)", event)
        page.evaluate(r"""() => window.__childStream.enqueue(new TextEncoder().encode(
          'event: phase\ndata: {"phase":"execute","stage":"research_tools",'
          + '"elapsed_seconds":12,"message":"Проверка источников"}\n\n'))""")
        expect(cards.first).to_contain_text("Сообщение о подмене банковского сайта")
        if width == 1440:
            expect(page.get_by_text("Выполнено подзадач: 1 из 2")).to_be_visible()
        preview = Path("workspace/subagent-qa")
        preview.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(preview / f"chat-{width}.png"), full_page=True)
        event.update(id="subagent-3", status="failed", completed=0, items=[],
                     error_code="timeout", message="secret-provider-response")
        page.evaluate("data => window.__childEvent(data)", event)
        expect(cards.nth(2)).to_contain_text("Не хватило времени на поиск и анализ")
        expect(cards.nth(2)).not_to_contain_text("secret-provider-response")
        event.update(error_code="unknown")
        page.evaluate("data => window.__childEvent(data)", event)
        expect(cards.nth(2)).to_contain_text("Отбор неполный")
        page.evaluate("() => window.__childStream.error(new Error('Поток прерван'))")
        expect(cards.nth(1)).to_contain_text("Прервано")
        expect(cards.first).to_contain_text("Завершено")
        expect(composer).to_be_enabled()
        composer.fill("Ещё исследование")
        page.get_by_role("button", name="Отправить сообщение").click()
        expect(cards).to_have_count(0)
        assert not errors
    finally:
        page.close()
