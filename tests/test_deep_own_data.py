"""Отчёт (deep research): собственные данные AuditLens — жалобы и лазейки.

Без сети и БД: инструменты данных подменяются фикстурами.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bank_audit.ai import agent_tools as T
from bank_audit.research.gptr import dossier, own_data, runstate
from bank_audit.research.gptr.facts import FactRegistry

OVERVIEW = {"bank": "Сбербанк", "as_of": "2026-09-24", "complaints": 2419, "prev_period": 1982,
            "change_pct": 22.1, "share_of_all_bank_complaints_pct": 9.7,
            "rank_by_complaints": 4, "banks_in_corpus": 134, "escalation_pct": 20.7,
            "escalation_filed_pct": 6.9, "market_escalation_pct": 19.8,
            "themes": [{"theme": "chargeback", "label": "Оспаривание операций и возвраты (чарджбэк)",
                        "n": 60, "pct": 2.5, "change_pct": 120, "change_significant": True}],
            "risk_flags": [], "cities": []}
SIGNALS = {"week_end": "2026-09-24",
           "overall": {"week": 145, "norm_per_week": 164.9, "ratio": 0.9, "market_ratio": 1.07},
           "signals": [{"theme": "chargeback", "label": "Оспаривание операций и возвраты (чарджбэк)",
                        "week": 15, "norm_per_week": 3.6, "ratio": 4.2, "market_ratio": 2.09,
                        "market_note": "в 2 раза сильнее рынка", "top_city": "Санкт-Петербург",
                        "top_city_share_pct": 62}]}
THEME = {"label": "Оспаривание операций и возвраты (чарджбэк)", "total": 15,
         "week_end": "2026-09-24",
         "similar_groups": [{"n": 5, "summary": "Отказ в чарджбэке по билетам на отменённый концерт.",
                             "quote": "отказывает в чарджбэке по билетам Канье Уэста",
                             "first": "2026-09-19", "last": "2026-09-23",
                             "cities": ["Санкт-Петербург"], "escalation_threats": 3}],
         "complaints": [{"date": "2026-09-23", "city": "Санкт-Петербург",
                         "summary": "Клиент требует чарджбэк по билетам.",
                         "quote": "банк отказывает в возврате за концерт",
                         "text": "Купил билеты. Концерт отменили, а банк отказывает в возврате за концерт.",
                         "url": "https://www.banki.ru/services/responses/bank/response/1"}]}
LOOPHOLES = {"stats": {"collected_since": "2026-07-01", "all_banks_total": 289,
                       "all_banks_found_last_30d": 217, "this_bank_tagged_total": 90},
             "records": [{"record_id": 19, "about_bank": True,
                          "title": "Продление грейс-периода кредитной карты Сбера до 150 дней",
                          "why_loophole": "перенос даты платежа удлиняет льготный период",
                          "text": "Описание схемы.", "source": "xvestor.ru",
                          "url": "https://xvestor.ru/x", "published": "2026-07-27",
                          "found": "2026-07-27", "bank_tag": "sberbank"}]}


MARKET_OFFERS = {"category": "deposit", "label": "Вклады", "metric": "Ставка", "metric_unit": "%",
                 "lower_is_better": False, "how_to_compare": None,
                 "by_bank": {
                     "Сбербанк": {"offers_total": 9, "best": [
                         {"bank_name": "Сбербанк", "title": "Выгодный старт +", "segment": "mass",
                          "rate_pct": 19.0, "term_months_min": 3, "term_months_max": 3,
                          "valid_from": "2026-09-05 16:00", "metric_value": 19.0}]},
                     "ВТБ": {"offers_total": 2, "best": [
                         {"bank_name": "ВТБ", "title": "ВТБ Вклад", "segment": "mass",
                          "rate_pct": 13.7, "term_months_min": 18, "term_months_max": 18,
                          "valid_from": "2026-09-22 05:00", "metric_value": 13.7}]}}}
MARKET_TOP = {"category": "deposit", "label": "Вклады", "metric": "Ставка", "metric_unit": "%",
              "lower_is_better": False, "offers_total": 200,
              "top": [{"bank_name": "Яндекс Банк", "title": "Вклад", "segment": "mass",
                       "term_months_min": 6, "term_months_max": 6, "metric_value": 16.0},
                      {"bank_name": "Сбербанк", "title": "Выгодный старт +", "segment": "mass",
                       "term_months_min": 3, "term_months_max": 3, "metric_value": 19.0}]}
MARKET_POSITION = {"as_of": "2026-09-25 05:56", "cells": [
    {"category": "deposit", "label": "Вклады", "rank": 1, "n_banks": 134, "metric_label": "Ставка",
     "metric_unit": "%", "title": "Выгодный старт +", "value": 19.0, "gap_median": 5.5,
     "gap_leader": 0.0, "gap_unit": " п.п.", "degenerate": False,
     "comparable": [{"segment": "pension", "n_banks": 33, "rank": 14, "value": 12.65,
                     "title": "Пенсионный Максимум", "median": 12.5, "leader": 15.5}]}],
    "method_caveats": ["у 23 предложений в «вклады» метрика не заполнена — они вне сравнения",
                       "в «ипотека» льготные программы исключены"]}
TREND = {"series": [{"ym": "2026-06", "n": 10, "partial": False, "spike": False},
                    {"ym": "2026-07", "n": 11, "partial": False, "spike": False},
                    {"ym": "2026-08", "n": 26, "partial": False, "spike": True,
                     "pct_vs_median": 136},
                    {"ym": "2026-09", "n": 8, "partial": True}], "baseline": 11.0}


@pytest.fixture
def fake_tools(monkeypatch):
    data = {"complaints_overview": OVERVIEW, "complaint_signals": SIGNALS,
            "complaint_theme": THEME, "loopholes": LOOPHOLES,
            "complaint_search": {"complaints": []},
            "market_offers": MARKET_OFFERS, "market_position": MARKET_POSITION}
    calls = []

    def spec(name):
        def fn(**kw):
            calls.append((name, kw))
            if name == "market_offers" and not kw.get("banks"):
                return json.dumps(MARKET_TOP, ensure_ascii=False)
            return json.dumps(data[name], ensure_ascii=False)
        return T.ToolSpec(name, name, fn, "")
    monkeypatch.setattr(T, "BY_NAME", {n: spec(n) for n in data})
    monkeypatch.setattr(T, "_rd", lambda: SimpleNamespace(trend=lambda *a, **k: TREND))
    runstate.new_run()
    return calls


PLAN = SimpleNamespace(subjects=["sberbank"], subject_labels={"sberbank": "Сбербанк"},
                       anchor="sberbank", product="чарджбэк", question_nature="quality",
                       intent_summary="")


def test_complaints_become_verifiable_pages(fake_tools):
    od = own_data.OwnData()
    scope = {"product": None, "themes": [], "days": 7, "relevant": True}
    own_data.collect_complaints(od, PLAN, scope, "Почему на этой неделе выросли жалобы на чарджбэк?")
    # каждый факт опирается на подстроку своей страницы
    assert od.facts
    for f in od.facts:
        assert f["verbatim"] in od.pages[f["url"]]
    text = "\n".join(od.pages.values())
    assert "15 жалоб при норме 3,6" in text and "×4,2" in text
    assert "Группа похожих жалоб" in text and "Канье Уэста" in text
    assert "2 419" in text and "20,7%" in text
    kinds = {m["kind"] for m in od.meta.values()}
    assert kinds == {"complaints", "review"}
    assert od.complaints == 1
    # тема сигнала разобрана за неделю: выборка сигнала, а не лента за квартал
    assert ("complaint_theme", {"theme": "chargeback", "bank": "Сбербанк", "product": None,
                                "days": 7}) in fake_tools


def test_fact_rejects_quote_not_on_page():
    od = own_data.OwnData()
    od.page("#x", "Заголовок", ["Жалоб за неделю: 15."], "complaints")
    od.fact(subject="s", attribute="a", value="15", verbatim="Жалоб за неделю: 16.", url="#x")
    od.fact(subject="s", attribute="a", value="15", verbatim="Жалоб за неделю: 15.", url="#x")
    assert [f["verbatim"] for f in od.facts] == ["Жалоб за неделю: 15."]


def test_loopholes_section_facts(fake_tools):
    od = own_data.OwnData()
    own_data.collect_loopholes(od, PLAN, "лазейки по кредитным картам")
    assert all(f["stance"] == "loophole" for f in od.facts)
    assert any("Продление грейс-периода" in f["value"] for f in od.facts)
    assert "Все оценки предварительные" in od.pages["#loophole"]
    for f in od.facts:
        assert f["verbatim"] in od.pages[f["url"]]


def test_dossier_voice_puts_analytics_first_and_loopholes_apart(fake_tools):
    od = own_data.OwnData()
    own_data.collect_complaints(od, PLAN, {"product": None, "themes": [], "days": 7,
                                           "relevant": True}, "всплеск чарджбэка")
    own_data.collect_loopholes(od, PLAN, "кредитные карты")
    state = runstate.current()
    state.own_meta.update(od.meta)
    reg = FactRegistry()
    reg.add(subject="sberbank", attribute="мнение", value="отзыв из веба", unit="",
            verbatim="длинная цитата из веба для проверки", url="https://example.com/r",
            stance="observed")
    for kw in od.facts:
        reg.add(**kw)
    voice = dossier.facts_for("voice", reg, PLAN)
    assert state.own_meta[voice[0].url]["kind"] == "complaints"
    assert voice[-1].url == "https://example.com/r"
    loops = dossier.facts_for("loopholes", reg, PLAN)
    assert loops and all(f.stance == "loophole" for f in loops)
    assert not any(f.stance == "loophole" for f in voice)
    rendered = dossier.render_facts(voice[:1], {"sberbank": "Сбербанк"})
    assert "аналитика жалоб AuditLens" in rendered
    assert "Лазейки и уязвимости" in dossier.outline(PLAN, reg)


def test_sources_ui_marks_auditlens_pages():
    from bank_audit.research.gptr.stream import _sources_ui
    pages = {"#reviews?theme=chargeback": "AuditLens · Отзывы: Сбербанк\nЖалоб: 15."}
    own = {"#reviews?theme=chargeback": {"title": "AuditLens · Отзывы: Сбербанк — тема",
                                         "kind": "complaints"}}
    src = _sources_ui(list(pages), pages, {}, {}, own)
    assert src[0]["domain"] == "AuditLens" and src[0]["source_kind"] == "auditlens"
    assert src[0]["trust_score"] == 0.95


def _fake_writer(monkeypatch, brief=None, delay=0.3):
    """Писатель без сети: раздел отвечает своим ключом, промпты копятся."""
    import asyncio
    prompts = {}
    monkeypatch.setattr(dossier, "facts_for", lambda key, reg, plan, own=None: [object()])
    monkeypatch.setattr(dossier, "render_facts", lambda facts, labels, own=None: "")
    monkeypatch.setattr(dossier.al_brief, "facts_digest", lambda *a, **k: "")

    def fake_prompt(key, *a, **kw):
        prompts[key] = kw
        return key
    monkeypatch.setattr(dossier, "section_prompt", fake_prompt)

    async def fake_brief(*a, **kw):
        return brief
    monkeypatch.setattr(dossier.al_brief, "make_brief", fake_brief)

    async def fake_stream(client, model, prompt):
        await asyncio.sleep(delay)
        yield f"текст раздела {prompt}.\n## Лишний заголовок\nещё. "
    monkeypatch.setattr(dossier, "_stream_section", fake_stream)
    return prompts


def _run_dossier(state=None):
    import asyncio
    import time

    async def run():
        t0 = time.monotonic()
        out = [ev async for ev in dossier.write_dossier(
            None, "m", question="q", plan=PLAN, registry=FactRegistry(), state=state,
            brief_model="b")]
        return out, time.monotonic() - t0
    return asyncio.run(run())


def test_sections_lead_written_together_in_brief_order(monkeypatch):
    from bank_audit.research.gptr.brief import Brief
    brief = Brief(answer="Всплеск — билеты на концерт [f:1].", theses=["t"],
                  sections=[{"key": "voice", "title": "Что стоит за всплеском", "focus": "f"},
                            {"key": "conditions", "title": "Правила банка", "focus": "f"}])
    prompts = _fake_writer(monkeypatch, brief)
    runstate.new_run()
    events, took = _run_dossier()
    order = [p for k, p in events if k == "section"]
    # порядок и состав — из брифа; лазейки — только если их выбрал бриф
    assert order == ["voice", "conditions"]
    outline = next(p for k, p in events if k == "outline")
    assert outline == ["Резюме для руководителя проверки", "Что проверять",
                       "Что стоит за всплеском", "Правила банка"]
    # тело — одновременно (~0,3 с), затем резюме и план вместе по готовому телу (~0,3 с)
    assert took < 0.3 * 4
    lead_kw = [prompts[k] for k in ("summary", "checks")]
    assert all("текст раздела voice" in kw["prior_text"] for kw in lead_kw)
    text = "".join(p for k, p in events if k == "chunk")
    assert "## Что стоит за всплеском" in text and "### Лишний заголовок" in text
    assert "\n## Лишний" not in text
    lead = next(p for k, p in events if k == "lead")
    assert "текст раздела summary" in lead and "### Лишний заголовок" in lead
    assert all(kw.get("brief") is brief for kw in prompts.values())


def test_without_brief_default_order_by_question_type(monkeypatch):
    _fake_writer(monkeypatch, None, delay=0.01)
    state = runstate.new_run()
    state.own_scope = {"complaints": True, "market": False}
    events, _ = _run_dossier(state)
    order = [p for k, p in events if k == "section"]
    assert order[0] == "voice" and "conflicts" in order


def test_voice_sees_own_data_when_each_step_runs_in_empty_context(fake_tools, monkeypatch):
    """Как в интерфейсе: обёртка потока исполняет каждый шаг отдельной задачей
    с пустым контекстом. Раньше раздел «Голос клиента» получал пустое состояние
    и писал «данных аналитики жалоб нет» при 14 жалобах в реестре."""
    import asyncio
    import contextvars
    od = own_data.OwnData()
    own_data.collect_complaints(od, PLAN, {"product": None, "themes": [], "days": 7},
                                "всплеск чарджбэка")
    state = runstate.current()
    state.own_meta.update(od.meta)
    reg = FactRegistry()
    for i in range(20):      # веб-факты с пустой датой вытесняли свои по лимиту
        reg.add(subject="sberbank", attribute="мнение", value=f"веб {i}", unit="",
                verbatim=f"длинная цитата из веба номер {i}", url=f"https://e.com/{i}",
                stance="observed")
    for kw in od.facts:
        reg.add(**kw)
    seen = {}

    def fake_prompt(key, plan, question, labels, *, facts_text, **kw):
        seen[key] = facts_text
        return key
    monkeypatch.setattr(dossier, "section_prompt", fake_prompt)

    async def no_brief(*a, **kw):
        return None
    monkeypatch.setattr(dossier.al_brief, "make_brief", no_brief)

    async def fake_stream(client, model, prompt):
        yield "текст. "
    monkeypatch.setattr(dossier, "_stream_section", fake_stream)

    async def drive():
        agen = dossier.write_dossier(None, "m", question="q", plan=PLAN, registry=reg,
                                     state=state, brief_model="b")
        while True:
            try:
                await asyncio.get_running_loop().create_task(
                    agen.__anext__(), context=contextvars.Context())
            except StopAsyncIteration:
                return
    asyncio.run(asyncio.wait_for(drive(), 10))
    assert "аналитика жалоб AuditLens" in seen["voice"]
    assert "15 жалоб при норме 3,6" in seen["voice"]


def test_brief_parse_keeps_known_sections_once():
    from bank_audit.research.gptr import brief as B
    raw = json.dumps({"answer": "Ответ [f:1].", "theses": ["a", "b"],
                      "sections": [{"key": "voice", "title": "«Что стоит за всплеском»",
                                    "focus": "сюжеты"},
                                   {"key": "voice", "title": "дубль"},
                                   {"key": "market", "title": "Сбер против рынка"},
                                   {"key": "nonsense", "title": "x"}]}, ensure_ascii=False)
    b = B.parse("```json\n" + raw + "\n```", {"voice": 5, "conditions": 3, "market": 0})
    assert [s["key"] for s in b.sections] == ["voice", "market"]
    assert b.sections[0]["title"] == "Что стоит за всплеском"
    assert b.skipped == ["conditions"] and b.ok()
    rendered = b.render("voice")
    assert "ГЛАВНЫЙ ОТВЕТ" in rendered and "← ТВОЙ РАЗДЕЛ" in rendered
    assert B.default_order(SimpleNamespace(question_nature="regulatory"), {})[0] == "regulatory"
    assert B.default_order(SimpleNamespace(question_nature="mixed"),
                           {"market": True})[0] == "market"
    assert B.default_order(SimpleNamespace(question_nature="mixed"),
                           {"loopholes": True})[0] == "loopholes"


def test_followup_reads_pages_for_entities_from_complaints(fake_tools, monkeypatch):
    import asyncio
    from bank_audit.research.gptr import followup
    od = own_data.OwnData()
    own_data.collect_complaints(od, PLAN, {"product": None, "themes": [], "days": 7},
                                "всплеск чарджбэка")
    text = followup.material(od.pages, od.meta)
    assert "Группа похожих" in text and "Концерт отменили" in text

    async def fake_queries(client, model, question, text_):
        return ["концерт Канье Уэста Газпром Арена отмена"]
    monkeypatch.setattr(followup, "queries_for", fake_queries)
    import bank_audit.rag.web_search as ws
    monkeypatch.setattr(ws, "search", lambda q, **kw: [
        {"url": "https://news.example/arena"},
        {"url": "https://www.banki.ru/services/responses/bank/response/1"}])
    import bank_audit.research.gptr.scraper as sc

    class FakeScraper:
        def __init__(self, url, state=None):
            self.url, self.state = url, state

        def scrape(self):
            self.state.note_page(self.url, "Арена заявила, что договор аренды не заключён.")
            return "Арена заявила, что договор аренды не заключён.", [], "t"
    monkeypatch.setattr(sc, "AuditLensScraper", FakeScraper)
    state = runstate.current()
    pages = asyncio.run(followup.collect(None, "m", "q", od, state=state))
    assert list(pages) == ["https://news.example/arena"]      # отзывы повторно не ищем


def test_pdf_sources_show_auditlens_slice_as_text():
    from bank_audit.web.pdf_export import _render_sources_section
    html = _render_sources_section([
        {"n": 1, "url": "#reviews?theme=chargeback&days=7", "title": "AuditLens · Отзывы",
         "source_kind": "auditlens", "trust_score": 0.95},
        {"n": 2, "url": "https://cbr.ru/x", "title": "ЦБ", "source_kind": "regulator",
         "trust_score": 0.98}])
    assert "AuditLens, срез вкладки: #reviews?theme=chargeback&amp;days=7" in html
    assert '<a href="https://cbr.ru/x">' in html and "Данные AuditLens" in html


def test_scope_drops_product_not_named_in_question():
    plan = SimpleNamespace(question_nature="quality")
    q = "Почему на этой неделе выросли жалобы на чарджбэк в Сбере?"
    sc = own_data.normalize_scope({"product": "Дебетовая карта", "themes": ["chargeback"],
                                   "days": 7, "complaints": True}, q, plan)
    assert sc["product"] is None and sc["themes"] == ["chargeback"] and sc["days"] == 7
    sc = own_data.normalize_scope({"product": "Вклад", "days": 90},
                                  "Жалобы клиентов на вклады за 90 дней", plan)
    assert sc["product"] == "Вклад"
    assert own_data.product_named("Кредитная карта", "лазейки по кредитным картам")
    assert not own_data.product_named("Дебетовая карта", "лазейки по кредитным картам")
    # рынок — только с категорией из перечня; регулирование — без лазеек
    assert not own_data.normalize_scope({"market": True}, "q", plan)["market"]
    assert own_data.normalize_scope({"market": True, "category": "deposit"}, "q", plan)["market"]
    reg = own_data.normalize_scope({"loopholes": True}, "q",
                                   SimpleNamespace(question_nature="regulatory"))
    assert not reg["loopholes"]


def test_summary_page_has_monthly_trend_and_banks(fake_tools, monkeypatch):
    ov = dict(OVERVIEW, banks_by_complaints=[
        {"bank": "Альфа-Банк", "n": 76, "pct": 14.4}, {"bank": "Сбербанк", "n": 35, "pct": 6.6}])
    tools = dict(T.BY_NAME)
    tools["complaints_overview"] = T.ToolSpec("complaints_overview", "", lambda **kw: json.dumps(
        ov, ensure_ascii=False), "")
    monkeypatch.setattr(T, "BY_NAME", tools)
    od = own_data.OwnData()
    own_data.collect_complaints(od, PLAN, {"product": "Вклад", "themes": [], "days": 90},
                                "жалобы на вклады: динамика")
    text = "\n".join(od.pages.values())
    assert "Жалобы по месяцам (по дате отзыва): июн 2026 — 10; июл 2026 — 11; авг 2026 — 26" in text
    assert "сен 2026 — 8 (месяц не завершён)" in text and "авг 2026 (+136% к медиане)" in text
    assert "Альфа-Банк — 76 (14,4%); Сбербанк — 35 (6,6%)" in text
    # темы не заданы — крупнейшие темы среза разобраны лентой, поиск для Сбера не нужен
    assert ("complaint_theme", {"theme": "chargeback", "bank": "Сбербанк", "product": "Вклад",
                                "days": 90}) in fake_tools
    assert not any(n == "complaint_search" for n, _ in fake_tools)
    for f in od.facts:
        assert f["verbatim"] in od.pages[f["url"]]


def test_market_pages_are_facts_with_right_banks(fake_tools):
    plan = SimpleNamespace(subjects=["sberbank", "vtb"],
                           subject_labels={"sberbank": "Сбербанк", "vtb": "ВТБ"},
                           anchor="sberbank", product="вклады")
    od = own_data.OwnData()
    own_data.collect_market(od, plan, {"category": "deposit", "market": True})
    assert ("market_offers", {"category": "deposit", "banks": ["ВТБ"]}) in fake_tools
    by_subj = {f["subject"]: f for f in od.facts if "Ставка" in f["attribute"]}
    assert by_subj["vtb"]["value"] == "13,7" and by_subj["sberbank"]["value"] == "19"
    assert "срок 3 мес." in by_subj["sberbank"]["verbatim"]
    assert all(f["stance"] == "declared" and f["verbatim"] in od.pages[f["url"]]
               for f in od.facts)
    place = od.pages["#market?cat=deposit&view=position"]
    assert "1-е из 134 банков" in place and "Сегмент «Пенсионные»: 14-е из 33" in place
    assert "вклады" in place and "ипотека" not in place
    assert {m["kind"] for m in od.meta.values()} == {"market"}
    # без флага рынка — ничего
    od2 = own_data.OwnData()
    own_data.collect_market(od2, plan, {"category": "deposit", "market": False})
    assert not od2.pages


def test_report_stream_hides_theme_keys_but_not_links():
    from bank_audit.ai.hermes_quick import PlainKeysStream
    ks = PlainKeysStream()
    parts = ["Всплеск по теме charge", "back: 15 жалоб. См. [срез](#reviews?th",
             "eme=chargeback) и wrong_", "debit."]
    out = "".join(ks.feed(p) for p in parts) + ks.finish()
    assert "chargeback:" not in out and "(#reviews?theme=chargeback)" in out
    assert "wrong_debit" not in out and out.startswith("Всплеск по теме ")


def test_demote_keeps_anchors_and_cuts_anchor_notes():
    t = dossier._demote_text("## Заг\nтекст [f:158 — в теле] и [f:12][f:3] конец\n# Итог")
    assert t == "### Заг\nтекст [f:158] и [f:12][f:3] конец\n### Итог"


def test_pdf_ordered_list_keeps_numbers_across_blank_lines():
    from bank_audit.web.pdf_export import _md_to_html
    html = _md_to_html("1. один\n\n2. два\n\n3. три", {})
    assert '<ol start="2">' in html and '<ol start="3">' in html


def test_brief_drops_titles_that_duplicate_lead_sections():
    from bank_audit.research.gptr import brief as B
    raw = json.dumps({"answer": "a", "sections": [
        {"key": "conditions", "title": "Что проверить в процессе Сбера", "focus": "f"}]},
        ensure_ascii=False)
    b = B.parse(raw, {"conditions": 3})
    assert b.sections[0]["title"] == ""


def test_market_always_has_leaders_and_themes_have_list_boundary(fake_tools):
    plan = SimpleNamespace(subjects=["sberbank", "vtb"],
                           subject_labels={"sberbank": "Сбербанк", "vtb": "ВТБ"},
                           anchor="sberbank", product="вклады")
    od = own_data.OwnData()
    own_data.collect_market(od, plan, {"category": "deposit", "market": True})
    leaders = od.pages["#market?cat=deposit"]
    assert "лидеры рынка" in leaders and "Яндекс Банк" in leaders
    assert any(f["subject"] == "Яндекс Банк" for f in od.facts)   # банк вне плана — по имени
    od2 = own_data.OwnData()
    own_data.collect_complaints(od2, PLAN, {"product": None, "themes": [], "days": 90}, "жалобы")
    text = "\n".join(od2.pages.values())
    assert "у тем вне списка — не больше 60 жалоб каждая" in text


def test_market_section_puts_bank_sources_before_vitrine():
    reg = FactRegistry()
    own = {"#market?cat=deposit": {"kind": "market"}}
    reg.add(subject="vtb", attribute="Ставка (витрина «Рынок»)", value="13,7", unit="%",
            verbatim="ВТБ, «ВТБ Вклад»: Ставка 13,7%", url="#market?cat=deposit",
            stance="declared")
    reg.add(subject="vtb", attribute="ставка", value="до 19", unit="%",
            verbatim="Краткосрочный вклад до 19% годовых", url="https://www.vtb.ru/x",
            stance="declared")
    got = dossier.facts_for("market", reg, PLAN, own)
    assert [f.url for f in got] == ["https://www.vtb.ru/x", "#market?cat=deposit"]


def test_common_prompt_has_comparability_rules():
    text = dossier._common(PLAN, "q", {"sberbank": "Сбербанк"})
    assert "СОПОСТАВИМОСТЬ" in text and "НЕ НАЙДЕНО ≠ НЕТ" in text
    assert "ГИПОТЕЗА — НЕ ПРИГОВОР" in text and "ТОЧКА ОТСЧЁТА" not in text


def test_voice_is_kept_when_brief_drops_it(monkeypatch):
    from bank_audit.research.gptr.brief import Brief
    brief = Brief(answer="a", sections=[{"key": "market", "title": "Сравнение", "focus": "f"}])
    _fake_writer(monkeypatch, brief, delay=0.01)
    own = {"#reviews": {"kind": "complaints"}}
    state = runstate.new_run()
    state.own_meta.update(own)
    monkeypatch.setattr(dossier, "facts_for", lambda key, reg, plan, own=None: [
        SimpleNamespace(url="#reviews")])
    plan2 = SimpleNamespace(**{**vars(PLAN), "subjects": ["sberbank", "vtb"]})
    import asyncio

    async def run():
        return [ev async for ev in dossier.write_dossier(
            None, "m", question="q", plan=plan2, registry=FactRegistry(), state=state,
            brief_model="b")]
    events = asyncio.run(run())
    order = [p for k, p in events if k == "section"]
    assert order[:2] == ["market", "voice"] and "loopholes" not in order


def test_scope_keeps_complaints_and_loopholes_for_comparisons():
    plan = SimpleNamespace(question_nature="tariff_product")
    sc = own_data.normalize_scope({"complaints": False, "loopholes": False, "market": True,
                                   "category": "deposit"}, "Сравни вклады Сбера и ВТБ", plan)
    assert sc["complaints"] and sc["loopholes"]
    reg = own_data.normalize_scope({"complaints": False}, "q",
                                   SimpleNamespace(question_nature="regulatory"))
    assert not reg["loopholes"]


def test_prompts_know_today_to_tell_current_from_future_rules():
    from bank_audit.clock import today_msk
    text = dossier._common(PLAN, "q", {"sberbank": "Сбербанк"})
    assert f"СЕГОДНЯ {today_msk().strftime('%d.%m.%Y')}" in text and "ДЕЙСТВУЕТ ИЛИ БУДЕТ" in text
