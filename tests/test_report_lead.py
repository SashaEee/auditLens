"""Руководитель исследования: добор недостающего после первого сбора."""
import asyncio
import json
from types import SimpleNamespace

from bank_audit.research.gptr import facts as F
from bank_audit.research.gptr import lead as L
from bank_audit.research.gptr import runstate
from bank_audit.research.v2 import conductor as C


def plan(**kw):
    base = dict(intent="x", intent_summary="Нужны доли банков на рынке ОМС", question_nature="mixed",
                subjects=[], subject_labels={}, product="обезличенные металлические счета",
                task="market_data", scope="none")
    base.update(kw)
    return C.ResearchPlan(**base)


def answer(obj):
    msg = SimpleNamespace(content=json.dumps(obj, ensure_ascii=False))
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_enough_means_no_extra_search():
    need = L.parse('{"enough": true, "missing": "", "queries": [{"q": "что-то ещё поискать"}]}', [])
    assert need.enough and need.queries == []


def test_new_queries_are_cleaned_and_repeats_are_dropped():
    raw = json.dumps({"enough": False, "missing": "нет долей банков", "queries": [
        {"q": "рынок ОМС доли банков 2026", "site": ""},                      # уже был
        {"q": "site:cbr.ru форма 0409101 металлические счета остатки", "site": "https://www.cbr.ru/banking_sector/"},
        {"q": "ОМС", "site": ""},                                             # слишком короткий
        "металлические счета физлиц остатки по банкам рейтинг",
        {"q": "ещё один запрос про рынок драгметаллов", "site": "не домен"},
        {"q": "пятый запрос сверх предела и он лишний", "site": ""},
        {"q": "шестой запрос сверх предела", "site": ""}]})
    need = L.parse(raw, ["рынок ОМС доли банков 2026 site:frankrg.com"])
    assert not need.enough and need.missing == "нет долей банков"
    assert need.queries[0] == {"q": "форма 0409101 металлические счета остатки", "site": "cbr.ru"}
    assert need.queries[1] == {"q": "металлические счета физлиц остатки по банкам рейтинг", "site": ""}
    assert need.queries[2]["site"] == "" and len(need.queries) == L.MAX_QUERIES


def test_broken_answer_or_model_failure_keeps_the_report_going(monkeypatch):
    assert L.parse("не JSON вовсе", []).enough
    assert L.parse('{"enough": false, "queries": []}', []).enough      # не хватает, но искать нечем

    async def boom(client, model, kw):
        raise RuntimeError("Connection error.")
    monkeypatch.setattr(F, "call_model", boom)
    state = runstate.new_run()
    need = asyncio.run(L.assess(None, "m", question="q", plan=plan(), registry=F.FactRegistry(), state=state))
    assert need.enough


def test_assessor_sees_question_done_queries_and_fact_quotes(monkeypatch):
    seen = {}

    async def fake(client, model, kw):
        seen["user"] = kw["messages"][1]["content"]
        return answer({"enough": False, "missing": "нет долей", "queries": [{"q": "остатки на металлических счетах по банкам"}]})
    monkeypatch.setattr(F, "call_model", fake)
    state = runstate.new_run()
    state.subqueries = ["рынок ОМС доли банков 2026"]
    state.unreadable["https://cbr.ru/hd_base/metall/"] = "каркас без содержимого"
    reg = F.FactRegistry()
    reg.add(subject="", attribute="объём", value="2,4 трлн", unit="руб.", url="https://cbr.ru/x",
            verbatim="Резервы по страхованию жизни составили 2,4 трлн руб.", stance="regulatory")
    need = asyncio.run(L.assess(None, "m", question="Доля рынка по ОМС", plan=plan(), registry=reg, state=state))
    assert not need.enough and len(need.queries) == 1
    assert "Доля рынка по ОМС" in seen["user"] and "рынок ОМС доли банков 2026" in seen["user"]
    assert "«Резервы по страхованию жизни" in seen["user"] and "cbr.ru/hd_base/metall" in seen["user"]


def test_fill_reads_only_new_pages_and_adds_facts(monkeypatch):
    state = runstate.new_run()
    state.pages["https://old.ru/a"] = "уже прочитано"
    hits = {"остатки на металлических счетах по банкам": ["https://old.ru/a", "https://cbr.ru/new1", "https://cbr.ru/new2",
                                                           "https://cbr.ru/new3", "https://cbr.ru/new4"],
            "рейтинг банков ОМС": ["https://frankrg.com/r1", "https://cbr.ru/new1"]}
    monkeypatch.setattr(L, "_search", lambda q, site: hits[q])

    def read(url, st):
        if url.endswith("new2"):
            return "мало"                                   # почти пустая страница
        st.note_page(url, "Остатки на металлических счетах Сбербанка составили 120 млрд рублей. " * 10)
        return st.pages[url]
    monkeypatch.setattr(L, "_read", read)

    async def fake_extract(reg, client, model, *, pages, attributes, plan, **kw):
        for u in pages:
            reg.add(subject="", attribute="остатки", value="120", unit="млрд руб.", url=u,
                    verbatim="Остатки на металлических счетах Сбербанка составили 120 млрд рублей", stance="observed")
        return len(pages)
    monkeypatch.setattr(F, "extract_into", fake_extract)
    need = L.Need(enough=False, missing="нет остатков", queries=[
        {"q": "остатки на металлических счетах по банкам", "site": "cbr.ru"}, {"q": "рейтинг банков ОМС", "site": ""}])
    reg = F.FactRegistry()
    out = asyncio.run(L.fill(None, "m", need, plan=plan(), registry=reg, attributes=["остатки"], state=state))
    # с одного сайта — не больше трёх, уже прочитанная страница не берётся
    assert "https://old.ru/a" not in out.urls and "https://cbr.ru/new4" not in out.urls
    assert out.pages == 3 and out.facts == 3 and out.queries == 2
    assert state.subqueries[-2:] == ["остатки на металлических счетах по банкам site:cbr.ru", "рейтинг банков ОМС"]


