"""Жалобы в ИИ-помощнике: сюжеты раздела, темы у конкурентов, поиск по ситуации."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from bank_audit.ai import agent_tools as T
from bank_audit.research.gptr import own_data, runstate
from bank_audit.research.v2 import conductor as C


def plan(**kw):
    base = dict(intent="x", intent_summary="", question_nature="mixed", subjects=["sberbank", "vtb"],
                subject_labels={"sberbank": "Сбербанк", "vtb": "ВТБ"}, product="потребительский кредит",
                task="compare", scope="named", anchor="sberbank")
    base.update(kw)
    return C.ResearchPlan(**base)


def _quote(n, bank):
    return {"date": "2026-10-2%d" % n, "city": "Тверь", "product": "Потребительский кредит",
            "summary": f"Клиенту {bank} не пересчитали проценты после досрочного погашения №{n}",
            "quote": f"проценты после досрочного погашения не пересчитали, обращение {n}",
            "text": f"Внёс досрочно, но проценты после досрочного погашения не пересчитали, обращение {n}. Жду ответа.",
            "url": f"https://www.banki.ru/services/responses/bank/response/{bank}-{n}/"}


@pytest.fixture
def tools(monkeypatch):
    calls = []

    def answer(name, kw):
        bank = kw.get("bank")
        if name == "complaints_overview":
            return {"bank": bank, "as_of": "2026-11-10", "complaints": 162, "prev_period": 157, "change_pct": 3.0,
                    "themes": [{"theme": "loan_calc", "label": "Ошибки в кредите: расчёт, погашение", "n": 34,
                                "pct": 21.0, "change_pct": 17.0}], "risk_flags": [], "cities": []}
        if name == "complaint_signals":
            return {"signals": [], "overall": {}}
        if name == "complaint_theme":
            return {"label": "Ошибки в кредите: расчёт, погашение", "total": 34,
                    "similar_groups": [{"n": 6, "first": "2026-09-01", "last": "2026-10-20",
                                        "summary": "После досрочного погашения платёж не уменьшили",
                                        "quote": "платёж не уменьшили"}],
                    "complaints": [_quote(i, bank) for i in range(1, 7)]}
        if name == "complaint_stories":
            return {"bank": bank, "total_stories": 9, "period": {"to": "2026-11-10"},
                    "link": f"#reviews?tab=complaints&bank={bank}&view=stories",
                    "stories": [{"title": "Перенос платежа обернулся ростом долга", "what": "Клиент перенёс платёж и получил новые платежи",
                                 "problem": "Ошибки в кредите: расчёт, погашение", "complaints_in_period": 11,
                                 "complaints_all_time": 19, "first_seen": "2026-07-02", "last_seen": "2026-11-03",
                                 "status": "действует", "badges": ["вырос с 12 до 19"],
                                 "grew_this_week": {"week": 4, "norm_per_week": 1.2, "ratio": 3.3},
                                 "examples": [_quote(8, bank), _quote(9, bank)]},
                                {"title": "Пустой сюжет", "complaints_in_period": 0, "examples": []}]}
        if name == "complaint_search":
            return {"by_words": 2, "complaints": [_quote(7, bank)], "link": "#reviews?q=x"}
        raise KeyError(name)

    def spec(name):
        def fn(**kw):
            calls.append((name, kw))
            return json.dumps(answer(name, kw), ensure_ascii=False)
        return T.ToolSpec(name, name, fn, "")
    monkeypatch.setattr(T, "BY_NAME", {n: spec(n) for n in (
        "complaints_overview", "complaint_signals", "complaint_theme", "complaint_stories", "complaint_search")})
    monkeypatch.setattr(T, "_rd", lambda: SimpleNamespace(trend=lambda *a, **k: None))
    runstate.new_run()
    return calls


def test_product_understood_by_the_planner_is_not_lost_in_a_follow_up():
    data = {"product": "Потребительский кредит", "themes": ["loan_calc"],
            "search": ["не пересчитали проценты после досрочного погашения", "да", 5, "x" * 200, "третья фраза лишняя"]}
    # в уточнении продукт не назван, но план его знает
    scope = own_data.normalize_scope(data, "Подробнее про досрочное погашение — что у кого", plan())
    assert scope["product"] == "Потребительский кредит" and scope["themes"] == ["loan_calc"]
    assert scope["search"][0].startswith("не пересчитали проценты") and len(scope["search"]) == 2
    assert all(len(x) <= 80 for x in scope["search"])
    # продукта нет ни в вопросе, ни в плане — срез его не выдумывает
    scope = own_data.normalize_scope(data, "Что с блокировками карт", plan(product="блокировки карт"))
    assert scope["product"] is None


def test_competitor_gets_themes_and_stories_from_markup_not_a_word_search(tools):
    od = own_data.OwnData()
    scope = {"product": "Потребительский кредит", "themes": ["loan_calc"], "search": [], "days": 90, "rivals": True}
    own_data.collect_complaints(od, plan(), scope, "Сравни потребкредиты Сбера и ВТБ: досрочное погашение")
    names = [(n, kw.get("bank")) for n, kw in tools]
    assert ("complaint_stories", "Сбербанк") in names and ("complaint_stories", "ВТБ") in names
    assert ("complaint_theme", "ВТБ") in names
    assert not any(n == "complaint_search" for n, _ in names)          # долгий поиск не понадобился
    limits = {kw["bank"]: kw["limit"] for n, kw in tools if n == "complaint_stories"}
    assert limits == {"Сбербанк": 6, "ВТБ": 3}                          # у точки отсчёта подробнее
    text = "\n".join(od.pages.values())
    assert "Сюжет «Перенос платежа обернулся ростом долга»: 11 жалоб за 90 дней (всего 19, первая 02.07.2026" in text
    assert "вырос с 12 до 19" in text and "за неделю 4 при норме 1,2 (×3,3)" in text
    assert "Пустой сюжет" not in text
    vtb = [f for f in od.facts if f["subject"] == "vtb"]
    assert any(f["attribute"].startswith("Жалобы: сюжет «Перенос платежа") and f["value"] == "11" for f in vtb)
    assert any(f["attribute"] == "Жалобы: тема «Ошибки в кредите: расчёт, погашение» за 90 дней" for f in vtb)
    assert sum(1 for f in vtb if f["attribute"].startswith("Жалоба:")) == 5      # 4 по теме + 1 пример сюжета
    for f in od.facts:
        assert f["verbatim"] in od.pages[f["url"]]


def test_situation_outside_the_lists_is_searched_in_client_words(tools):
    od = own_data.OwnData()
    scope = {"product": None, "themes": [], "search": ["курьер не привёз карту"], "days": 90, "rivals": False}
    p = plan(task="complaints", scope="sber", subjects=["sberbank"], subject_labels={"sberbank": "Сбербанк"},
             product="курьерская доставка карт")
    own_data.collect_complaints(od, p, scope, "Какие проблемы с курьерской доставкой карт")
    searches = [kw["query"] for n, kw in tools if n == "complaint_search"]
    assert searches == ["курьер не привёз карту"]                       # не название продукта
    assert not any(n == "complaint_stories" for n, _ in tools)           # сюжеты всего банка сюда не идут


def test_stories_tool_gives_the_same_view_as_the_tab(monkeypatch):
    from bank_audit.rag import review_stories as rs
    stories = [{"story_id": 7, "title": "Перенос платежа обернулся ростом долга", "summary": "Суть сюжета",
                "issue_label": "Ошибки в кредите", "n": 19, "n_window": 11, "status": "active",
                "first_seen": "2026-07-02T10:00:00+00:00", "last_seen": "2026-11-03T09:00:00+00:00",
                "badges": [{"kind": "grew", "label": "вырос с 12 до 19"}], "top_urls": ["u1", "u2", "u3"], "urls": ["u1"]}]
    seen = {}

    def list_stories(bank, *, product=None, limit=30, t0=None, t1=None, _meta=None, **kw):
        seen.update(bank=bank, product=product, limit=limit, window=(t0, t1))
        _meta["total"] = 14
        return stories
    monkeypatch.setattr(rs, "list_stories", list_stories)
    monkeypatch.setattr(rs, "story_signals", lambda bank, product=None: [
        {"story_id": 7, "week": 4, "baseline_week": 1.2, "ratio": 3.3, "market": {"n": 2, "banks": 2}}])
    monkeypatch.setattr(T, "_rd", lambda: SimpleNamespace(resolve_bank=lambda b: "Сбербанк" if "бер" in b else None))
    monkeypatch.setattr(T, "_rw", lambda: SimpleNamespace(reviews_by_urls=lambda urls: [
        {"url": u, "date": "2026-10-20", "city": "Тверь", "product": "Потребительский кредит", "text": "Текст жалобы " + u,
         "ann": {"summary": "Изложение " + u, "quote": "цитата " + u}, "themes": []} for u in urls]))
    per = SimpleNamespace(days=90, binds=lambda: {"t0": datetime(2026, 8, 13), "t1": datetime(2026, 11, 11)},
                          info=lambda: {"from": "2026-08-13", "to": "2026-11-10"})
    monkeypatch.setattr(T, "_period", lambda product, days, d0=None, d1=None: per)
    monkeypatch.setattr(T, "link_stories", lambda bank, product, period: "#reviews?tab=complaints&view=stories")
    res = json.loads(T.tool_complaint_stories("Сбер", "Потребительский кредит", 90, 5))
    assert seen["bank"] == "Сбербанк" and seen["limit"] == 5 and seen["window"][0] == datetime(2026, 8, 13)
    st = res["stories"][0]
    assert (st["complaints_in_period"], st["complaints_all_time"], st["first_seen"]) == (11, 19, "2026-07-02")
    assert st["badges"] == ["вырос с 12 до 19"] and st["grew_this_week"]["ratio"] == 3.3
    assert [e["url"] for e in st["examples"]] == ["u1", "u2"] and st["examples"][0]["quote"] == "цитата u1"
    assert res["total_stories"] == 14 and res["link"].endswith("view=stories")
    # неизвестный банк и сбой базы — ошибка, а не «сюжетов нет»
    assert "не найден" in json.loads(T.tool_complaint_stories("Неизвестный"))["error"]

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(rs, "list_stories", boom)
    assert json.loads(T.tool_complaint_stories("Сбер"))["error"].startswith("stories_failed")
    assert any(t.name == "complaint_stories" for t in T.TOOLS)


def test_unavailable_reranker_is_skipped_for_a_while(monkeypatch):
    import httpx
    from bank_audit.ai import analyst
    from bank_audit.rag import reviews_dash as rd
    monkeypatch.setattr(analyst, "LLM_API_KEY", "k")
    monkeypatch.setattr(analyst, "LLM_BASE_URL", "http://x/v1")
    monkeypatch.setattr(rd, "_rr_down_until", 0.0)
    calls = []

    def post(*a, **kw):
        calls.append(1)
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "post", post)
    assert rd._rerank("перевод не дошёл", ["a", "b"]) is None
    assert rd._rerank("перевод не дошёл", ["a", "b"]) is None        # второй раз в сеть не ходим
    assert len(calls) == 1
    monkeypatch.setattr(rd, "_rr_down_until", 0.0)                     # пауза истекла — пробуем снова
    assert rd._rerank("перевод не дошёл", ["a", "b"]) is None and len(calls) == 2


# ── Короткий показ собственных данных, пока отчёт собирается ─────────────────
def test_preview_shows_counted_things_and_no_loopholes():
    from bank_audit.research.gptr import own_data as O
    od = O.OwnData()
    url = "#reviews?bank=ВТБ&view=stories"
    line1 = "Сюжет «Ставку снизили после открытия»: 14 жалоб за 90 дней."
    line2 = "Сюжет «Не закрывают вклад в приложении»: 9 жалоб за 90 дней."
    od.page(url, "AuditLens · Отзывы: ВТБ · Вклады — сюжеты за 90 дней", [line1, line2], "complaints")
    od.fact(subject="vtb", attribute="Жалобы: сюжет «Ставку снизили после открытия»", value="14",
            verbatim=line1, url=url)
    od.fact(subject="vtb", attribute="Жалобы: сюжет «Не закрывают вклад в приложении»", value="9",
            verbatim=line2, url=url)
    od.page("#market?cat=deposit", "AuditLens · Рынок: Вклады — лидеры",
            ["Витрина «Рынок»: описание.", "ВТБ: 15,5 % годовых, 6 мес.", "Сбербанк: 15 % годовых, 6 мес.",
             "Как сравнивать: по сроку."], O.MARKET)
    od.page("https://news.example/a", "ВТБ изменил условия вкладов с 1 ноября", ["Текст новости."], O.NEWS)
    od.page("#loophole", "AuditLens · Уязвимости: сводка раздела", ["Раздел «Аудит уязвимостей»: …"], "loopholes")
    pv = O.preview(od)
    kinds = [b["kind"] for b in pv["blocks"]]
    assert kinds == ["complaints", "market", "news"]          # уязвимостей в показе нет
    c = pv["blocks"][0]
    assert c["link"] == url and "ВТБ · Вклады" in c["title"]
    assert c["rows"][0] == {"label": "Ставку снизили после открытия", "value": "14 жалоб"}
    assert O._count_ru("1") == "1 жалоба" and O._count_ru("3") == "3 жалобы" and O._count_ru("—") == "—"
    m = pv["blocks"][1]
    assert [r["label"] for r in m["rows"]] == ["ВТБ: 15,5 % годовых, 6 мес.", "Сбербанк: 15 % годовых, 6 мес."]
    assert pv["blocks"][2]["rows"][0]["link"] == "https://news.example/a"
    assert O.preview(O.OwnData()) is None                     # нечего показать — события нет


# ── Новости для отчёта: отбор моделью, запасной — по словам ──────────────────
def _news_env(monkeypatch, answer):
    """news_find отдаёт три новости; модель отвечает answer (текст или исключение)."""
    import json as _json
    import types

    from bank_audit.research.gptr import facts as F
    from bank_audit.research.gptr import own_data as O
    rows = [{"url": "https://n.example/1", "title": "Сбер обновил сервис для плательщиков НПД", "text": "текст",
             "summary": "новые условия", "source": "РБК", "ts": "2026-11-01T10:00:00"},
            {"url": "https://n.example/2", "title": "Возбуждено уголовное дело о хищении", "text": "текст",
             "summary": "", "source": "ТАСС", "ts": "2026-11-02T10:00:00"},
            {"url": "https://n.example/3", "title": "Своё дело: Сбер поднял лимит для самозанятых", "text": "текст",
             "summary": "лимит вырос", "source": "Ведомости", "ts": "2026-11-03T10:00:00"}]
    monkeypatch.setattr(O, "_j", lambda tool, **kw: {"news": rows})
    seen: dict = {}

    async def call_model(client, model, kw):
        seen.update(kw)
        if isinstance(answer, Exception):
            raise answer
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=answer))])
    monkeypatch.setattr(F, "call_model", call_model)
    plan = types.SimpleNamespace(product="Своё дело", product_synonyms=["самозанятые"], intent_summary="")
    return O, plan, seen, _json


def test_news_are_picked_by_the_model_not_by_word_stems(monkeypatch):
    import asyncio
    O, plan, seen, _ = _news_env(monkeypatch, '{"keep": [1, 3, 9]}')
    cands = O.news_candidates(plan, "Сервис Своё дело для самозанятых")
    keep = asyncio.run(O.pick_news(None, "m", "Сервис Своё дело для самозанятых", plan, cands))
    assert keep == [0, 2]                      # «уголовное дело» отсеяно, НПД без слова «самозанятый» взято
    user = seen["messages"][1]["content"]
    assert "1. Сбер обновил сервис" in user and "2. Возбуждено уголовное дело" in user
    od = O.OwnData()
    O.add_news(od, cands, keep)
    assert od.news == 2 and list(od.pages) == ["https://n.example/1", "https://n.example/3"]


def test_news_fall_back_to_words_when_the_model_is_silent(monkeypatch):
    import asyncio
    O, plan, _seen, _ = _news_env(monkeypatch, RuntimeError("шлюз"))
    cands = O.news_candidates(plan, "Сервис Своё дело для самозанятых")
    assert asyncio.run(O.pick_news(None, "m", "вопрос", plan, cands)) is None
    assert O.news_by_words(cands, plan, "вопрос") == [1, 2]      # прежний отбор: по основам слов
    O2, plan2, _s, _ = _news_env(monkeypatch, "не JSON")
    assert asyncio.run(O2.pick_news(None, "m", "вопрос", plan2, cands)) is None
    assert asyncio.run(O.pick_news(None, "m", "вопрос", plan, [])) == []
