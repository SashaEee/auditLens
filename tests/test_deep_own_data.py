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


def test_sections_written_in_parallel_but_streamed_in_order(monkeypatch):
    import asyncio
    import time

    runstate.new_run()
    body_keys = [k for k in dossier.WRITING_ORDER if k not in dossier.LEAD]
    monkeypatch.setattr(dossier, "facts_for", lambda key, reg, plan: [object()])
    monkeypatch.setattr(dossier, "section_prompt", lambda key, *a, **kw: key)
    monkeypatch.setattr(dossier, "facts_index", lambda facts, labels: "")
    monkeypatch.setattr(dossier, "render_facts", lambda facts, labels: "")
    monkeypatch.setenv("GPTR_SECTION_CONCURRENCY", "8")

    async def fake_stream(client, model, prompt):
        await asyncio.sleep(0.3)
        yield f"текст раздела {prompt}. "

    monkeypatch.setattr(dossier, "_stream_section", fake_stream)
    reg = FactRegistry()

    async def run():
        t0 = time.monotonic()
        out = [ev async for ev in dossier.write_dossier(None, "m", question="q", plan=PLAN,
                                                        registry=reg)]
        return out, time.monotonic() - t0

    events, took = asyncio.run(run())
    order = [p for k, p in events if k == "section"]
    assert order == body_keys
    # тело — одновременно (~0,3 с), затем два раздела суждения тоже вместе (~0,3 с)
    assert took < 0.3 * len(body_keys)
    text = "".join(p for k, p in events if k == "chunk")
    for k in body_keys:
        assert f"текст раздела {k}." in text
    assert any(k == "lead" for k, _ in events)


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
    place = od.pages["#market?cat=deposit"]
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
