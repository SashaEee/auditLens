"""Интерфейс вкладки «Уязвимости» в системе AuditLens (макет 27.09).

Страница собирается из настоящих loophole.jsx / loophole.css и vendor-React;
API подменено заглушкой, которая записывает каждый запрос в window.__calls.
Проверяется поведение, а не разметка: какие запросы уходят, что видит
пользователь, как работают отмена решения, дозагрузка, суть только для
находок и фиксация записей исследования в базе.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "bank_audit" / "loophole" / "static"
VENDOR = ROOT / "src" / "bank_audit" / "web" / "static" / "vendor"

CONTEXTS = [
    {"id": "catalog", "title": "Общая база"},
    {"id": "ai_research", "title": "AI-исследования"},
    {"id": "queue", "title": "Очередь верификации"},
    {"id": "admin", "title": "Управление доступом"},
]
AUDITOR = CONTEXTS[:2]


def _rec(rid, title, kind, *, bank="sberbank", conf=0.8, manual=False, day=20):
    return {
        "record_id": rid, "title": title, "url": f"https://forum.example/t/{rid}",
        "domain": "forum.example", "bank_slug": bank, "classification": kind,
        "is_loophole": kind != "not_confirmed", "verdict_confidence": 1.0 if manual else conf,
        "verdict_reason": f"Комментарий классификатора: {title}",
        "verdict_model": "manual" if manual else "gpt", "status": "preliminary",
        "content_status": "full", "snippet": f"Фрагмент обсуждения: {title}",
        "published_at": f"2026-09-{day:02d}T10:00:00+03:00",
        "collected_at": f"2026-09-{day:02d}T12:00:00+03:00",
        "awaiting": kind != "not_confirmed" and not manual, "reviewed": manual,
    }


RECORDS = [
    _rec(1, "Кэшбэк за переводы между своими картами", "vulnerability", conf=0.91, day=26),
    _rec(2, "Подмена QR-кода на кассе", "fraud_scheme", conf=0.86, day=25),
    _rec(3, "Льготный период при частичном погашении", "vulnerability", bank="vtb",
         manual=True, day=24),
    _rec(4, "Списание за неактивность", "not_confirmed", conf=0.3, day=23),
]
SUMMARY = {
    "totals": {"total": 78616, "vulnerability": 312, "fraud_scheme": 131, "not_confirmed": 78173,
               "awaiting": 291, "awaiting_vulnerability": 204, "awaiting_fraud_scheme": 87,
               "new_7d": 38, "new_prev_7d": 29},
    "facets": {"types": {"vulnerability": 2, "fraud_scheme": 1, "not_confirmed": 1,
                         "confirmed": 3, "all": 4},
               "awaiting": 2,
               "banks": [{"slug": "sberbank", "count": 3}, {"slug": "vtb", "count": 1}]},
}

STUB = r"""
window.__calls = [];
window.__cfg = %(cfg)s;
const RECS = %(records)s, SUMMARY = %(summary)s;
if (window.__cfg.rich) {
  Object.assign(RECS[1], {headline: "Подмена QR-кода поверх кода магазина",
    summary_doubt: "похоже на новость без описания приёма.", copy_ids: [1]});
  Object.assign(RECS[0], {copy_ids: [2]});
}
const J = (v, s = 200) => new Response(JSON.stringify(v), {status: s, headers: {"Content-Type": "application/json"}});
const SSE = events => new Response(events.map(([n, d]) => "event: " + n + "\ndata: " + JSON.stringify(d)).join("\n\n") + "\n\n",
  {status: 200, headers: {"Content-Type": "text/event-stream"}});