def test_fill_without_new_pages_changes_nothing(monkeypatch):
    state = runstate.new_run()
    monkeypatch.setattr(L, "_search", lambda q, site: [])
    need = L.Need(enough=False, queries=[{"q": "что-нибудь новое про рынок", "site": ""}])
    out = asyncio.run(L.fill(None, "m", need, plan=plan(), registry=F.FactRegistry(), attributes=["x"], state=state))
    assert (out.pages, out.facts) == (0, 0) and state.subqueries == []


def test_search_engine_service_pages_are_not_sources():
    from bank_audit.rag import web_search as W
    res = W._real_pages([
        {"url": "https://yandex.ru/images/search?text=%D0%B8%D1%81%D1%82", "domain": "yandex.ru"},
        {"url": "https://tr-page.yandex.ru/translate?lang=en-ru&url=https%3A%2F%2Fwww.history.com%2Farticles%2Fcards",
         "domain": "tr-page.yandex.ru"},
        {"url": "https://www.google.com/search?q=x", "domain": "google.com"},
        {"url": "https://yandex.ru/company/press", "domain": "yandex.ru"},        # обычная страница Яндекса
        {"url": "https://cbr.ru/statistics/", "domain": "cbr.ru"},
        {"url": "https://cbr.ru/statistics/", "domain": "cbr.ru"},                # повтор
        {"url": "https://translate.yandex.ru/translate?lang=en-ru", "domain": "translate.yandex.ru"}])
    assert [r["url"] for r in res] == ["https://www.history.com/articles/cards",
                                       "https://yandex.ru/company/press", "https://cbr.ru/statistics/"]
    assert res[0]["domain"] == "history.com"


def test_unread_page_is_named_by_site_and_path():
    from bank_audit.research.gptr import gaps as G
    assert G.short_url("https://www.cbr.ru/hd_base/metall/?UniDbQuery.Posted=True&x=1") == "cbr.ru/hd_base/metall"
    long = G.short_url("https://example.ru/" + "раздел/" * 30)
    assert len(long) <= 70 and long.endswith("…")
    assert G.short_url("не адрес") == "не адрес"


def test_critic_checks_only_facts_added_by_the_fill():
    from bank_audit.research.gptr import critic as K
    reg = F.FactRegistry()
    page1 = "Ставка по вкладу составляет 16 процентов годовых при открытии в приложении банка."
    reg.add(subject="sber", attribute="ставка", value="16", unit="%", url="https://a.ru/1",
            verbatim="Ставка по вкладу составляет 16 процентов годовых", stance="declared")
    pages = {"https://a.ru/1": page1}
    first = asyncio.run(K.review(None, "m", reg, pages))
    known = {f.id for f in reg.facts}
    assert first.exact == 1 and first.cut == 0

    judged = []

    async def fake_judge(client, model, doubtful, pg):
        judged.extend(f.verbatim for f in doubtful)
        return {f.id for f in doubtful}                      # модель сняла всё сомнительное
    orig, K.judge = K.judge, fake_judge
    try:
        page2 = "Остатки на металлических счетах выросли до 120 млрд рублей за год по данным банка."
        reg.add(subject="sber", attribute="остатки", value="120", unit="млрд руб.", url="https://b.ru/2",
                verbatim="Остатки на металлических счетах выросли до 120 млрд рублей", stance="observed")
        reg.add(subject="sber", attribute="остатки", value="999", unit="млрд руб.", url="https://b.ru/2",
                verbatim="Такого предложения на странице нет совсем и быть не может", stance="observed")
        pages["https://b.ru/2"] = page2
        total = asyncio.run(K.review_added(None, "m", reg, pages, known, first))
    finally:
        K.judge = orig
    assert judged == ["Такого предложения на странице нет совсем и быть не может"]
    assert (total.exact, total.cut) == (2, 1) and len(reg.facts) == 2
    assert total.notes and "Снято утверждений без опоры на источник: 1 из 3" in total.notes[0]
    # добор ничего не добавил — вердикт прежний
    assert asyncio.run(K.review_added(None, "m", reg, pages, {f.id for f in reg.facts}, first)) is first
