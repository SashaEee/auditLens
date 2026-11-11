"""Отчёт под задачу аудитора: сравнение — только когда его просят, жалобы — по
предмету вопроса, «Что проверять» — по задаче, база знаний — источник отчёта.

Без сети и БД: модель и инструменты данных подменяются.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from bank_audit.ai import agent_tools as T
from bank_audit.research.gptr import brief as B
from bank_audit.research.gptr import dossier as D
from bank_audit.research.gptr import facts as F
from bank_audit.research.gptr import own_data, planner, runstate
from bank_audit.research.v2 import conductor as C

LABELS = {"sberbank": "Сбербанк", "vtb": "ВТБ", "alfabank": "Альфа-Банк", "tinkoff": "Т-Банк",
          "cian": "Циан", "domclick": "Домклик"}


def plan(**kw):
    base = dict(intent="x", intent_summary="", question_nature="mixed", subjects=[],
                subject_labels={}, product="")
    base.update(kw)
    base["subject_labels"] = base["subject_labels"] or {s: LABELS.get(s, s) for s in base["subjects"]}
    return C.ResearchPlan(**base)


# ── рамка: непрошеные конкуренты ─────────────────────────────────────────────

@pytest.mark.parametrize("question, model_plan, named, task, subjects, checks", [
    ("Сервис Свое Дело для самозанятых в Сбере",
     dict(task="compare", anchor="sberbank", subjects=["sberbank", "tinkoff", "alfabank", "vtb"]),
     [], "product", ["sberbank"], "short"),
    ("жалобы самозанятых",
     dict(task="complaints", anchor="sberbank", subjects=["sberbank", "tinkoff", "vtb"]),
     [], "complaints", ["sberbank"], "full"),
    ("Сравни условия кредитных карт Сбера и Т-Банка",
     dict(task="compare", anchor="sberbank", subjects=["sberbank", "tinkoff"]),
     ["sberbank", "tinkoff"], "compare", ["sberbank", "tinkoff"], "short"),
    ("Сравни вклады Сбера с конкурентами",
     dict(task="compare", scope="market", anchor="sberbank", subjects=["sberbank", "vtb", "alfabank"]),
     ["sberbank"], "compare", ["sberbank", "vtb", "alfabank"], "short"),
    ("Сравни сервисы аренды у Домклик и Циан",
     dict(task="compare", anchor="domclick", subjects=["domclick", "cian"]),
     [], "compare", ["domclick", "cian"], "short"),
    ("Жалобы на ипотеку в Сбере и ВТБ",
     dict(task="complaints", anchor="sberbank", subjects=["sberbank", "vtb", "alfabank"]),
     ["sberbank", "vtb"], "complaints", ["sberbank", "vtb"], "full"),
    ("Что проверить в процессе выдачи кредитных карт Сбера",
     dict(task="product", anchor="sberbank", subjects=["sberbank"]),
     ["sberbank"], "product", ["sberbank"], "full"),
])
def test_scope_keeps_only_asked_organisations(question, model_plan, named, task, subjects, checks):
    p = C.enforce_scope(plan(**model_plan), question, named)
    assert (p.task, p.subjects, p.checks) == (task, subjects, checks)
    assert set(p.subject_labels) == set(subjects)


def test_reference_question_gets_no_banks_and_no_checks():
    p = C.enforce_scope(plan(task="explain", anchor="", subjects=["sberbank", "vtb", "alfabank"]),
                        "Расскажи об истории появления кредитной карты в СССР и РФ", [])
    assert p.subjects == [] and p.scope == "none" and p.checks == "none"
    assert C.open_subjects(p)


def test_complaints_question_without_a_bank_is_about_sber():
    p = C.enforce_scope(plan(task="complaints", anchor="", subjects=[]), "жалобы самозанятых", [])
    assert p.subjects == ["sberbank"] and p.anchor == "sberbank" and p.scope == "sber"
    assert p.subject_labels == {"sberbank": "Сбербанк"}


def test_deliverable_is_read_from_the_question():
    p = C.enforce_scope(plan(task="product", anchor="sberbank", subjects=["sberbank"]),
                        "Сделай презентацию по драгметаллам в Сбере", ["sberbank"])
    assert p.deliverable == "deck"
    p = C.enforce_scope(plan(task="market_data"), "Построй диаграмму долей рынка ПДС", [])
    assert p.deliverable == "chart"


def test_fallback_plan_does_not_invent_four_banks():
    p = C._fallback_plan("история кредитной карты", [])
    assert p.subjects == [] and p.task == "explain"
    p = C._fallback_plan("Сравни вклады Сбера и ВТБ", ["sberbank", "vtb"])
    assert p.subjects == ["sberbank", "vtb"] and p.task == "compare"


def test_new_plan_fields_are_parsed_and_sanitised():
    data = {"intent": "x", "task": "market_data", "scope": "none", "subjects": [],
            "topics": ["доля рынка", " объём  средств "], "observed": "", "regulatory": "",
            "queries": [{"q": "статистика металлические счета", "site": "https://www.cbr.ru/statistics/"},
                        "рынок ОМС доли банков 2026", {"q": "x"}, {"q": "плохой сайт", "site": "не домен"}],
            "wants_checks": True, "period": "за 2026 год"}
    p = C._build_plan_from_dict(data, "q")
    assert p.queries == [{"q": "статистика металлические счета", "site": "cbr.ru"},
                         {"q": "рынок ОМС доли банков 2026", "site": ""},
                         {"q": "плохой сайт", "site": ""}]
    assert p.topics == ["доля рынка", "объём средств"] and p.checks == "full"
    assert p.missions == [] and p.to_ui_plan()[0]["tool"] == "own"


def test_site_operator_inside_a_query_moves_to_its_field():
    p = C._build_plan_from_dict({"queries": [{"q": "site:cbr.ru статистика ОМС банки", "site": ""}]}, "q")
    assert p.queries == [{"q": "статистика ОМС банки", "site": "cbr.ru"}]


def test_planner_falls_back_to_second_model_before_keyword_plan(monkeypatch):
    calls = []

    class Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    calls.append(kw["model"])
                    if kw["model"] == "первая":
                        raise RuntimeError("Error code: 403 - model is not available")
                    msg = SimpleNamespace(content=json.dumps({
                        "intent": "x", "task": "explain", "scope": "none", "subjects": [],
                        "topics": ["история появления"], "queries": [{"q": "история кредитных карт"}]}))
                    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    monkeypatch.setenv("GPTR_PLAN_FALLBACK", "вторая")
    p = asyncio.run(C.plan_research(Client(), "первая", "история кредитной карты"))
    assert calls == ["первая", "вторая"]
    assert p.task == "explain" and p.queries and p.intent == "x"


# ── поиск и список того, что собрать ─────────────────────────────────────────

def test_search_uses_planner_queries_and_skips_unreadable_law_portal():
    p = plan(task="market_data", scope="none", product="обезличенные металлические счета",
             topics=["доля банка на рынке", "объём средств", "динамика"], regulatory="требования к ОМС",
             queries=[{"q": "статистика металлические счета банки", "site": "cbr.ru"},
                      {"q": "рынок ОМС доли банков 2026", "site": ""}])
    contract = asyncio.run(F.plan_attributes(None, "m", "Доля рынка по ОМС", p))
    assert list(contract) == ["доля банка на рынке", "объём средств", "динамика", "требования к ОМС"]
    assert contract.observed == ""          # справке взгляд клиентов не навязывается
    qs, _ = planner.plan_to_subqueries(p, "Доля рынка по ОМС", attributes=contract)
    assert qs[0] == "статистика металлические счета банки site:cbr.ru"
    assert "рынок ОМС доли банков 2026" in qs
    assert not any("pravo.gov.ru" in q for q in qs)
    assert any(q.endswith("site:consultant.ru") for q in qs)


def test_open_list_keeps_organisation_outside_the_plan(monkeypatch):
    async def fake_extract(client, model, *, url, text, attributes, subjects, labels, open_list=False, **kw):
        assert open_list
        return [{"subject": "ВТБ", "attribute": "доля рынка", "value": "18", "unit": "%",
                 "verbatim": "доля ВТБ составила 18%"},
                {"subject": "Сбербанк", "attribute": "доля рынка", "value": "41", "unit": "%",
                 "verbatim": "доля Сбербанка составила 41%"}]
    monkeypatch.setattr(F, "extract_page", fake_extract)
    reg = F.FactRegistry()
    p = plan(task="market_data", scope="sber", subjects=["sberbank"])
    asyncio.run(F.extract_into(reg, None, "m", pages={"https://x.ru/a": "текст"},
                               attributes=["доля рынка"], plan=p))
    assert [f.subject for f in reg.facts] == ["ВТБ", "sberbank"]


def test_closed_list_still_blanks_unknown_organisation(monkeypatch):
    async def fake_extract(client, model, *, url, text, attributes, subjects, labels, open_list=False, **kw):
        return [{"subject": "ВТБ", "attribute": "ставка", "value": "1", "unit": "%",
                 "verbatim": "ставка ВТБ один процент"}]
    monkeypatch.setattr(F, "extract_page", fake_extract)
    reg = F.FactRegistry()
    p = plan(task="product", scope="sber", subjects=["sberbank"])
    asyncio.run(F.extract_into(reg, None, "m", pages={"https://x.ru/a": "т"},
                               attributes=["ставка"], plan=p))
    assert [f.subject for f in reg.facts] == [""]


def test_extractor_is_told_the_subject_of_the_question(monkeypatch):
    seen = {}

    async def fake_call(client, model, kw):
        seen["system"] = kw["messages"][0]["content"]
        seen["user"] = kw["messages"][1]["content"]
        msg = SimpleNamespace(content='{"facts": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    monkeypatch.setattr(F, "call_model", fake_call)
    p = plan(task="market_data", product="обезличенные металлические счета",
             product_synonyms=["ОМС", "металлические счета"],
             intent_summary="Аудитору нужны доли банков на рынке металлических счетов.")
    asyncio.run(F.extract_into(F.FactRegistry(), None, "m", pages={"https://cbr.ru/a": "текст страницы"},
                               attributes=["объём средств"], plan=p))
    assert "# Предмет вопроса\nобезличенные металлические счета (иначе: ОМС, металлические счета)" in seen["user"]
    assert "Только о ПРЕДМЕТЕ ВОПРОСА" in seen["system"] and "Список объектов ОТКРЫТ" in seen["system"]


def test_brief_index_shows_what_the_number_is_about():
    f = F.Fact(id=7, subject="", attribute="объём средств на металлических счетах", value="2,4 трлн",
               unit="руб.", verbatim="Резервы по страхованию жизни на конец 2к26 составили 2,4 трлн руб.",
               url="https://cbr.ru/x", stance="regulatory")
    line = B._index_line(f, {}, {})
    assert "«Резервы по страхованию жизни" in line and line.startswith("[f:7] общее | объём средств")


# ── собственные данные ───────────────────────────────────────────────────────

@pytest.mark.parametrize("task, question, complaints, loopholes", [
    ("explain", "Расскажи об истории кредитной карты", False, False),
    ("market_data", "Доля рынка по ОМС", False, False),
    ("market_data", "Сколько жалоб на вклады было в 2026 году", True, False),
    ("product", "Сервис Своё дело в Сбере", True, True),
    ("regulation", "Какие требования ЦБ к раскрытию ПСК", False, False),
    ("regulation", "Мошеннические схемы и требования 161-ФЗ", False, True),
])
def test_complaints_and_loopholes_follow_the_task(task, question, complaints, loopholes):
    scope = own_data.normalize_scope({}, question, plan(task=task))
    assert (scope["complaints"], scope["loopholes"]) == (complaints, loopholes)


OVERVIEW = {"bank": "Сбербанк", "as_of": "2026-11-05", "complaints": 2485, "prev_period": 2049,
            "change_pct": 21.3, "themes": [], "risk_flags": [], "cities": []}
ON_TOPIC = {"date": "2026-10-27", "city": "Тюмень", "product": "Бизнес: счёт и РКО",
            "summary": "Клиент не может сформировать чек в сервисе Своё дело",
            "quote": "в сервисе Своё дело чек не формируется третий день",
            "text": "Я самозанятый, в сервисе Своё дело чек не формируется третий день, поддержка молчит.",
            "url": "https://www.banki.ru/services/responses/bank/response/1/"}
OFF_TOPIC = {"date": "2026-10-20", "city": "Псков", "product": "Дебетовая карта",
             "summary": "Заблокировали карту по 115-ФЗ", "quote": "карту заблокировали без объяснений",
             "text": "Мне заблокировали карту без объяснений, требую разблокировать.",
             "url": "https://www.banki.ru/services/responses/bank/response/2/"}


@pytest.fixture
def tools(monkeypatch):
    calls = []
    data = {"complaints_overview": OVERVIEW,
            "complaint_signals": {"signals": [], "overall": {}},
            "complaint_search": {"by_words": 1, "complaints": [ON_TOPIC, OFF_TOPIC],
                                 "link": "#reviews?q=своё+дело"}}

    def spec(name):
        def fn(**kw):
            calls.append((name, kw))
            return json.dumps(data[name], ensure_ascii=False)
        return T.ToolSpec(name, name, fn, "")
    monkeypatch.setattr(T, "BY_NAME", {n: spec(n) for n in data})
    monkeypatch.setattr(T, "_rd", lambda: SimpleNamespace(trend=lambda *a, **k: None))
    runstate.new_run()
    return calls, data


def test_product_outside_the_catalogue_gets_only_on_topic_complaints(tools):
    calls, _ = tools
    p = plan(task="product", scope="sber", anchor="sberbank", subjects=["sberbank"],
             product="сервис «Своё дело» для самозанятых", product_synonyms=["Своё дело"])
    od = own_data.OwnData()
    scope = {"product": None, "themes": [], "days": 90}
    own_data.collect_complaints(od, p, scope, "Сервис Своё дело для самозанятых в Сбере")
    # сводка по всему банку не запрашивалась и в отчёт не попала
    assert not any(name == "complaints_overview" for name, _ in calls)
    text = "\n".join(od.pages.values()).replace("\xa0", " ")
    assert "2 485" not in text and "заблокировали" not in text
    assert "чек не формируется" in text and od.complaints == 1
    assert any(f["attribute"] == "Жалобы: по предмету вопроса" for f in od.facts)
    for f in od.facts:
        assert f["verbatim"] in od.pages[f["url"]]


def test_no_on_topic_complaints_becomes_a_gap_line_not_a_section(tools):
    _, data = tools
    data["complaint_search"] = {"by_words": 0, "complaints": [OFF_TOPIC]}
    p = plan(task="product", scope="sber", anchor="sberbank", subjects=["sberbank"],
             product="обезличенные металлические счета")
    od = own_data.OwnData()
    own_data.collect_complaints(od, p, {"product": None, "themes": [], "days": 90}, "ОМС в Сбере")
    assert not od.pages and not od.facts
    assert od.notes and "не найдено" in od.notes[0] and "обезличенные" in od.notes[0]


def test_question_about_the_bank_as_a_whole_keeps_the_bank_overview(tools):
    calls, _ = tools
    p = plan(task="complaints", scope="sber", anchor="sberbank", subjects=["sberbank"], product="")
    od = own_data.OwnData()
    own_data.collect_complaints(od, p, {"product": None, "themes": [], "days": 90},
                                "Сколько жалоб на Сбербанк за 90 дней")
    assert any(name == "complaints_overview" for name, _ in calls)
    assert "2 485" in "\n".join(od.pages.values()).replace("\xa0", " ")


def test_knowledge_base_documents_become_run_pages(monkeypatch):
    from bank_audit.rag import retriever
    from bank_audit.research.gptr import scraper

    def search(query, **kw):
        assert kw["bank_slugs"] == ["sberbank"]
        return {"groups": [{"document_id": 7, "url": "https://www.sberbank.ru/svoedelo",
                            "title": "Своё дело", "fetched_at": "2026-11-03T10:00:00",
                            "hits": [{"idx": 1}]}]}
    monkeypatch.setattr(retriever, "hybrid_search", search)
    chunks = [{"idx": i, "text": f"Абзац {i} о сервисе Своё дело: условия подключения и комиссия. " * 6}
              for i in range(4)]
    monkeypatch.setattr(T, "_q", lambda sql, params=None: chunks)
    state = runstate.new_run()
    p = plan(task="product", scope="sber", anchor="sberbank", subjects=["sberbank"],
             product="сервис Своё дело")
    od = own_data.OwnData()
    own_data.collect_knowledge(od, p, "Сервис Своё дело", state)
    url = "https://www.sberbank.ru/svoedelo"
    assert od.knowledge == 1 and url in state.pages and state.pages[url].startswith("# Своё дело")
    assert state.kb_meta[url]["loaded"] == "2026-11-03" and state.kb_meta[url]["doc"] == 7
    # скрапер не скачивает страницу заново — отдаёт текст базы знаний
    text, _, title = scraper.AuditLensScraper(url, state=state).scrape()
    assert text == state.pages[url] and title == "Своё дело"


def test_knowledge_document_about_another_subject_is_skipped(monkeypatch):
    from bank_audit.rag import retriever
    monkeypatch.setattr(retriever, "hybrid_search", lambda query, **kw: {"groups": [
        {"document_id": 9, "url": "https://cbr.ru/insurance_review", "title": "Обзор страхования",
         "fetched_at": "2026-11-01", "hits": [{"idx": 0}]}]})
    monkeypatch.setattr(T, "_q", lambda sql, params=None: [
        {"idx": 0, "text": "Резервы по страхованию жизни выросли, взносы по ОСАГО и ДМС увеличились. " * 12}])
    state = runstate.new_run()
    p = plan(task="market_data", scope="none", product="обезличенные металлические счета",
             product_synonyms=["металлические счета"])
    od = own_data.OwnData()
    own_data.collect_knowledge(od, p, "Доля рынка по ОМС", state)
    assert od.knowledge == 0 and not state.preloaded


def test_long_knowledge_document_is_cut_around_the_question(monkeypatch):
    chunks = [{"idx": i, "text": ("Тариф эквайринга для оборота свыше миллиона. " if i == 40
                                  else "Общие положения договора банковского обслуживания. ") * 30}
              for i in range(80)]
    monkeypatch.setattr(T, "_q", lambda sql, params=None: chunks)
    text = own_data._kb_text(1, [10], {"эквайринга", "тариф"}, 6000)
    assert "Тариф эквайринга" in text and "[…]" in text and len(text) <= 6200


# ── досье под задачу ─────────────────────────────────────────────────────────

def fact(i, subj, attr, stance, url="https://sberbank.ru/a", val="значение"):
    return F.Fact(id=i, subject=subj, attribute=attr, value=val, unit="",
                  verbatim=f"дословная цитата номер {i}", url=url, stance=stance)


def test_reference_question_has_plain_summary_and_no_checks():
    p = C.enforce_scope(plan(task="explain"), "история кредитной карты", [])
    assert D.lead_keys(p) == ("summary",)
    assert D.titles(p)["summary"] == "Главное"
    text = D.section_prompt("summary", p, "история кредитной карты", {}, facts_text="")
    assert "РАЗДЕЛ «ГЛАВНОЕ»" in text and "руководитель проверки" not in text
    assert "справочный материал" in text


def test_short_checks_for_product_and_full_for_complaints():
    prod = C.enforce_scope(plan(task="product", anchor="sberbank", subjects=["sberbank"]),
                           "Сервис Своё дело в Сбере", ["sberbank"])
    assert D.lead_keys(prod) == ("summary", "checks")
    assert D.titles(prod)["checks"] == "Что проверить"
    short = D.section_prompt("checks", prod, "q", LABELS, facts_text="")
    assert "три–пять пунктов" in short and "1. Гипотезы" not in short
    assert "Сравнения с конкурентами аудитор не просил" in short
    compl = C.enforce_scope(plan(task="complaints", anchor="sberbank", subjects=["sberbank"]),
                            "жалобы на вклады Сбера", ["sberbank"])
    full = D.section_prompt("checks", compl, "q", LABELS, facts_text="")
    assert "1. Гипотезы" in full


def test_comparison_keeps_own_bank_framing():
    p = C.enforce_scope(plan(task="compare", anchor="sberbank", subjects=["sberbank", "vtb"]),
                        "Сравни вклады Сбера и ВТБ", ["sberbank", "vtb"])
    text = D.section_prompt("market", p, "q", LABELS, facts_text="")
    assert "СВОЙ БАНК — Сбербанк" in text and "аудитор не просил" not in text


def test_third_party_facts_go_to_reference_and_leave_voice():
    reg = F.FactRegistry()
    reg.facts += [fact(1, "", "объём рынка", "observed", "https://frankrg.com/1"),
                  fact(2, "sberbank", "жалобы", "observed", "https://www.banki.ru/services/responses/bank/response/5/"),
                  fact(3, "sberbank", "условия", "declared")]
    p = plan(task="market_data", subjects=["sberbank"], anchor="sberbank")
    ref = D.facts_for("reference", reg, p, {})
    assert [f.id for f in ref] == [1]
    assert [f.id for f in D.facts_for("voice", reg, p, {}, web_in_voice=False)] == [2]
    assert [f.id for f in D.facts_for("voice", reg, p, {})] == [2, 1]


def _write(monkeypatch, p, reg, brief):
    written = {}

    async def fake_brief(*a, **kw):
        return brief
    monkeypatch.setattr(D.al_brief, "make_brief", fake_brief)

    async def fake_stream(client, model, prompt):
        yield "Текст раздела по существу вопроса. "
    monkeypatch.setattr(D, "_stream_section", fake_stream)
    real = D.section_prompt

    def spy(section, *a, **kw):
        written[section] = kw.get("facts_text", "")
        return real(section, *a, **kw)
    monkeypatch.setattr(D, "section_prompt", spy)
    monkeypatch.setattr(D.al_viz, "MIN_FACTS", 10 ** 6)       # без дизайнера
    monkeypatch.setenv("LLM_MODEL_NAME", "m")
    state = runstate.new_run()

    async def run():
        return [ev async for ev in D.write_dossier(None, "m", question="q", plan=p,
                                                   registry=reg, state=state)]
    return asyncio.run(run()), written


def test_product_report_is_built_from_topic_sections_without_market(monkeypatch):
    reg = F.FactRegistry()
    reg.facts += [fact(1, "sberbank", "подключение", "declared"),
                  fact(2, "sberbank", "тарифы и комиссии", "declared"),
                  fact(3, "sberbank", "тарифы и комиссии", "observed", "https://vc.ru/x"),
                  fact(4, "sberbank", "ограничения", "declared")]
    p = C.enforce_scope(plan(task="product", anchor="sberbank", subjects=["sberbank"]),
                        "Сервис Своё дело в Сбере", ["sberbank"])
    brief = B.parse(json.dumps({
        "report_title": "Сервис Своё дело", "answer": "Ответ [f:1].", "theses": ["т"],
        "sections": [{"key": "topic", "title": "Как подключается", "focus": "ф", "attributes": ["подключение"]},
                     {"key": "topic", "title": "Сколько стоит", "focus": "ф", "attributes": ["тарифы и комиссии"]},
                     {"key": "market", "title": "Сбер против рынка", "focus": "ф"},
                     {"key": "conditions", "title": "Ограничения", "focus": "ф"}]}),
        {"conditions": 3, "market": 3}, {"подключение": 1, "тарифы и комиссии": 2, "ограничения": 1})
    events, written = _write(monkeypatch, p, reg, brief)
    outline = next(x for k, x in events if k == "outline")
    assert outline == ["Резюме для руководителя проверки", "Что проверить",
                       "Как подключается", "Сколько стоит", "Ограничения"]
    assert "f:3" in written["topic2"] and "f:2" in written["topic2"]     # обе стороны в одной теме
    assert "f:4" in written["conditions"] and "f:1" not in written["conditions"]
    assert "market" not in written


def test_reference_report_has_one_lead_block(monkeypatch):
    reg = F.FactRegistry()
    reg.facts += [fact(i, "", "история появления", "observed", f"https://ria.ru/{i}") for i in range(1, 5)]
    p = C.enforce_scope(plan(task="explain"), "история кредитной карты", [])
    events, written = _write(monkeypatch, p, reg, None)
    outline = next(x for k, x in events if k == "outline")
    assert outline == ["Главное", "Рынок и публикации"]
    assert "checks" not in written and "summary" in written
    lead = next(x for k, x in events if k == "lead")
    assert lead.startswith("## Главное") and "Что провер" not in lead


def test_one_broken_section_does_not_cancel_the_report(monkeypatch):
    reg = F.FactRegistry()
    reg.facts += [fact(1, "sberbank", "условия", "declared"),
                  fact(2, "", "норма", "regulatory", "https://cbr.ru/a")]
    p = C.enforce_scope(plan(task="product", anchor="sberbank", subjects=["sberbank"]),
                        "Вклады Сбера", ["sberbank"])

    async def fake_brief(*a, **kw):
        return None
    monkeypatch.setattr(D.al_brief, "make_brief", fake_brief)

    async def fake_stream(client, model, prompt):
        if "РАЗДЕЛ «НОРМАТИВНАЯ РАМКА»" in prompt:
            raise RuntimeError("unknown failure")
        yield "Готовый текст раздела. "
    monkeypatch.setattr(D, "_stream_section", fake_stream)
    monkeypatch.setattr(D.al_viz, "MIN_FACTS", 10 ** 6)
    monkeypatch.setenv("LLM_MODEL_NAME", "m")
    state = runstate.new_run()

    async def run():
        return [ev async for ev in D.write_dossier(None, "m", question="q", plan=p,
                                                   registry=reg, state=state)]
    events = asyncio.run(run())
    text = "".join(x for k, x in events if k == "chunk")
    assert "Готовый текст раздела" in text and "Раздел не дописан" in text
    assert any(k == "lead" for k, _ in events)


# ── Короткий план проверки — только список действий ──────────────────────────
def test_short_checks_keep_only_the_numbered_actions():
    from bank_audit.research.gptr import dossier as D
    raw = """Сравнение веду относительно Сбербанка.