URL.createObjectURL = () => "blob:x"; URL.revokeObjectURL = () => {};
HTMLAnchorElement.prototype.click = function () { window.__calls.push({method: "DOWNLOAD", url: this.download}); };
navigator.sendBeacon = (url, blob) => { window.__calls.push({method: "BEACON", url}); return true; };
window.fetch = async (input, init = {}) => {
  const url = String(input), method = (init.method || "GET").toUpperCase();
  let body = null; try { body = init.body ? JSON.parse(init.body) : null; } catch (e) { body = init.body; }
  window.__calls.push({method, url, body});
  const cfg = window.__cfg;
  if (url.endsWith("/contexts")) {
    if (cfg.authz === "deny") return J({detail: "no"}, 403);
    if (cfg.authz === "error") throw new TypeError("network");
    return J({contexts: cfg.contexts, capabilities: {can_mark_verdict: cfg.contexts.some(c => c.id === "queue")}});
  }
  if (url.endsWith("/banks")) return J({banks: ["sber", "sberbank", "vtb"]});
  if (url.includes("/catalog/summary")) return J(SUMMARY);
  if (url.includes("/api/loophole/catalog") && cfg.catalogFails && !window.__catalogRecovered) return J({detail: "boom"}, 500);
  if (url.includes("/api/loophole/catalog") && cfg.empty) return J({records: [], total: 0});
  if (url.includes("/api/loophole/catalog")) {
    const q = new URLSearchParams(url.split("?")[1] || "");
    const cls = q.get("classification"), offset = +(q.get("offset") || 0);
    if (cfg.many) {
      const all = Array.from({length: 60}, (_, i) => Object.assign({}, RECS[0], {record_id: 1000 + i, title: "Запись " + (i + 1)}));
      return J({records: all.slice(offset, offset + 50), total: 60});
    }
    let rows = RECS.filter(r => cls === "all" || (cls === "confirmed" ? r.classification !== "not_confirmed" : r.classification === cls));
    return J({records: rows, total: rows.length});
  }
  let m = url.match(/\/records\/(\d+)\/content/);
  if (m) {
    const r = RECS.find(x => x.record_id === +m[1]) || Object.assign({}, RECS[0], {record_id: +m[1]});
    const failed = cfg.contentFailed && r.record_id === 2;
    return J(Object.assign({}, r, {raw_text: failed ? null : "Полный текст " + r.title, raw_text_len: failed ? 0 : 900,
      content_status: failed ? "fetch_failed" : r.content_status,
      classifier_verdict_reason: r.record_id === 3 ? "Исходный комментарий модели" : (r.record_id === 2 ? null : r.verdict_reason),
      verdict_reason: r.record_id === 3 ? "Подтверждено экспертом" : r.verdict_reason,
      summary: null, provenance: null,
      decisions: r.verdict_model === "manual" ? [{decision_id: 9, decision: r.classification,
        decided_by: "expert.ivanova", decided_at: "2026-09-25T10:00:00+03:00", comment: "Подтверждено"}] : [],
      expert_decisions: cfg.rich && r.verdict_model === "manual" ? [{decision_id: 1, decided_by: "expert.petrov",
        decided_at: "2026-09-26T12:00:00+03:00", previous: "not_confirmed", decision: r.classification,
        comment: "Перепроверил: механизм работает", source: "base"}] : []}));
  }
  m = url.match(/\/records\/(\d+)\/summary/);
  if (m) return J({summary: "Суть записи " + m[1] + ": механизм и кто теряет.", generated: true});
  if (url.includes("/export/catalog.xlsx") && cfg.exportFails) throw new TypeError("Failed to fetch");
  if (url.includes("/export/catalog.xlsx")) return new Response("xlsx", {status: 200, headers: {
    "Content-Disposition": 'attachment; filename="AuditLens_uyazvimosti_2026-09-27.xlsx"'}});
  if (url.endsWith("/api/cases") && method === "GET") return J({cases: [{case_id: 7, title: "СБП и кэшбэк", items: 2}]});
  if (/\/api\/cases\/\d+\/items/.test(url)) return J({item_id: 1});
  if (url.includes("/queue")) {
    const rows = RECS.filter(r => r.awaiting).map(r => Object.assign({}, r, {decisions: []}));
    if (url.includes("sort=conf")) rows.sort((a, b) => b.verdict_confidence - a.verdict_confidence);
    return J({records: rows, count: rows.length, total: rows.length});
  }
  if (url.endsWith("/records/verdict")) return J({updated: body.record_ids, skipped: []});
  if (url.endsWith("/admin/roles")) return J({roles: [{username: "expert.ivanova", status: "active",
    created_at: "2026-09-01T10:00:00+03:00"}], active_experts: 1, max_experts: 5});
  if (url.endsWith("/admin/audit")) return J({events: []});
  if (url.endsWith("/admin/roles/revoke")) return J({ok: true});
  if (url.endsWith("/workspaces")) return J({workspaces: [
    {workspace_id: 1, name: "Новое исследование", created_at: "2026-09-27T09:00:00+03:00"},
    {workspace_id: 2, name: "Кэшбэк за переводы", created_at: "2026-09-26T09:00:00+03:00"}]});
  if (url.endsWith("/history/1")) return J({workspace: {workspace_id: 1, name: "Новое исследование", user_id: "u"},
    messages: [], reports: [], findings: [], read_only: false});
  if (url.endsWith("/history/2")) return J({workspace: {workspace_id: 2, name: "Кэшбэк за переводы", user_id: "u"},
    messages: [{message_id: 1, role: "user", content: "Найди лазейки в кэшбэке"},
               {message_id: 2, role: "assistant", content: "Нашёл механизм.", report_id: 5}],
    reports: [], findings: [RECS[0]], read_only: false});
  if (url.includes("/findings")) return J({findings: [RECS[1]]});
  if (url.endsWith("/workspace")) return J({workspace_id: 1});
  if (url.endsWith("/chat") && method === "POST") {
    if (!body.clarify_token) return SSE([["phase", {phase: "clarify"}], ["phase", {phase: "await_clarify"}],
      ["question", {clarification_token: "t1", questions: [{id: "banks", question: "Какие банки смотреть?", type: "multi",
        allow_other: false, options: [{value: "sber", label: "Сбербанк"}, {value: "all", label: "Все банки"}]}]}]]);
    return SSE([["phase", {phase: "execute"}], ["tool_call", {name: "audit_web_search"}],
      ["records", {records: [{record_id: 999, title: "Запись агента"}]}],
      ["token", "Итог исследования"], ["done", {}]]);
  }
  if (url.endsWith("/clarify/answer")) return J({enriched_question: "кэшбэк (Сбербанк)", execution_token: "e1", answer_message: "Сбербанк"});
  return J({});
};
"""


def _html(**cfg) -> str:
    cfg = {"contexts": CONTEXTS, "authz": "ok", "many": False, "catalogFails": False,
           "empty": False, "exportFails": False, "contentFailed": False, "rich": False, **cfg}
    compiled = cfg.pop("compiled", False)
    stub = STUB % {"cfg": json.dumps(cfg, ensure_ascii=False),
                   "records": json.dumps(RECORDS, ensure_ascii=False),
                   "summary": json.dumps(SUMMARY, ensure_ascii=False)}
    names = ("react.min.js", "react-dom.min.js") + (() if compiled else ("babel.min.js",))
    vendor = "".join(f"<script>{(VENDOR / name).read_text(encoding='utf-8')}</script>" for name in names)
    app = (f"<script>{(STATIC / 'loophole.js').read_text(encoding='utf-8')}</script>" if compiled
           else f'<script type="text/babel">{(STATIC / "loophole.jsx").read_text(encoding="utf-8")}</script>')
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<style>{(STATIC / 'loophole.css').read_text(encoding='utf-8')}</style></head>"
            f'<body><div id="loophole-root"></div>{vendor}<script>{stub}</script>{app}'
            "</body></html>")


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def _open(browser, *, width=1280, clock=False, wait_list=True, **cfg):
    page = browser.new_page(viewport={"width": width, "height": 900},
                            timezone_id="Europe/Moscow", locale="ru-RU")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page._lp_errors = errors
    if clock:
        page.clock.install()
    page.set_content(_html(**cfg))
    if wait_list:
        page.locator(".lp-c").first.wait_for(timeout=15000)
    return page


def _calls(page, pattern, method=None):
    return [c for c in page.evaluate("window.__calls")
            if re.search(pattern, c["url"]) and (method is None or c["method"] == method)]


def _last_catalog(page):
    urls = [c["url"] for c in page.evaluate("window.__calls")
            if "/api/loophole/catalog?" in c["url"] and "/summary" not in c["url"]]
    return urls[-1]


# ── Шапка, вкладки, доступ ───────────────────────────────────────────────────


def test_head_tabs_and_access_follow_server_contexts(browser):
    page = _open(browser)
    expect(page.get_by_role("heading", name="Уязвимости", level=1)).to_be_visible()
    tabs = page.get_by_role("tab")
    expect(tabs).to_have_count(3)
    assert [t.inner_text().split("\n")[0].strip() for t in tabs.all()] == ["База", "Исследовать", "Очередь"]
    expect(page.get_by_role("button", name="Доступ")).to_be_visible()
    expect(page.get_by_role("tab", name="Добавить источник")).to_have_count(0)
    page.close()

    auditor = _open(browser, contexts=AUDITOR)
    expect(auditor.get_by_role("tab")).to_have_count(2)
    expect(auditor.get_by_role("button", name="Доступ")).to_have_count(0)
    assert not auditor._lp_errors
    auditor.close()


def test_fail_closed_surfaces_show_no_data(browser):
    denied = _open(browser, authz="deny", wait_list=False)
    expect(denied.get_by_role("heading", name="Нет доступа к модулю «Уязвимости»")).to_be_visible()
    assert not _calls(denied, r"/catalog")
    denied.close()
    broken = _open(browser, authz="error", wait_list=False)
    expect(broken.get_by_role("heading", name="Сервис недоступен")).to_be_visible()
    expect(broken.get_by_role("button", name="Повторить")).to_be_visible()
    broken.close()


# ── База: сводка, фильтры, сортировка ────────────────────────────────────────


def test_summary_cards_and_filters_drive_catalog_requests(browser):
    page = _open(browser)
    cards = page.locator(".lp-kpi")
    expect(cards).to_have_count(4)
    expect(cards.nth(0)).to_contain_text("291")
    expect(cards.nth(2)).to_contain_text("312")
    assert "classification=confirmed" in _last_catalog(page)

    cards.nth(0).click()
    expect(cards.nth(0)).to_have_attribute("aria-pressed", "true")
    page.wait_for_timeout(500)
    assert "verification_status=awaiting" in _last_catalog(page)

    page.get_by_role("button", name="по вероятности").click()
    page.wait_for_timeout(500)
    assert "sort=conf" in _last_catalog(page)

    page.get_by_role("button", name="Все банки").click()
    options = page.locator(".lp-pop .lp-pi")
    # Коды-синонимы (sber / sberbank) — одна строка, со счётчиком среза.
    expect(options).to_have_count(2)
    expect(options.nth(0)).to_contain_text("Сбербанк")
    expect(options.nth(0)).to_contain_text("3")
    options.nth(1).click()
    page.get_by_role("button", name="Готово").click()
    page.wait_for_timeout(500)
    assert "bank_slugs=vtb" in _last_catalog(page)
    expect(page.locator(".lp-fchip", has_text="ВТБ")).to_be_visible()
    page.get_by_role("button", name="Сбросить все").click()
    page.wait_for_timeout(500)
    assert "bank_slugs" not in _last_catalog(page)
    page.close()


def test_records_without_finding_are_hidden_by_default_with_a_way_back(browser):
    page = _open(browser)
    expect(page.locator(".lp-c")).to_have_count(3)
    banner = page.locator(".lp-hid")
    expect(banner).to_contain_text("Ещё 1 запись без находки скрыты")
    banner.get_by_role("button", name="Показать все").click()
    expect(page.locator(".lp-c")).to_have_count(4)
    assert "classification=all" in _last_catalog(page)
    page.close()


# ── База: карточка записи ───────────────────────────────────────────────────


def test_reader_opens_record_and_keyboard_moves_through_list(browser):
    page = _open(browser)
    reader = page.locator(".lp-rd")
    expect(reader.locator(".lp-rd-title")).to_have_text("Кэшбэк за переводы между своими картами")
    page.locator(".lp-c").nth(1).click()
    expect(reader.locator(".lp-rd-title")).to_have_text("Подмена QR-кода на кассе")
    expect(page.locator(".lp-c-title").nth(1)).to_have_attribute("aria-current", "true")
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("j")
    expect(reader.locator(".lp-rd-title")).to_have_text("Льготный период при частичном погашении")
    expect(reader).to_contain_text("Проверено экспертом")
    expect(reader.locator(".lp-tl")).to_contain_text("expert.ivanova")
    page.keyboard.press("k")
    expect(reader.locator(".lp-rd-title")).to_have_text("Подмена QR-кода на кассе")
    page.keyboard.press("/")
    expect(page.locator("#lp-filter-text")).to_be_focused()
    page.close()


def test_summary_is_generated_only_for_vulnerabilities_and_schemes(browser):
    page = _open(browser)
    expect(page.locator(".lp-rd")).to_contain_text("Суть записи 1: механизм и кто теряет.")
    page.locator(".lp-hid").get_by_role("button", name="Показать все").click()
    page.locator(".lp-c", has_text="Списание за неактивность").click()
    reader = page.locator(".lp-rd")
    expect(reader.locator(".lp-rd-title")).to_have_text("Списание за неактивность")
    expect(reader).to_contain_text("Комментарий классификатора")
    page.wait_for_timeout(300)
    posted = [c["url"] for c in _calls(page, r"/summary$", "POST")]
    assert any(u.endswith("/records/1/summary") for u in posted)
    assert not any(u.endswith("/records/4/summary") for u in posted)
    page.close()


def test_show_more_appends_page_and_keeps_selection(browser):
    page = _open(browser, many=True)
    expect(page.locator(".lp-c")).to_have_count(50)
    page.get_by_role("button", name="Выбрать", exact=True).click()
    page.locator("#lp-select-record-1000").check()
    expect(page.locator(".lp-selbar")).to_contain_text("Выбрано 1")
    page.get_by_role("button", name="Показать ещё 10").click()
    expect(page.locator(".lp-c")).to_have_count(60)
    assert "offset=50" in _last_catalog(page)
    expect(page.locator(".lp-selbar")).to_contain_text("Выбрано 1")
    page.close()


def test_excel_export_sends_filters_or_marked_records(browser):
    page = _open(browser)
    page.get_by_role("button", name="Excel").click()
    expect(page.locator(".lp-toast")).to_contain_text("Excel сформирован")
    first = _calls(page, r"/export/catalog\.xlsx", "POST")[-1]["body"]
    assert first["classification"] == "confirmed" and "record_ids" not in first
    assert page.evaluate("window.__calls").count(
        {"method": "DOWNLOAD", "url": "AuditLens_uyazvimosti_2026-09-27.xlsx"}) == 1
    page.get_by_role("button", name="Выбрать", exact=True).click()
    page.locator("#lp-select-record-2").check()
    page.get_by_role("button", name="Excel · 1").click()
    page.wait_for_timeout(300)
    assert _calls(page, r"/export/catalog\.xlsx", "POST")[-1]["body"] == {"record_ids": [2]}
    page.close()


def test_record_goes_to_audit_case_as_document(browser):
    page = _open(browser)
    page.locator(".lp-rd").get_by_role("button", name="Добавить в аудит-дело").click()
    page.locator(".lp-pop").get_by_role("button", name=re.compile("СБП и кэшбэк")).click()
    expect(page.locator(".lp-toast")).to_contain_text("Добавлено в дело «СБП и кэшбэк»")
    item = _calls(page, r"/api/cases/7/items", "POST")[-1]["body"]
    assert item["kind"] == "document" and item["url"] == "https://forum.example/t/1"
    assert item["title"] == "Кэшбэк за переводы между своими картами"
    assert "Уязвимость" in item["note"]
    page.close()


def test_catalog_error_is_not_masked_as_empty_and_can_be_retried(browser):
    page = _open(browser, catalogFails=True, wait_list=False)
    expect(page.get_by_role("alert")).to_contain_text("Не удалось загрузить записи")
    expect(page.get_by_text("Ничего не нашлось")).to_have_count(0)
    page.evaluate("window.__catalogRecovered = true")
    page.get_by_role("alert").get_by_role("button", name="Повторить").click()
    expect(page.locator(".lp-c")).to_have_count(3)
    page.close()


def test_empty_selection_offers_reset_and_research(browser):
    page = _open(browser, empty=True, wait_list=False)
    expect(page.get_by_text("Ничего не нашлось")).to_be_visible()
    expect(page.get_by_role("button", name="Сбросить фильтры")).to_be_visible()
    expect(page.get_by_role("button", name="Исследовать", exact=True)).to_be_visible()
    page.close()


def test_export_network_failure_is_an_error_toast(browser):
    page = _open(browser, exportFails=True)
    page.get_by_role("button", name="Excel").click()
    toast = page.get_by_role("alert")
    expect(toast).to_contain_text("Не удалось сформировать Excel")
    expect(page.locator(".lp-toast-error")).to_have_count(1)
    page.close()


def test_record_text_is_cached_and_failed_text_keeps_card_usable(browser):
    page = _open(browser, contentFailed=True)
    items = page.locator(".lp-c")
    items.nth(1).click()
    reader = page.locator(".lp-rd")
    expect(reader).to_contain_text("Полный контент не удалось загрузить; показан сохранённый фрагмент.")
    expect(reader.locator(".lp-quote")).to_contain_text("Фрагмент обсуждения: Подмена QR-кода")
    expect(reader.get_by_role("button", name=re.compile("Полный текст"))).to_have_count(0)
    items.nth(0).click()
    reader.get_by_role("button", name=re.compile("Полный текст")).click()
    expect(reader.locator(".lp-fulltext")).to_have_text("Полный текст Кэшбэк за переводы между своими картами")
    items.nth(1).click()
    items.nth(0).click()
    assert len(_calls(page, r"/records/1/content$")) == 1
    assert len(_calls(page, r"/records/2/content$")) == 1
    page.close()


def test_history_shows_frozen_classifier_comment_and_expert_decisions(browser):
    page = _open(browser)
    page.locator(".lp-c", has_text="Льготный период").click()
    timeline = page.locator(".lp-rd .lp-tl")
    # Комментарий модели — замороженный classifier_verdict_reason, а не решение эксперта.
    expect(timeline).to_contain_text("«Исходный комментарий модели»")
    expect(timeline).to_contain_text("Решение ЦК КС (expert.ivanova): уязвимость")
    expect(timeline).to_contain_text("«Подтверждено»")
    expect(timeline).to_contain_text("25.09.2026")
    # Без замороженного комментария — запасной verdict_reason записи.
    page.locator(".lp-c", has_text="Подмена QR-кода").click()
    page.get_by_role("tab", name=re.compile("Очередь")).click()
    page.locator(".lp-qi", has_text="Подмена QR-кода").click()
    expect(page.locator(".lp-qcard .lp-tl")).to_contain_text(
        "«Комментарий классификатора: Подмена QR-кода на кассе»")
    page.close()


# ── Очередь: решение на карточке, отмена ────────────────────────────────────


def test_queue_decision_requires_comment_and_can_be_undone(browser):
    page = _open(browser, clock=True)
    page.get_by_role("tab", name=re.compile("Очередь")).click()
    items = page.locator(".lp-qi")
    expect(items).to_have_count(2)
    save = page.get_by_role("button", name="Сохранить решение")
    page.get_by_role("radio", name=re.compile("Мошенническая схема")).click()
    expect(save).to_be_disabled()
    page.get_by_label("Комментарий участника ЦК").fill("Подтверждаю по трём обсуждениям")
    expect(save).to_be_enabled()
    save.click()
    expect(items).to_have_count(1)
    toast = page.locator(".lp-toast")
    expect(toast).to_contain_text("запишется через 10 секунд")
    toast.get_by_role("button", name="Отменить").click()
    expect(items).to_have_count(2)
    assert not _calls(page, r"/records/verdict$", "POST")
    # Черновик вернулся вместе с записью — повторное сохранение в один клик.
    expect(page.get_by_label("Комментарий участника ЦК")).to_have_value("Подтверждаю по трём обсуждениям")
    save.click()
    page.clock.run_for(10_500)
    page.wait_for_timeout(200)
    sent = _calls(page, r"/records/verdict$", "POST")
    assert len(sent) == 1
    assert sent[0]["body"]["classification"] == "fraud_scheme"
    assert sent[0]["body"]["comment"] == "Подтверждаю по трём обсуждениям"
    page.close()


def test_queue_order_comes_from_server(browser):
    page = _open(browser)
    page.get_by_role("tab", name=re.compile("Очередь")).click()
    expect(page.locator(".lp-qi")).to_have_count(2)
    page.get_by_role("button", name="по вероятности").click()
    page.wait_for_timeout(300)
    assert any("/queue?sort=conf" in c["url"] for c in page.evaluate("window.__calls"))
    page.close()


# ── Исследование ────────────────────────────────────────────────────────────


def test_research_clarifies_with_chips_and_never_replaces_the_base(browser):
    page = _open(browser)
    page.get_by_role("tab", name="Исследовать").click()
    expect(page.get_by_role("heading", name="Что проверить?")).to_be_visible()
    page.get_by_role("button", name="Схемы с оплатой по QR-коду в СБП").click()
    expect(page.locator(".lp-clar")).to_contain_text("Какие банки смотреть?")
    page.locator(".lp-opt", has_text="Сбербанк").click()
    page.get_by_role("button", name="Начать исследование").click()
    expect(page.locator(".lp-msg-a").last).to_contain_text("Итог исследования")
    expect(page.locator(".lp-cands")).to_contain_text("Подмена QR-кода на кассе")
    # Событие records из потока не подменяет список «Базы».
    page.get_by_role("tab", name=re.compile("База")).click()
    expect(page.locator(".lp-c", has_text="Запись агента")).to_have_count(0)
    expect(page.locator(".lp-c")).to_have_count(3)
    page.close()


def test_saved_research_shows_findings_that_open_in_the_base(browser):
    page = _open(browser)
    page.get_by_role("tab", name="Исследовать").click()
    page.get_by_role("button", name="Открыть исследование Кэшбэк за переводы").click()
    cands = page.locator(".lp-cands")
    expect(cands).to_contain_text("Находки исследования · 1")
    expect(page.get_by_role("button", name="PDF")).to_be_visible()
    cands.locator(".lp-cand").first.click()
    expect(page.get_by_role("tab", name=re.compile("База"))).to_have_attribute("aria-selected", "true")
    expect(page.locator(".lp-rd .lp-rd-title")).to_have_text("Кэшбэк за переводы между своими картами")
    page.close()


# ── Доступ ──────────────────────────────────────────────────────────────────


def test_access_sheet_revokes_role_only_after_undo_window(browser):
    page = _open(browser, clock=True)
    page.get_by_role("button", name="Доступ").click()
    sheet = page.get_by_role("dialog", name="Доступ к модулю")
    expect(sheet).to_contain_text("expert.ivanova")
    sheet.get_by_role("button", name="Отозвать").click()
    expect(sheet).not_to_contain_text("expert.ivanova")
    page.locator(".lp-toast").get_by_role("button", name="Отменить").click()
    expect(sheet).to_contain_text("expert.ivanova")
    assert not _calls(page, r"/admin/roles/revoke", "POST")
    sheet.get_by_role("button", name="Отозвать").click()
    page.clock.run_for(10_500)
    page.wait_for_timeout(200)
    assert _calls(page, r"/admin/roles/revoke", "POST")[-1]["body"] == {"username": "expert.ivanova"}
    page.keyboard.press("Escape")
    expect(sheet).to_have_count(0)
    page.close()


# ── Телефон ─────────────────────────────────────────────────────────────────


def test_phone_width_shows_list_then_record_then_back(browser):
    page = _open(browser, width=390)
    reader = page.locator(".lp-rd-wrap")
    expect(reader).to_be_hidden()
    page.locator(".lp-c").nth(1).click()
    expect(reader).to_be_visible()
    expect(page.locator(".lp-list")).to_be_hidden()
    page.locator(".lp-rd").get_by_role("button", name="База").click()
    expect(page.locator(".lp-list")).to_be_visible()
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390
    page.close()


# ── Статический контракт ────────────────────────────────────────────────────


def _css():
    return (STATIC / "loophole.css").read_text(encoding="utf-8")


def test_colors_come_only_from_tokens():
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    outside = re.sub(r"(:root|html\.dark)\s*\{[^}]*\}", "", css)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|oklch\(|rgba?\(|hsla?\(", outside)


def test_ui_uses_svg_icons_not_emoji():
    jsx = (STATIC / "loophole.jsx").read_text(encoding="utf-8")
    assert not re.findall(r"[\U0001F300-\U0001FAFF☀-➿]", jsx)
    assert "function Icon(" in jsx


def test_legacy_source_panel_is_kept_unchanged_and_isolated():
    css = _css()
    assert "Наследие модуля" in css
    legacy = css[css.index("Наследие модуля"):]
    for rule in (".lp-sources-surface", ".lp-source-card", ".lp-parsers-modal"):
        assert rule in legacy
    # Новые компоненты не делят имена классов со старой панелью.
    new = set(re.findall(r"\.(lp-[a-z0-9-]+)", css[:css.index("Наследие модуля")]))
    old = set(re.findall(r"\.(lp-[a-z0-9-]+)", legacy))
    assert new & old <= {"lp-btn-primary"}



# ── Исправления после аудита 27.09 ──────────────────────────────────────────


def test_prebuilt_page_runs_without_babel(browser):
    page = _open(browser, compiled=True)
    expect(page.get_by_role("heading", name="Уязвимости", level=1)).to_be_visible()
    assert page.evaluate("typeof window.Babel") == "undefined"
    assert not page._lp_errors
    page.close()


def test_not_confirmed_shows_no_probability_and_cards_say_what_is_checked(browser):
    page = _open(browser)
    cards = page.locator(".lp-kpi")
    expect(cards.nth(2)).to_contain_text("проверено экспертом:")
    page.locator(".lp-hid").get_by_role("button", name="Показать все").click()
    item = page.locator(".lp-c", has_text="Списание за неактивность")
    expect(item).not_to_contain_text("вероятность")
    item.click()
    facts = page.locator(".lp-rd .lp-facts")
    expect(facts).to_contain_text("находки нет")
    expect(page.locator(".lp-rd")).not_to_contain_text("Уверенность модели")
    page.close()


def test_new_selection_opens_its_first_record_and_arrows_move_through_list(browser):
    page = _open(browser)
    page.locator(".lp-c").nth(2).click()
    expect(page.locator(".lp-rd .lp-rd-title")).to_have_text("Льготный период при частичном погашении")
    page.get_by_role("group", name="Тип записи").get_by_role("button", name=re.compile("^Схемы")).click()
    # Карточка прошлой выборки не остаётся: открыта первая запись новой.
    expect(page.locator(".lp-rd .lp-rd-title")).to_have_text("Подмена QR-кода на кассе")
    page.get_by_role("group", name="Тип записи").get_by_role("button", name="Уязвимости и схемы").click()
    page.locator(".lp-c-title").first.focus()
    page.keyboard.press("ArrowDown")
    expect(page.locator(".lp-c-title").nth(1)).to_be_focused()
    expect(page.locator(".lp-rd .lp-rd-title")).to_have_text("Подмена QR-кода на кассе")
    page.close()


def test_headline_doubt_and_expert_history_in_the_card(browser):
    page = _open(browser, rich=True)
    page.locator(".lp-c").nth(1).click()
    reader = page.locator(".lp-rd")
    expect(reader.locator(".lp-rd-title")).to_have_text("Подмена QR-кода поверх кода магазина")
    expect(reader.locator(".lp-rd-topic")).to_have_text("Тема: Подмена QR-кода на кассе")
    expect(reader.locator(".lp-callout-doubt")).to_contain_text(
        "Модель сомневается: похоже на новость без описания приёма.")
    expect(reader).to_contain_text("Копии")
    page.locator(".lp-c", has_text="Льготный период").click()
    expect(reader.locator(".lp-tl")).to_contain_text(
        "Решение ЦК КС (expert.petrov): не подтверждено → уязвимость")
    expect(reader.locator(".lp-tl")).to_contain_text("«Перепроверил: механизм работает»")
    page.close()


def test_queue_decision_applies_to_exact_copies(browser):
    page = _open(browser, rich=True, clock=True)
    page.get_by_role("tab", name=re.compile("Очередь")).click()
    items = page.locator(".lp-qi")
    expect(items).to_have_count(2)
    expect(items.first).to_contain_text("+1 копия")
    copies = page.get_by_label(re.compile("Применить и к 1 копии в очереди"))
    expect(copies).to_be_checked()
    page.get_by_role("radio", name=re.compile("Не подтверждено")).click()
    page.get_by_label("Комментарий участника ЦК").fill("Одна и та же новость")
    page.get_by_role("button", name="Сохранить решение").click()
    expect(items).to_have_count(0)
    expect(page.locator(".lp-toast")).to_contain_text("для 2 записей")
    page.clock.run_for(10_500)
    page.wait_for_timeout(200)
    body = _calls(page, r"/records/verdict$", "POST")[-1]["body"]
    assert sorted(body["record_ids"]) == [1, 2] and body["source"] == "queue"
    page.close()


def test_new_research_reuses_the_empty_one(browser):
    page = _open(browser)
    page.get_by_role("tab", name="Исследовать").click()
    expect(page.get_by_role("heading", name="Что проверить?")).to_be_visible()
    page.get_by_role("button", name="Новое исследование", exact=True).click()
    page.wait_for_timeout(300)
    assert not _calls(page, r"/workspace$", "POST")
    expect(page.get_by_label("Сообщение аналитику")).to_be_focused()
    page.close()


def test_phone_opens_record_at_the_card_not_at_page_top(browser):
    page = _open(browser, width=390)
    page.locator(".lp-c").nth(1).click()
    page.wait_for_timeout(200)
    assert page.evaluate("window.scrollY") > 300
    expect(page.locator(".lp-rd .lp-rd-title")).to_be_focused()
    top = page.locator(".lp-rd-head").bounding_box()["y"]
    assert top < 200
    page.close()
