"""Browser-runtime контракт финального макета «Лазеек»."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Browser, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "bank_audit" / "loophole" / "static"
VENDOR = ROOT / "src" / "bank_audit" / "web" / "static" / "vendor"

ALL_CONTEXTS = [
    {"id": "catalog", "title": "Общая база"},
    {"id": "sources", "title": "Добавить источник"},
    {"id": "ai_research", "title": "Новое AI-исследование"},
    {"id": "queue", "title": "Очередь верификации"},
    {"id": "admin", "title": "Управление доступом"},
]

RECORDS = [
    {
        "record_id": 1,
        "title": "Льгота перевода при частичном погашении",
        "url": "https://bank.example/one",
        "bank_slug": "sber",
        "verdict_confidence": 0.82,
        "trust_score": 0.99,
        "is_loophole": True,
        "status": "published",
        "published_at": "2026-08-26T14:32:00+03:00",
        "collected_at": "2026-08-30T11:04:00+03:00",
    },
    {
        "record_id": 3,
        "title": "Комиссия за перевод сверх лимита",
        "url": "https://bank.example/three",
        "bank_slug": "vtb",
        "verdict_confidence": 0.76,
        "trust_score": 0.51,
        "is_loophole": True,
        "status": "published",
        "published_at": None,
        "collected_at": "2026-08-30T10:03:00+03:00",
    },
]


def _runtime_html(
    contexts: list[dict] | None = None,
    *,
    deny_protected: bool = False,
    event_source_error: bool = False,
    parser_targets: list[str] | None = None,
    clarify_answer_delay_ms: int = 0,
    clarify_answer_error: bool = False,
    clarify_answer_status: int | None = None,
    clarify_questions: list[dict] | None = None,
    clarification_tokens: list[str] | None = None,
    tokenized_chat_failure: str | None = None,
    initial_chat_failure: bool = False,
    report_snapshot_id: int | None = None,
) -> str:
    react = (VENDOR / "react.min.js").read_text(encoding="utf-8")
    react_dom = (VENDOR / "react-dom.min.js").read_text(encoding="utf-8")
    babel = (VENDOR / "babel.min.js").read_text(encoding="utf-8")
    jsx = (STATIC / "loophole.jsx").read_text(encoding="utf-8")
    css = (STATIC / "loophole.css").read_text(encoding="utf-8")
    context_json = json.dumps(contexts or ALL_CONTEXTS, ensure_ascii=False)
    records_json = json.dumps(RECORDS, ensure_ascii=False)
    parser_json = json.dumps(
        {
            "parser_id": 7,
            "name": "Тарифы ВТБ",
            "status": "ready",
            "is_running": False,
            "targets": parser_targets or ["https://www.vtb.ru/personal/tarify/"],
            "records_count": 12,
            "auto_enabled": False,
            "last_run": {"status": "success", "finished_at": "2026-08-30T10:00:00"},
        },
        ensure_ascii=False,
    )
    deny_protected_json = json.dumps(deny_protected)
    event_source_error_json = json.dumps(event_source_error)
    clarify_answer_error_json = json.dumps(clarify_answer_error)
    clarify_answer_status_json = json.dumps(clarify_answer_status)
    clarification_tokens_json = json.dumps(
        clarification_tokens or ["clarification-token-1"],
        ensure_ascii=False,
    )
    tokenized_chat_failure_json = json.dumps(tokenized_chat_failure)
    initial_chat_failure_json = json.dumps(initial_chat_failure)
    report_snapshot_id_json = json.dumps(report_snapshot_id)
    clarify_questions_json = json.dumps(
        clarify_questions
        or [{
            "id": "bank",
            "question": "Какой банк исследовать?",
            "type": "text",
            "allow_other": True,
            "options": [],
        }],
        ensure_ascii=False,
    )
    fetch_stub = f"""
      window.__exportBodies = [];
      window.__downloads = [];
      window.__catalogUrls = [];
      window.__eventSources = [];
      window.__adminTelegramTargetFetches = 0;
      window.__chatBodies = [];
      window.__clarifyAnswerBodies = [];
      window.__clarifyAnswerResolved = false;
      window.__clarifyAnswerDelayMs = {clarify_answer_delay_ms};
      window.__clarifyAnswerError = {clarify_answer_error_json};
      window.__clarifyAnswerStatus = {clarify_answer_status_json};
      window.__clarificationTokens = {clarification_tokens_json};
      window.__tokenizedChatFailure = {tokenized_chat_failure_json};
      window.__initialChatFailure = {initial_chat_failure_json};
      window.__reportSnapshotId = {report_snapshot_id_json};
      window.__denyProtected = {deny_protected_json};
      window.__eventSourceError = {event_source_error_json};
      window.__parsers = [{parser_json}];
      URL.createObjectURL = () => "blob:loophole-test";
      URL.revokeObjectURL = () => {{}};
      HTMLAnchorElement.prototype.click = function () {{
        window.__downloads.push({{href: this.href, download: this.download}});
      }};
      class FakeEventSource {{
        constructor(url) {{
          this.url = url;
          this.listeners = {{}};
          this.closed = false;
          window.__eventSources.push(this);
          setTimeout(() => this.emit("log", "Проверка доступности — 200 OK"), 20);
          if (window.__eventSourceError) {{
            setTimeout(() => this.onerror && this.onerror(new Event("error")), 40);
          }} else {{
            setTimeout(() => this.emit("done", JSON.stringify({{status: "success", items_new: 1}})), 40);
          }}
        }}
        addEventListener(name, callback) {{ (this.listeners[name] ||= []).push(callback); }}
        emit(name, data) {{ (this.listeners[name] || []).forEach(cb => cb({{data}})); }}
        close() {{ this.closed = true; }}
      }}
      window.EventSource = FakeEventSource;
      window.fetch = async (input, init = {{}}) => {{
        const url = String(input);
        const method = (init.method || "GET").toUpperCase();
        const jsonResponse = (value, status = 200) => new Response(JSON.stringify(value), {{
          status,
          headers: {{"Content-Type": "application/json"}},
        }});
        const newline = String.fromCharCode(10);
        const sseResponse = events => new Response(
          events.map(entry => (
            "event: " + entry[0] + newline + "data: " + JSON.stringify(entry[1])
          )).join(newline + newline) + newline + newline,
          {{status: 200, headers: {{"Content-Type": "text/event-stream"}}}}
        );
        if (url.endsWith("/contexts")) return jsonResponse({{contexts: {context_json}}});
        if (url.endsWith("/workspace")) return jsonResponse({{workspace_id: 1}});
        if (url.endsWith("/banks")) return jsonResponse({{banks: ["sber", "vtb"]}});
        if (url.includes("/catalog")) {{
          window.__catalogUrls.push(url);
          return jsonResponse({{records: {records_json}}});
        }}
        if (url.endsWith("/queue")) {{
          return window.__denyProtected
            ? jsonResponse({{detail: "forbidden"}}, 403)
            : jsonResponse({{records: {records_json}}});
        }}
        if (url.endsWith("/admin/roles")) return jsonResponse({{
          roles: [{{username: "expert.ivanova", status: "active", created_at: "2026-08-30T09:15:00+08:00"}}],
          active_experts: 1, max_experts: 5,
        }}, window.__denyProtected ? 403 : 200);
        if (url.endsWith("/admin/telegram-targets")) {{
          window.__adminTelegramTargetFetches += 1;
          return jsonResponse({{detail: "unused admin data"}}, 500);
        }}
        if (url.endsWith("/admin/audit")) return jsonResponse({{events: [{{
          action: "role_assign", decision: "allow", count: 3,
          last_at: "2026-08-30T10:14:00+08:00",
        }}]}}, window.__denyProtected ? 403 : 200);
        if (url.endsWith("/parsers") && method === "GET") return jsonResponse({{parsers: window.__parsers}});
        if (url.endsWith("/parsers") && method === "POST") {{
          const parser = {{
            parser_id: 8, name: "Новый веб-источник", status: "created",
            is_running: true, targets: ["https://example.ru/tariffs"], records_count: 0,
            auto_enabled: false,
          }};
          window.__parsers = [...window.__parsers, parser];
          return jsonResponse({{...parser, validation_run_id: 88}});
        }}
        if (url.endsWith("/clarify/answer") && method === "POST") {{
          window.__clarifyAnswerBodies.push(JSON.parse(init.body));
          await new Promise(resolve => setTimeout(resolve, window.__clarifyAnswerDelayMs));
          window.__clarifyAnswerResolved = true;
          if (window.__clarifyAnswerStatus) {{
            const status = window.__clarifyAnswerStatus;
            const detail = status === 400
              ? "Уточнение устарело или не принадлежит этому запросу"
              : {{message: "Не удалось подготовить исследование. Повторите отправку ответа."}};
            return jsonResponse({{detail}}, status);
          }}
          if (window.__clarifyAnswerError) {{
            return jsonResponse({{
              detail: {{message: "Не удалось подготовить исследование. Повторите отправку ответа."}},
            }}, 503);
          }}
          return jsonResponse({{
            enriched_question: "найди лазейки (уточнения — Какой банк: Сбербанк)",
            execution_token: "execution-token-1",
            answer_message: "Сбербанк",
          }});
        }}
        if (url.endsWith("/chat") && method === "POST") {{
          const body = JSON.parse(init.body);
          window.__chatBodies.push(body);
          if (window.__initialChatFailure && !body.clarify_token) {{
            return sseResponse([
              ["phase", {{phase: "execute"}}],
              ["phase", {{
                phase: "error",
                error: "agent_unavailable",
                message: "Error calling LLM: Connection error.",
              }}],
            ]);
          }}
          if (!body.clarify_token) {{
            const challengeIndex = window.__chatBodies.filter(
              entry => !entry.clarify_token
            ).length - 1;
            const challengeToken = window.__clarificationTokens[
              Math.min(challengeIndex, window.__clarificationTokens.length - 1)
            ];
            return sseResponse([
              ["phase", {{phase: "clarify"}}],
              ["phase", {{phase: "await_clarify"}}],
              ["question", {{
                questions: {clarify_questions_json},
                clarification_token: challengeToken,
              }}],
            ]);
          }}
          if (window.__tokenizedChatFailure === "http") {{
            return jsonResponse({{detail: "Агент временно недоступен"}}, 502);
          }}
          if (window.__tokenizedChatFailure === "sse") {{
            return sseResponse([
              ["phase", {{
                phase: "error",
                error: "agent_unavailable",
                message: "Агент временно недоступен",
              }}],
            ]);
          }}
          return sseResponse([
            ["phase", {{phase: "execute"}}],
            ["token", "Исследование запущено\\n\\n# Итог"],
            ["token", "\\n\\n- Проверенный источник"],
            ["report", window.__reportSnapshotId ? {{report_id: window.__reportSnapshotId}} : {{}}],
            ["done", {{}}],
          ]);
        }}
        if (url.endsWith("/export") && method === "POST") {{
          window.__exportBodies.push(JSON.parse(init.body));
          return new Response("record_id,published_at,collected_at\\n1,2026-08-26,2026-08-30", {{
            status: 200,
            headers: {{"Content-Type": "text/csv"}},
          }});
        }}
        return jsonResponse({{}});
      }};
    """
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<style>{css}</style></head><body><div id=\"loophole-root\"></div>"
        f"<script>{react}</script><script>{react_dom}</script>"
        f"<script>{babel}</script><script>{fetch_stub}</script>"
        f'<script type="text/babel">{jsx}</script></body></html>'
    )


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def _open(
    browser: Browser,
    *,
    width: int = 1440,
    contexts: list[dict] | None = None,
    deny_protected: bool = False,
    event_source_error: bool = False,
    parser_targets: list[str] | None = None,
    clarify_answer_delay_ms: int = 0,
    clarify_answer_error: bool = False,
    clarify_answer_status: int | None = None,
    clarify_questions: list[dict] | None = None,
    clarification_tokens: list[str] | None = None,
    tokenized_chat_failure: str | None = None,
    initial_chat_failure: bool = False,
    report_snapshot_id: int | None = None,
):
    page = browser.new_page(viewport={"width": width, "height": 900})
    page.set_default_timeout(15_000)
    page.set_content(
        _runtime_html(
            contexts,
            deny_protected=deny_protected,
            event_source_error=event_source_error,
            parser_targets=parser_targets,
            clarify_answer_delay_ms=clarify_answer_delay_ms,
            clarify_answer_error=clarify_answer_error,
            clarify_answer_status=clarify_answer_status,
            clarify_questions=clarify_questions,
            clarification_tokens=clarification_tokens,
            tokenized_chat_failure=tokenized_chat_failure,
            initial_chat_failure=initial_chat_failure,
            report_snapshot_id=report_snapshot_id,
        ),
        wait_until="load",
    )
    page.get_by_role("tab", name="Общая база").wait_for(state="visible")
    page.locator("tbody tr").first.wait_for(state="visible")
    return page


def _open_ai_chat(page, *, compact: bool) -> None:
    page.get_by_role("tab", name="Новое AI-исследование").click()
    if compact:
        page.get_by_role("button", name="Открыть чат").click()
    page.get_by_label("Сообщение аналитику").wait_for(state="visible")


@pytest.mark.parametrize("width", [1440, 992], ids=["desktop", "offcanvas"])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_text_clarification_uses_composer_and_keeps_continuous_busy_state(
    browser: Browser,
    width: int,
    dark: bool,
):
    """Text-answer не создаёт карточку или enriched-дубль во время delayed submit."""
    page = _open(browser, width=width, clarify_answer_delay_ms=700)
    page.set_default_timeout(5_000)
    try:
        page.evaluate("dark => document.documentElement.classList.toggle('dark', dark)", dark)
        _open_ai_chat(page, compact=width < 1100)
        composer = page.get_by_label("Сообщение аналитику")
        send = page.get_by_role("button", name="Отправить сообщение")

        composer.fill("найди лазейки")
        send.click()
        page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Какой банк исследовать?",
        ).wait_for(state="visible")

        assert page.locator(".lp-questions-card").count() == 0
        assert composer.is_enabled()
        assert composer.get_attribute("placeholder") == "Ответ на уточняющий вопрос…"

        composer.fill("Сбербанк")
        send.click()
        page.locator(".lp-bubble-user", has_text="Сбербанк").wait_for(state="visible")

        assert page.evaluate("window.__clarifyAnswerResolved") is False
        assert "Обдумывает ответ" in page.locator(".lp-agent-status").inner_text()
        user_messages_during_submit = page.locator(
            ".lp-bubble-user .lp-bubble-content"
        ).all_inner_texts()
        assert user_messages_during_submit == ["найди лазейки", "Сбербанк"]
        assert page.locator(".lp-research-kv dd").first.inner_text() == "найди лазейки"
        assert page.locator("#lp-research-progress-title").inner_text() == "Ожидает уточнения"
        assert "Какой банк исследовать?" not in page.locator(
            ".lp-research-evidence"
        ).inner_text()

        page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Исследование запущено",
        ).wait_for(state="visible")
        assert page.evaluate("window.__chatBodies.length") == 2
        assert page.evaluate("window.__clarifyAnswerBodies.length") == 1
        assert page.locator(".lp-bubble-user .lp-bubble-content").all_inner_texts() == [
            "найди лазейки",
            "Сбербанк",
        ]
        assert page.evaluate("window.__chatBodies[1].clarify_token") == "execution-token-1"
    finally:
        page.close()


def test_failed_text_answer_restores_question_and_composer_draft(browser: Browser):
    """Сбой answer endpoint убирает optimistic bubble и возвращает введённый ответ."""
    page = _open(browser, clarify_answer_delay_ms=50, clarify_answer_error=True)
    page.set_default_timeout(5_000)
    try:
        _open_ai_chat(page, compact=False)
        composer = page.get_by_label("Сообщение аналитику")
        send = page.get_by_role("button", name="Отправить сообщение")

        composer.fill("найди лазейки")
        send.click()
        page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Какой банк исследовать?",
        ).wait_for(state="visible")

        composer.fill("Сбербанк")
        send.click()
        page.get_by_role("alert").wait_for(state="visible")

        assert "Не удалось подготовить исследование" in page.get_by_role("alert").inner_text()
        assert composer.is_enabled()
        assert composer.input_value() == "Сбербанк"
        assert composer.get_attribute("placeholder") == "Ответ на уточняющий вопрос…"
        assert page.locator(".lp-bubble-user .lp-bubble-content").all_inner_texts() == [
            "найди лазейки",
        ]
        assert page.evaluate("window.__chatBodies.length") == 1
    finally:
        page.close()


def test_initial_agent_connection_error_is_safe_and_restores_query(browser: Browser):
    """Первичный transport failure не раскрывает provider text и оставляет ручной retry."""
    page = _open(browser, initial_chat_failure=True)
    page.set_default_timeout(5_000)
    query = "Найди лазейки по кредитным картам за август 2026 года"
    try:
        _open_ai_chat(page, compact=False)
        composer = page.get_by_label("Сообщение аналитику")
        composer.fill(query)
        page.get_by_role("button", name="Отправить сообщение").click()

        safe_error = page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Аналитик временно недоступен",
        )
        safe_error.wait_for(state="visible")

        assert composer.input_value() == query
        assert "повторите" in safe_error.inner_text().lower()
        assert "Error calling LLM" not in page.locator("body").inner_text()
        assert "Connection error" not in page.locator("body").inner_text()
        assert page.locator("#lp-research-progress-title").inner_text() == "Ошибка"
        assert page.locator(".lp-bubble-content", has_text="(пустой ответ)").count() == 0
    finally:
        page.close()


def test_expired_clarification_discards_challenge_and_builds_manual_draft(
    browser: Browser,
):
    """HTTP 400 очищает старый token и сохраняет запрос с ответом для нового хода."""
    page = _open(browser, clarify_answer_status=400)
    page.set_default_timeout(5_000)
    try:
        _open_ai_chat(page, compact=False)
        composer = page.get_by_label("Сообщение аналитику")
        send = page.get_by_role("button", name="Отправить сообщение")

        composer.fill("найди лазейки")
        send.click()
        page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Какой банк исследовать?",
        ).wait_for(state="visible")

        composer.fill("Сбербанк")
        send.click()
        alert = page.get_by_role("alert")
        alert.wait_for(state="visible")

        assert "Уточнение истекло или уже использовано" in alert.inner_text()
        assert composer.input_value() == (
            "найди лазейки\n\nОтвет на уточнение: Сбербанк"
        )
        assert composer.get_attribute("placeholder") == "Сообщение аналитику…"
        assert composer.is_enabled()
        assert page.locator(".lp-bubble-user .lp-bubble-content").all_inner_texts() == [
            "найди лазейки",
        ]
        assert page.locator("#lp-research-progress-title").inner_text() == "Ошибка"
        assert page.evaluate("window.__clarifyAnswerBodies.length") == 1
        assert page.evaluate("window.__chatBodies.length") == 1
    finally:
        page.close()


def test_new_challenge_with_same_question_id_is_shown_again(browser: Browser):
    """Новый server-side token делает одинаковый id новым challenge, а не дублем."""
    page = _open(
        browser,
        clarification_tokens=["clarification-token-1", "clarification-token-2"],
    )
    page.set_default_timeout(5_000)
    try:
        _open_ai_chat(page, compact=False)
        composer = page.get_by_label("Сообщение аналитику")
        send = page.get_by_role("button", name="Отправить сообщение")
        question_bubbles = page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Какой банк исследовать?",
        )

        composer.fill("найди лазейки")
        send.click()
        question_bubbles.first.wait_for(state="visible")
        composer.fill("Сбербанк")
        send.click()
        page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Исследование запущено",
        ).wait_for(state="visible")

        composer.fill("проверь другой продукт")
        send.click()
        page.wait_for_function(
            "document.querySelector('#lp-chat-input').placeholder === "
            "'Ответ на уточняющий вопрос…'"
        )

        assert question_bubbles.count() == 2
        assert page.evaluate("window.__chatBodies.length") == 3
        assert page.evaluate(
            "window.__chatBodies.filter(body => !body.clarify_token).length"
        ) == 2
    finally:
        page.close()


@pytest.mark.parametrize("failure_mode", ["http", "sse"])
def test_tokenized_chat_failure_keeps_confirmed_answer_and_manual_retry(
    browser: Browser,
    failure_mode: str,
):
    """Сбой второго /chat не маскируется как done после принятого уточнения."""
    page = _open(browser, tokenized_chat_failure=failure_mode)
    page.set_default_timeout(5_000)
    try:
        _open_ai_chat(page, compact=False)
        composer = page.get_by_label("Сообщение аналитику")
        send = page.get_by_role("button", name="Отправить сообщение")

        composer.fill("найди лазейки")
        send.click()
        page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Какой банк исследовать?",
        ).wait_for(state="visible")

        composer.fill("Сбербанк")
        send.click()
        alert = page.get_by_role("alert")
        alert.wait_for(state="visible")

        assert "Ответ на уточнение сохранён" in alert.inner_text()
        assert "исследование не запустилось" in alert.inner_text()
        assert composer.input_value() == (
            "найди лазейки (уточнения — Какой банк: Сбербанк)"
        )
        assert composer.get_attribute("placeholder") == "Сообщение аналитику…"
        assert composer.is_enabled()
        assert page.locator(".lp-bubble-user .lp-bubble-content").all_inner_texts() == [
            "найди лазейки",
            "Сбербанк",
        ]
        assert page.locator(".lp-bubble-content", has_text="(пустой ответ)").count() == 0
        assert page.locator("#lp-research-progress-title").inner_text() == "Ошибка"
        assert page.evaluate("window.__chatBodies.length") == 2
    finally:
        page.close()


def test_all_selection_questions_are_visible_and_required(browser: Browser):
    """Все single/multi controls видны, а незаполненный набор не запускается."""
    questions = [
        {
            "id": "bank",
            "question": "Выберите банк",
            "type": "single",
            "allow_other": False,
            "options": [
                {"value": "sber", "label": "Сбербанк"},
                {"value": "vtb", "label": "ВТБ"},
            ],
        },
        {
            "id": "product",
            "question": "Выберите продукты",
            "type": "multi",
            "allow_other": False,
            "options": [
                {"value": "debit", "label": "Дебетовые карты"},
                {"value": "credit", "label": "Кредиты"},
            ],
        },
    ]
    page = _open(
        browser,
        clarify_answer_delay_ms=300,
        clarify_questions=questions,
    )
    page.set_default_timeout(5_000)
    try:
        _open_ai_chat(page, compact=False)
        composer = page.get_by_label("Сообщение аналитику")
        composer.fill("найди лазейки")
        page.get_by_role("button", name="Отправить сообщение").click()

        card = page.locator(".lp-questions-card")
        card.get_by_text("Выберите банк", exact=True).wait_for(state="visible")
        assert card.get_by_text("Выберите продукты", exact=True).is_visible()
        answer_button = card.get_by_role("button", name="Ответить")
        assert answer_button.is_disabled()
        assert "Ответьте на все вопросы" in card.inner_text()
        assert composer.is_disabled()

        card.get_by_label("Сбербанк").check()
        assert answer_button.is_disabled()
        card.get_by_label("Дебетовые карты").check()
        assert answer_button.is_enabled()
        answer_button.click()

        page.locator(".lp-bubble-user", has_text="sber; debit").wait_for(state="visible")
        assert page.locator(".lp-questions-card").count() == 0
        assert "Обдумывает ответ" in page.locator(".lp-agent-status").inner_text()
        page.locator(
            ".lp-bubble-assistant .lp-bubble-content",
            has_text="Исследование запущено",
        ).wait_for(state="visible")
        assert page.evaluate("window.__clarifyAnswerBodies[0].answers.length") == 2
        assert page.evaluate(
            "window.__clarifyAnswerBodies[0].answers.map(a => a.selected)"
        ) == [["sber"], ["debit"]]
    finally:
        page.close()


def test_selection_answers_remain_checked_after_temporary_503(browser: Browser):
    """Временный 503 восстанавливает radio/checkbox без повторного выбора."""
    questions = [
        {
            "id": "bank",
            "question": "Выберите банк",
            "type": "single",
            "allow_other": False,
            "options": [
                {"value": "sber", "label": "Сбербанк"},
                {"value": "vtb", "label": "ВТБ"},
            ],
        },
        {
            "id": "product",
            "question": "Выберите продукты",
            "type": "multi",
            "allow_other": False,
            "options": [
                {"value": "debit", "label": "Дебетовые карты"},
                {"value": "credit", "label": "Кредиты"},
            ],
        },
    ]
    page = _open(
        browser,
        clarify_answer_error=True,
        clarify_questions=questions,
    )
    page.set_default_timeout(5_000)
    try:
        _open_ai_chat(page, compact=False)
        composer = page.get_by_label("Сообщение аналитику")
        composer.fill("найди лазейки")
        page.get_by_role("button", name="Отправить сообщение").click()

        card = page.locator(".lp-questions-card")
        card.get_by_text("Выберите банк", exact=True).wait_for(state="visible")
        card.get_by_label("Сбербанк").check()
        card.get_by_label("Дебетовые карты").check()
        card.get_by_role("button", name="Ответить").click()
        page.get_by_role("alert").wait_for(state="visible")

        assert card.is_visible()
        assert card.get_by_label("Сбербанк").is_checked()
        assert card.get_by_label("Дебетовые карты").is_checked()
        assert card.get_by_label("ВТБ").is_checked() is False
        assert card.get_by_label("Кредиты").is_checked() is False
        assert card.get_by_role("button", name="Ответить").is_enabled()
        assert composer.is_disabled()
        assert page.locator(".lp-bubble-user .lp-bubble-content").all_inner_texts() == [
            "найди лазейки",
        ]
    finally:
        page.close()


@pytest.mark.parametrize("width", [1440, 992], ids=["desktop", "offcanvas"])
def test_chat_panel_follows_theme_tokens_without_gradient_or_slash_copy(
    browser: Browser,
    width: int,
):
    """Панель наследует light/dark AuditLens tokens вместо постоянного dark canvas."""
    page = _open(browser, width=width)
    page.set_default_timeout(5_000)
    try:
        _open_ai_chat(page, compact=width < 1100)
        light = page.evaluate(
            """() => {
              const probe = document.createElement('div');
              probe.style.background = 'var(--surface)';
              document.body.appendChild(probe);
              const result = {
                sidebar: getComputedStyle(document.querySelector('.lp-sidebar')).backgroundColor,
                surface: getComputedStyle(probe).backgroundColor,
                avatarImage: getComputedStyle(document.querySelector('.lp-agent-avatar')).backgroundImage,
              };
              probe.remove();
              return result;
            }"""
        )
        assert light["sidebar"] == light["surface"]
        assert light["avatarImage"] == "none"
        empty_copy = page.locator(".lp-chat-empty").inner_text()
        assert "Доступны команды" not in empty_copy
        assert "/" not in empty_copy

        page.evaluate("document.documentElement.classList.add('dark')")
        dark = page.evaluate(
            """() => {
              const probe = document.createElement('div');
              probe.style.background = 'var(--surface)';
              document.body.appendChild(probe);
              const result = {
                sidebar: getComputedStyle(document.querySelector('.lp-sidebar')).backgroundColor,
                surface: getComputedStyle(probe).backgroundColor,
              };
              probe.remove();
              return result;
            }"""
        )
        assert dark["sidebar"] == dark["surface"]
        assert dark["sidebar"] != light["sidebar"]
    finally:
        page.close()


def test_research_result_renders_safe_markdown_and_exposes_snapshot_downloads(browser: Browser):
    page = _open(browser, report_snapshot_id=73)
    try:
        page.get_by_role("tab", name="Новое AI-исследование").click()
        composer = page.get_by_label("Сообщение аналитику")
        send = page.get_by_role("button", name="Отправить сообщение")
        composer.fill("Проверь условия")
        send.click()
        composer.fill("Сбербанк")
        send.click()
        page.get_by_role("heading", name="Итог").wait_for(state="visible")

        report = page.locator(".lp-research-evidence")
        assert report.get_by_role("listitem").inner_text() == "Проверенный источник"
        menu = report.get_by_text("Скачать исследование", exact=True)
        menu.click()
        pdf = report.get_by_role("link", name="PDF")
        word = report.get_by_role("link", name="Word")
        assert pdf.get_attribute("href").endswith("/research/reports/73/export/pdf")
        assert word.get_attribute("href").endswith("/research/reports/73/export/docx")
    finally:
        page.close()


def test_tabs_dates_and_internal_trust_match_final_contract(browser: Browser):
    page = _open(browser)
    try:
        tabs = page.get_by_role("tab").all_inner_texts()
        assert tabs == [context["title"] for context in ALL_CONTEXTS]
        assert page.get_by_role("tab", name="Общая база").get_attribute("aria-selected") == "true"

        headers = page.locator(".lp-table thead").first.inner_text()
        assert "Дата публикации" in headers
        assert "Собрано" in headers
        assert "Надёжность" not in headers
        assert "Trust" not in headers
        known_row = page.locator(".lp-table tbody tr", has_text="Льгота перевода")
        assert ":" in known_row.locator(".lp-cell-published").inner_text()
        assert ":" in known_row.locator(".lp-cell-collected").inner_text()
        assert known_row.locator(".lp-status").inner_text() == "подтверждено"
        unknown_row = page.locator(".lp-table tbody tr", has_text="Комиссия за перевод")
        assert unknown_row.locator(".lp-cell-published").inner_text() == "—"
        assert "30.08.2026" in unknown_row.locator(".lp-cell-collected").inner_text()
        assert ":" in unknown_row.locator(".lp-cell-collected").inner_text()
        assert page.locator(".lp-table tbody", has_text="published").count() == 0
    finally:
        page.close()


def test_tablist_keyboard_navigation_wraps_selects_and_focuses(browser: Browser):
    """Ломается, если tablist снова поддерживает только мышь."""
    page = _open(browser)
    try:
        page.get_by_role("tab", name="Общая база").focus()
        page.keyboard.press("ArrowLeft")
        page.wait_for_function(
            '() => document.activeElement.id === "lp-tab-admin" '
            '&& document.activeElement.getAttribute("aria-selected") === "true"'
        )

        page.keyboard.press("ArrowRight")
        page.wait_for_function(
            '() => document.activeElement.id === "lp-tab-catalog" '
            '&& document.activeElement.getAttribute("aria-selected") === "true"'
        )

        page.keyboard.press("End")
        page.wait_for_function(
            '() => document.activeElement.id === "lp-tab-admin" '
            '&& document.activeElement.getAttribute("aria-selected") === "true"'
        )
        page.keyboard.press("Home")
        page.wait_for_function(
            '() => document.activeElement.id === "lp-tab-catalog" '
            '&& document.activeElement.getAttribute("aria-selected") === "true"'
        )

        page.keyboard.press("ArrowDown")
        page.wait_for_function(
            '() => document.activeElement.id === "lp-tab-sources" '
            '&& document.activeElement.getAttribute("aria-selected") === "true"'
        )
        page.keyboard.press("ArrowUp")
        page.wait_for_function(
            '() => document.activeElement.id === "lp-tab-catalog" '
            '&& document.activeElement.getAttribute("aria-selected") === "true"'
        )
    finally:
        page.close()


def test_every_tab_controls_one_labelled_panel_including_denied_states(browser: Browser):
    """Ломается при отсутствующем panel, неверной связи или дублированном id."""
    page = _open(browser)
    try:
        for context in ALL_CONTEXTS:
            tab = page.get_by_role("tab", name=context["title"])
            panel_id = tab.get_attribute("aria-controls")
            assert panel_id == f"lp-panel-{context['id']}"
            panel = page.locator(f"#{panel_id}")
            assert panel.count() == 1
            assert panel.get_attribute("role") == "tabpanel"
            assert panel.get_attribute("aria-labelledby") == f"lp-tab-{context['id']}"
            tab.click()
            assert panel.is_visible()
    finally:
        page.close()

    denied_page = _open(browser, deny_protected=True)
    try:
        for context_id, title, denied_heading in (
            ("queue", "Очередь верификации", "Нет доступа к очереди верификации"),
            ("admin", "Управление доступом", "Нет доступа к администрированию"),
        ):
            tab = denied_page.get_by_role("tab", name=title)
            tab.click()
            denied_page.get_by_role("heading", name=denied_heading).wait_for(state="visible")
            panel = denied_page.locator(f"#lp-panel-{context_id}")
            assert panel.count() == 1
            assert panel.get_attribute("role") == "tabpanel"
            assert panel.get_attribute("aria-labelledby") == f"lp-tab-{context_id}"
    finally:
        denied_page.close()


def test_catalog_exposes_read_only_published_loophole_scope_without_false_query_params(
    browser: Browser,
):
    """Ломается, если catalog снова обещает неподдерживаемые verdict/status-фильтры."""
    page = _open(browser)
    try:
        assert page.locator("#lp-filter-verdict").count() == 0
        assert page.locator("#lp-filter-status").count() == 0
        assert page.get_by_label("Каталог показывает только лазейки").inner_text() == "лазейки"
        assert page.get_by_label("Каталог показывает только опубликованные записи").inner_text() == (
            "опубликованные"
        )

        page.get_by_label("Поиск по тексту").fill("комиссия")
        page.wait_for_function("() => window.__catalogUrls.length >= 2")
        assert page.evaluate(
            """() => window.__catalogUrls.every(url =>
              !url.includes("only_loophole=") && !/[?&]status=/.test(url))"""
        )
    finally:
        page.close()


def test_selected_csv_download_is_repeatable_and_preserves_selection(browser: Browser):
    page = _open(browser)
    try:
        page.locator("#lp-select-record-1").check()
        page.locator("#lp-select-record-3").check()
        assert page.locator(".lp-mark-panel").count() == 0
        page.get_by_role("button", name="CSV").click()
        page.wait_for_function("() => window.__exportBodies.length === 1")

        checkbox_accent = page.locator("#lp-select-record-1").evaluate(
            "element => getComputedStyle(element).accentColor"
        )
        csv_background = page.get_by_role("button", name="CSV").evaluate(
            "element => getComputedStyle(element).backgroundColor"
        )
        assert checkbox_accent == csv_background

        assert page.evaluate("window.__exportBodies[0]") == {
            "records": [1, 3],
            "format": "csv",
        }
        toast = page.get_by_role("status")
        assert "CSV сформирован · 2 записи" in toast.inner_text()
        assert toast.get_by_role("button", name="Скачать повторно").is_visible()
        assert page.locator("#lp-select-record-1").is_checked()
        assert page.locator("#lp-select-record-3").is_checked()
        toast.get_by_role("button", name="Скачать повторно").click()
        assert page.evaluate("window.__downloads.length") == 2

        page.get_by_role("tab", name="Добавить источник").click()
        page.get_by_label("URL веб-источника").fill("https://example.ru/tariffs")
        page.get_by_label("Что собирать").fill("Тарифы и комиссии")
        page.get_by_role("button", name="Создать и проверить").click()
        page.get_by_text("Парсер создан.").wait_for(state="visible")
        assert page.get_by_role("button", name="Скачать повторно").count() == 0
    finally:
        page.close()


def test_web_parser_lifecycle_is_inline_on_sources_tab(browser: Browser):
    page = _open(browser)
    try:
        page.get_by_role("tab", name="Добавить источник").click()
        page.get_by_role("heading", name="Новый парсер веб-источника").wait_for(state="visible")
        assert page.locator('[role="dialog"][aria-labelledby="lp-parsers-title"]').count() == 0
        assert "Telegram-источники" in page.locator(".lp-source-note").inner_text()

        page.get_by_label("URL веб-источника").fill("https://example.ru/tariffs")
        page.get_by_label("Что собирать").fill("Тарифы, комиссии и условия обслуживания")
        page.get_by_role("button", name="Создать и проверить").click()

        page.locator(".lp-log-panel").wait_for(state="visible")
        page.get_by_text("Проверка доступности — 200 OK").wait_for(state="visible")
        assert page.get_by_role("tab", name="Добавить источник").get_attribute("aria-selected") == "true"
    finally:
        page.close()


def test_parser_log_disconnect_closes_stream_and_shows_inline_error(browser: Browser):
    """Ломается, если оборванный EventSource остаётся в ложном состоянии «идёт»."""
    page = _open(browser, event_source_error=True)
    try:
        page.get_by_role("tab", name="Добавить источник").click()
        page.get_by_label("URL веб-источника").fill("https://example.ru/tariffs")
        page.get_by_label("Что собирать").fill("Тарифы и комиссии")
        page.get_by_role("button", name="Создать и проверить").click()

        error = page.locator(".lp-log-panel [role='alert']")
        error.wait_for(state="visible")
        assert "Соединение с журналом прервано" in error.inner_text()
        assert page.locator(".lp-log-panel .lp-log-running").count() == 0
        assert page.locator(".lp-log-panel .lp-log-done").count() == 0
        assert page.evaluate("window.__eventSources[0].closed") is True
    finally:
        page.close()


def test_parser_targets_link_only_safe_supported_addresses(browser: Browser):
    """Ломается, если target попадает в href без нормализации схемы."""
    targets = [
        "https://bank.example/tariffs",
        "http://bank.example/archive",
        "@bank_secrets",
        "t.me/bank_public/news",
        "javascript:alert(1)",
        "ftp://bank.example/dump",
    ]
    page = _open(browser, parser_targets=targets)
    try:
        page.get_by_role("tab", name="Добавить источник").click()
        target_list = page.locator(".lp-parser-targets")
        links = target_list.locator("a").evaluate_all(
            """elements => elements.map(element => ({
              text: element.textContent,
              href: element.getAttribute("href"),
            }))"""
        )
        assert links == [
            {"text": "https://bank.example/tariffs", "href": "https://bank.example/tariffs"},
            {"text": "http://bank.example/archive", "href": "http://bank.example/archive"},
            {"text": "@bank_secrets", "href": "https://t.me/bank_secrets"},
            {"text": "t.me/bank_public/news", "href": "https://t.me/bank_public/news"},
        ]
        assert target_list.get_by_text("javascript:alert(1)", exact=True).is_visible()
        assert target_list.get_by_text("ftp://bank.example/dump", exact=True).is_visible()
    finally:
        page.close()


def test_secondary_surfaces_use_final_board_composition(browser: Browser):
    page = _open(browser)
    try:
        page.get_by_role("tab", name="Новое AI-исследование").click()
        page.get_by_role("heading", name="Новое AI-исследование").wait_for(state="visible")
        assert page.locator(".lp-research-board").is_visible()
        assert page.locator(".lp-research-card").count() >= 3
        assert page.get_by_role("complementary", name="Аналитик лазеек").is_visible()

        page.get_by_role("tab", name="Очередь верификации").click()
        page.get_by_role("heading", name="Очередь верификации").wait_for(state="visible")
        page.locator(".lp-queue-review").wait_for(state="visible")
        columns = page.locator(".lp-queue-review").evaluate(
            "element => getComputedStyle(element).gridTemplateColumns"
        )
        assert len(columns.split()) == 2
        assert page.locator(".lp-queue-card[aria-current='true']").count() == 1
        detail = page.locator(".lp-queue-detail")
        assert detail.get_by_text("Дата публикации", exact=True).count() == 1
        assert detail.get_by_text("Собрано", exact=True).count() == 1
        assert detail.get_by_role("button", name="Проверить вердикт").is_visible()
        page.locator(".lp-queue-card").nth(1).click()
        assert "Комиссия за перевод" in detail.inner_text()

        page.get_by_role("tab", name="Управление доступом").click()
        page.get_by_role("heading", name="Управление доступом").wait_for(state="visible")
        admin = page.locator(".lp-admin")
        admin.wait_for(state="visible")
        admin_columns = admin.evaluate(
            "element => getComputedStyle(element).gridTemplateColumns"
        )
        assert len(admin_columns.split()) == 2
        assert page.locator(".lp-admin-section").count() == 2
        assert admin.get_by_role("heading", name="Статус Telegram-целей").count() == 0
        assert admin.get_by_role("heading", name="Роль ЦК КС").is_visible()
        assert admin.get_by_role("heading", name="Сводный аудит").is_visible()
        assert page.evaluate("window.__adminTelegramTargetFetches") == 0
        assert page.locator(".lp-admin-section").first.evaluate(
            "element => getComputedStyle(element).gridRowEnd"
        ) == "auto"
        assert page.locator(".lp-admin-section").first.evaluate(
            "element => getComputedStyle(element).borderTopStyle"
        ) != "none"
        assert admin.get_by_text("активна", exact=True).is_visible()
        assert admin.get_by_text("30.08.2026, 09:15", exact=True).is_visible()
        assert admin.get_by_text("3", exact=True).is_visible()
    finally:
        page.close()


@pytest.mark.parametrize("width", [1440, 1200, 992, 735, 390])
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_breakpoints_have_no_root_overflow_or_clipped_persistent_controls(
    browser: Browser,
    width: int,
    dark: bool,
):
    page = _open(browser, width=width)
    try:
        page.evaluate("dark => document.documentElement.classList.toggle('dark', dark)", dark)
        metrics = page.evaluate(
            """() => {
              const viewport = window.innerWidth;
              const selectors = ['.lp-main-header', '.lp-context-nav', '.lp-header-actions'];
              return {
                viewport,
                rootWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
                boxes: selectors.map(selector => {
                  const rect = document.querySelector(selector).getBoundingClientRect();
                  return {selector, left: rect.left, right: rect.right, width: rect.width};
                }),
              };
            }"""
        )
        assert metrics["rootWidth"] <= metrics["viewport"] + 1
        for box in metrics["boxes"]:
            assert box["left"] >= -1, box
            assert box["right"] <= metrics["viewport"] + 1, box
            assert box["width"] > 0, box
        assert page.get_by_role("button", name="CSV").is_visible()
        assert page.get_by_role("tab", name="Добавить источник").is_visible()
    finally:
        page.close()