### Сводная таблица условий

| Характеристика | Сбербанк | ВТБ |
|---|---|---|
| Ставка | 26,9% [f:1] | 9,6% [f:2] |

**Вывод:** по нижней границе Сбербанк лучше.

### Что проверить

1. Запросить индивидуальные условия кредитования [f:17].
2. Сверить требования к заёмщику с регламентом [f:11],
   так как публично раскрыт только принцип.

3. Открыть тариф на допуслуги [f:11].

### Ещё что-то
текст после списка
"""
    out = D.only_actions(raw)
    assert out.splitlines() == [
        "1. Запросить индивидуальные условия кредитования [f:17].",
        "2. Сверить требования к заёмщику с регламентом [f:11], так как публично раскрыт только принцип.",
        "3. Открыть тариф на допуслуги [f:11]."]
    assert "Сводная таблица" not in out and "|" not in out
    # список длиннее предела обрезается, текст без списка остаётся как есть
    many = "\n".join(f"{i}. Действие номер {i}" for i in range(1, 10))
    assert len(D.only_actions(many).splitlines()) == 6
    assert D.only_actions("Проверять нечего: данных нет.") == "Проверять нечего: данных нет."


def test_short_checks_prompt_has_no_other_sections_tasks():
    from bank_audit.research.gptr import brief as B
    from bank_audit.research.gptr import dossier as D
    b = B.Brief(answer="Сбер дешевле по нижней границе.", theses=["тезис один"],
                sections=[{"key": "market", "title": "Сбер против ВТБ", "focus": "сводная таблица условий",
                           "attributes": []}])
    p = plan(task="compare", scope="named", checks="short", subjects=["sberbank", "vtb"])
    short = D.section_prompt("checks", p, "Сравни кредиты", {}, facts_text="f", prior_text="тело", brief=b)
    assert "сводная таблица условий" not in short and "начинается сразу с «1.»" in short
    assert "Сбер дешевле по нижней границе." in short
    # резюме по-прежнему видит задания разделов
    lead = D.section_prompt("summary", p, "Сравни кредиты", {}, facts_text="f", prior_text="тело", brief=b)
    assert "сводная таблица условий" in lead
