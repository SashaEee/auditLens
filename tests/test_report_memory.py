"""Память исследования: факты отчёта сохраняются и продолжают следующий вопрос."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bank_audit.research.gptr import facts as F
from bank_audit.research.gptr import memory as M
from bank_audit.research.gptr import runstate
from bank_audit.research.v2 import conductor as C


def _plan(**kw):
    base = dict(intent="x", intent_summary="Сравнить условия кредитов наличными", question_nature="mixed",
                subjects=["sberbank", "vtb"], subject_labels={"sberbank": "Сбербанк", "vtb": "ВТБ"},
                product="потребительский кредит", task="compare", scope="named")
    base.update(kw)
    return C.ResearchPlan(**base)


def _run():
    state = runstate.new_run()
    reg = F.FactRegistry()
    state.note_page("https://sberbank.ru/credit", "Кредит наличными\nСтавка от 19,4% годовых. Сумма от 10 000 рублей.")
    state.page_dates["https://sberbank.ru/credit"] = "2026-11-01"
    state.own_meta["auditlens://complaints/sberbank"] = {"kind": "complaints", "title": "Жалобы: Сбербанк"}
    state.pages["auditlens://complaints/sberbank"] = "Жалобы: Сбербанк\nЗа 90 дней 162 жалобы"
    f1 = reg.add(subject="sberbank", attribute="процентная ставка", value="19,4", unit="%",
                 verbatim="Ставка от 19,4% годовых", url="https://sberbank.ru/credit", stance="declared")
    f1.support = "дословно"
    f2 = reg.add(subject="sberbank", attribute="жалобы клиентов", value="162", unit="",
                 verbatim="За 90 дней 162 жалобы", url="auditlens://complaints/sberbank", stance="observed")
    f2.support = "близко к тексту"
    f2.date = "2026-11-10"
    return reg, state


def test_snapshot_survives_json_and_restores_into_a_new_run():
    reg, state = _run()
    mem = json.loads(json.dumps(M.dump(reg, state, _plan(), "Сравни кредиты Сбера и ВТБ",
                                       ["процентная ставка", "жалобы клиентов"]),
                                ensure_ascii=False, default=str))
    assert mem["plan"]["product"] == "потребительский кредит" and len(mem["facts"]) == 2
    assert mem["sources"]["https://sberbank.ru/credit"]["title"] == "Кредит наличными"
    assert mem["sources"]["auditlens://complaints/sberbank"]["own"]["kind"] == "complaints"

    state2 = runstate.new_run()
    reg2 = F.FactRegistry()
    n, support = M.preload(reg2, state2, mem)
    # сводка жалоб из памяти не берётся: её прочитают заново, на сегодня
    assert n == 1 and support == {"exact": 1, "close": 0}
    got = {f.attribute: f for f in reg2.facts}
    assert got["процентная ставка"].value == "19,4" and got["процентная ставка"].stance == "declared"
    assert "жалобы клиентов" not in got and "auditlens://complaints/sberbank" not in state2.pages
    # «страница» источника — его цитаты: число отчёта сверяется с ними
    assert "Ставка от 19,4% годовых" in state2.pages["https://sberbank.ru/credit"]
    assert state2.page_dates["https://sberbank.ru/credit"] == "2026-11-01"


def test_client_review_quotes_stay_in_memory_but_summaries_do_not():
    state = runstate.new_run()
    reg = F.FactRegistry()
    state.own_meta["https://banki.ru/r/1"] = {"kind": "review", "title": "Отзыв"}
    state.pages["https://banki.ru/r/1"] = "Отзыв\nКурьер не приехал в назначенный интервал"
    state.own_meta["auditlens://market/credit"] = {"kind": "market", "title": "Рынок"}
    state.pages["auditlens://market/credit"] = "Рынок\nПСК от 18,4%"
    f = reg.add(subject="sberbank", attribute="жалобы", value="", unit="", stance="observed",
                verbatim="Курьер не приехал в назначенный интервал", url="https://banki.ru/r/1")
    f.date = "2026-10-07"
    reg.add(subject="sberbank", attribute="ПСК", value="18,4", unit="%", stance="observed",
            verbatim="ПСК от 18,4%", url="auditlens://market/credit")
    mem = json.loads(json.dumps(M.dump(reg, state, _plan(), "q"), ensure_ascii=False, default=str))
    reg2, state2 = F.FactRegistry(), runstate.new_run()
    n, _ = M.preload(reg2, state2, mem)
    assert n == 1 and reg2.facts[0].url == "https://banki.ru/r/1" and reg2.facts[0].date == "2026-10-07"
    assert state2.own_meta["https://banki.ru/r/1"]["kind"] == "review"
    assert M.MAX_AGE_DAYS <= 3


def test_brief_for_planner_names_subject_and_what_is_collected():
    reg, state = _run()
    mem = M.dump(reg, state, _plan(), "Сравни кредиты Сбера и ВТБ")
    mem["created_at"] = datetime.now(timezone.utc) - timedelta(days=2)
    text = M.brief(mem)
    assert "потребительский кредит" in text and "Сбербанк, ВТБ" in text
    assert "Собрано фактов: 2" in text and "процентная ставка — 1" in text
    assert M.age_days(mem) == 2 and M.when(mem)


def test_topic_guard_and_new_organisations():
    reg, state = _run()
    mem = M.dump(reg, state, _plan(), "Сравни кредиты")
    assert M.same_topic(_plan(product="кредит наличными"), mem)           # общий корень «креди»
    assert not M.same_topic(_plan(product="вклады и накопительные счета"), mem)
    assert M.same_topic(_plan(product=""), mem)                             # продукт не назван — решает модель
    assert M.new_subjects(_plan(subjects=["sberbank"]), mem) == []
    assert M.new_subjects(_plan(subjects=["sberbank", "alfabank"]), mem) == ["alfabank"]


def _client(answer: dict, seen: list):
    class Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    seen.append(kw["messages"][-1]["content"])
                    msg = SimpleNamespace(content=json.dumps(answer, ensure_ascii=False))
                    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return Client()


def test_continuation_without_named_banks_inherits_previous_frame():
    seen: list = []
    answer = {"intent": "x", "task": "product", "scope": "sber", "subjects": ["sberbank"],
              "subject_labels": {"sberbank": "Сбербанк"}, "anchor": "sberbank",
              "product": "потребительский кредит", "topics": ["требования к заёмщику"],
              "queries": [], "continues": True}
    p = asyncio.run(C.plan_research(
        _client(answer, seen), "m", "подробнее про требования к заёмщику",
        prior="Вопрос: Сравни кредиты\nПредмет: потребительский кредит; организации: Сбербанк, ВТБ",
        prior_subjects={"sberbank": "Сбербанк", "vtb": "ВТБ"}))
    assert p.continues and p.subjects == ["sberbank", "vtb"] and p.subject_labels["vtb"] == "ВТБ"
    assert "# Предыдущий отчёт в этом разговоре" in seen[0] and '"continues": true' in seen[0]


def test_new_topic_or_no_memory_is_never_a_continuation():
    seen: list = []
    answer = {"intent": "x", "task": "product", "scope": "sber", "subjects": ["sberbank"],
              "anchor": "sberbank", "product": "вклады", "queries": [], "continues": False}
    p = asyncio.run(C.plan_research(_client(answer, seen), "m", "условия вкладов в Сбере",
                                    prior="Вопрос: Сравни кредиты", prior_subjects={"vtb": "ВТБ"}))
    assert not p.continues and p.subjects == ["sberbank"]
    # модель сказала «продолжение», но памяти нет — не продолжение
    answer["continues"] = True
    p = asyncio.run(C.plan_research(_client(answer, seen), "m", "условия вкладов в Сбере"))
    assert not p.continues and "Предыдущий отчёт" not in seen[-1]


def test_memory_is_off_without_session_and_survives_missing_table(monkeypatch):
    assert M.latest(None, "u") is None and M.latest(5, "") is None
    from bank_audit import db

    def boom():
        raise RuntimeError('relation "report_memory" does not exist')
    monkeypatch.setattr(db, "session", boom)
    assert M.latest(5, "u") is None
    assert M.save(7, 5, "u", {"facts": [{"q": "x"}]}) is False
    assert M.save(7, 5, "u", {"facts": []}) is False


def test_memory_event_never_reaches_the_client():
    async def inner():
        yield json.dumps({"type": "text", "chunk": "a"}, ensure_ascii=False)
        yield json.dumps({"type": "memory", "data": {"facts": [1]}}, ensure_ascii=False)
        yield json.dumps({"type": "done"}, ensure_ascii=False)

    async def collect():
        return [json.loads(e)["type"] async for e in M.without_event(inner())]
    assert asyncio.run(collect()) == ["text", "done"]
