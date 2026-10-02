"""Новости «Обзора»: повторы, сюжеты, выпуск после выходных.

27 и 28.09 выпуск дважды открывался одной новостью из разных изданий
(«с 1 октября наличные через чужой банкомат по СБП»), а понедельничный выпуск
28.09 вышел с четырьмя новостями — только за воскресенье. БД, модели и
векторы подменяются.
"""
import asyncio
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from bank_audit.digest import newsflow as nf
from bank_audit.digest import writer
from bank_audit.rag import embedder

MSK = timezone(timedelta(hours=3))


# ── окно выпуска ─────────────────────────────────────────────────────────────

def test_monday_issue_covers_weekend():
    sc = nf.issue_scope(date(2026, 9, 28), datetime(2026, 9, 28, 7, 0, tzinfo=MSK))
    assert sc["after_off"]
    assert sc["republish_from"] == date(2026, 9, 26)          # выпуски сб и вс — снова можно
    assert sc["days_off"] == ["2026-09-26", "2026-09-27"]
    assert sc["since"] == "2026-09-25T05:00:00+03:00"          # с утра пятницы
    assert sc["window_h"] >= 74 and sc["reg_window_h"] >= sc["window_h"]
    assert sc["news_max"] == nf.NEWS_MAX_AFTER_OFF


@pytest.mark.parametrize("d", [date(2026, 9, 29), date(2026, 9, 26), date(2026, 9, 27)])
def test_ordinary_days_keep_one_day_window(d):
    sc = nf.issue_scope(d)
    assert not sc["after_off"]
    assert sc["republish_from"] == d and sc["window_h"] == 26.0
    assert sc["news_max"] == nf.NEWS_MAX


def test_holiday_counts_as_day_off(monkeypatch):
    monkeypatch.setattr(nf, "HOLIDAYS", {date(2026, 11, 4)})
    sc = nf.issue_scope(date(2026, 11, 5), datetime(2026, 11, 5, 7, 0, tzinfo=MSK))
    assert sc["after_off"] and sc["days_off"] == ["2026-11-04"]
    assert sc["prev_workday"] == "2026-11-03"


# ── отбор ────────────────────────────────────────────────────────────────────

def test_dated_regulation_on_the_edge_goes_in():
    """«С 1 октября банкам снова сократят квоту…» — 5 из 10 внутри интервью."""
    today = date(2026, 9, 28)
    e = {"value": 5, "s2": {"category": "regulation", "deadline": "2026-10-01"}}
    nf._dated_floor(e, today)
    assert e["value"] == 6 and e["floor"] == "dated_regulation"
    for v, cat, dl in ((5, "market_background", "2026-10-01"), (5, "regulation", "2026-09-20"),
                       (4, "regulation", "2026-10-01"), (5, "regulation", "")):
        e = {"value": v, "s2": {"category": cat, "deadline": dl}}
        nf._dated_floor(e, today)
        assert e["value"] == v


def test_digest_is_not_event_lead():
    items = [
        {"source": "banki_news", "title": "Главное о кредитах за неделю", "body": "x" * 5000, "ts": None},
        {"source": "ria_novosti", "title": "ЦБ ужесточит выдачу кредитов должникам", "body": "y" * 900, "ts": None},
    ]
    items.sort(key=lambda x: nf._lead_key(x, {"banki_news": 1, "ria_novosti": 3}))
    assert items[0]["source"] == "ria_novosti"


def _patch_published(monkeypatch, pub_by_event):
    class _S:
        def execute(self, _stmt, params):
            class _R:
                def scalar(_self):
                    return pub_by_event.get(params["e"])
            return _R()

    @contextmanager
    def fake():
        yield _S()
    monkeypatch.setattr(nf.db, "session", fake)


def _ev(eid, value, cat="fraud", hours_ago=10, title="Новость"):
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    lead = {"id": eid, "title": title, "url": f"https://x/{eid}", "source": "ria_novosti", "ts": ts}
    return {"event_id": eid, "lead": lead, "items": [lead], "n_sources": 1, "rel": 8, "rtype": cat,
            "s2": {"category": cat, "headline": title}, "value": value, "ts": ts}


def test_weekend_issues_can_be_shown_again_on_monday(monkeypatch):
    evs = [_ev(1, 8, hours_ago=20), _ev(2, 7, hours_ago=10), _ev(3, 6, hours_ago=60)]
    monkeypatch.setattr(nf, "_event_rows", lambda since_h: [dict(e) for e in evs])
    today = datetime.now(MSK).date()
    _patch_published(monkeypatch, {1: today - timedelta(days=1)})   # вышло во вчерашнем выпуске
    plain = nf.day_events(26, 96, today)
    assert [e["event_id"] for e in plain] == [2]      # обычный день: сутки и без вышедшего вчера
    wide = nf.day_events(75, 96, today - timedelta(days=2))
    assert [e["event_id"] for e in wide] == [1, 2, 3]  # после выходных: трое суток, выходные снова


# ── сверка с вышедшим ────────────────────────────────────────────────────────

_PREV = [
    {"event_id": 65139, "published_on": date(2026, 9, 27), "url": "https://banki/65139",
     "head": "С 1 октября — внесение наличных через банкоматы любых банков по СБП с лимитами",
     "summary": "Лимиты 25 тыс. за операцию."},
    {"event_id": 500, "published_on": date(2026, 9, 25), "url": "https://ria/500",
     "head": "ЦБ предложил ограничить выдачу кредитов закредитованным заёмщикам",
     "summary": "Проект."},
]
_VEC = {
    _PREV[0]["head"]: [1.0, 0.0, 0.0],
    _PREV[1]["head"]: [0.0, 1.0, 0.0],
    "С 1 октября — пополнение счёта наличными через чужой банкомат по СБП": [0.95, 0.05, 0.0],
    "ЦБ утвердил ограничения выдачи кредитов закредитованным": [0.0, 0.72, 0.69],
    "Троян RedWing заразил 10 тыс. Android": [0.0, 0.0, 1.0],
}


def _cand(eid, head, summary=""):
    return {"event_id": eid, "lead": {"title": head, "url": f"https://x/{eid}"},
            "s2": {"headline": head, "summary": summary}}


@pytest.fixture
def repeat_env(monkeypatch):
    monkeypatch.setattr(nf, "published_episodes", lambda before, days=14: [dict(p) for p in _PREV])
    monkeypatch.setattr(embedder, "embed_batch", lambda texts, *a, **k: [_VEC[t] for t in texts])

    def cos(a, b):
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return sum(x * y for x, y in zip(a, b)) / (na * nb)
    monkeypatch.setattr(embedder, "cosine_similarity", cos)
    calls = []

    async def chat(model, system, user, max_tokens):
        calls.append(user)
        return json.dumps({"items": [{"n": 1, "rel": "same", "new": ""},
                                     {"n": 2, "rel": "story", "new": "ЦБ утвердил лимиты на IV квартал"}]},
                          ensure_ascii=False), 10, 10
    monkeypatch.setattr(nf, "_chat", chat)
    return calls


def test_same_story_from_another_outlet_is_a_repeat(repeat_env):
    cands = [_cand(103664, "С 1 октября — пополнение счёта наличными через чужой банкомат по СБП"),
             _cand(700, "ЦБ утвердил ограничения выдачи кредитов закредитованным"),
             _cand(91124, "Троян RedWing заразил 10 тыс. Android")]
    out = asyncio.run(nf.classify_repeats(cands, date(2026, 9, 28)))
    assert out[103664]["kind"] == "repeat"
    assert out[103664]["prev"]["date"] == "2026-09-27"
    assert out[700]["kind"] == "continuation"
    assert out[700]["new_fact"] == "ЦБ утвердил лимиты на IV квартал"
    assert out[700]["episodes"][-1]["url"] == "https://ria/500"
    assert 91124 not in out                                      # другое событие — без пары
    assert "БЫЛО (27.09)" in repeat_env[0]


def test_without_model_close_pairs_become_continuations(repeat_env, monkeypatch):
    async def down(*a, **k):
        raise RuntimeError("модель недоступна")
    monkeypatch.setattr(nf, "_chat", down)
    cands = [_cand(103664, "С 1 октября — пополнение счёта наличными через чужой банкомат по СБП"),
             _cand(700, "ЦБ утвердил ограничения выдачи кредитов закредитованным")]
    out = asyncio.run(nf.classify_repeats(cands, date(2026, 9, 28)))
    assert out[103664]["kind"] == "continuation"                 # не теряем новость
    assert 700 not in out                                        # серая зона без модели — не трогаем


# ── заголовок ────────────────────────────────────────────────────────────────

def test_continuation_never_opens_the_issue():
    leads = [{"ref": "a", "repeat": True}, {"ref": "b"}, {"ref": "c"}]
    assert [x["ref"] for x in writer._fresh_first(leads)] == ["b", "a", "c"]
    assert [x["ref"] for x in writer._fresh_first([{"ref": "a"}, {"ref": "b", "repeat": True}])] == ["a", "b"]
    only = [{"ref": "a", "repeat": True}]
    assert writer._fresh_first(only) == only


def test_continuation_lead_is_marked_and_lower(monkeypatch):
    monkeypatch.setattr(writer, "_new_sber_loopholes", lambda: [])
    monkeypatch.setattr(writer, "_sber_rating_move", lambda: None)
    item = {"event_id": 103664, "title": "Пополнение наличными через чужой банкомат по СБП", "score": 8,
            "domain": "ria.ru", "codes": [], "continues": {"date": "2026-09-27", "title": "Внесение наличных"}}
    fresh = {**item, "event_id": 91124, "title": "Троян RedWing", "continues": None}
    secs = {"news": {"payload": {"groups": [{"items": [item, fresh]}]}}}
    leads, _bg = writer._build_leads(secs, set())
    by = {ld["ref"]: ld for ld in leads}
    assert by["news:103664"]["repeat"] and by["news:103664"]["score"] == 7.0
    assert "продолжение сюжета, выходил 27.09" in by["news:103664"]["facts"]
    assert not by["news:91124"]["repeat"]
    assert writer._provenance("news_alert", item).endswith("продолжение сюжета от 27.09")


# ── раздел новостей целиком ─────────────────────────────────────────────────

def test_news_section_drops_repeats_and_marks_weekend(monkeypatch):
    monday = date(2026, 9, 28)
    evs = [_ev(103664, 8, "payments", title="С 1 октября — пополнение наличными по СБП"),
           _ev(91124, 8, "fraud", title="Троян RedWing заразил 10 тыс. Android"),
           _ev(700, 7, "regulation", title="ЦБ утвердил лимиты для закредитованных")]
    for e in evs:
        e["group"] = "incidents"
    seen = {}

    async def tick():
        return {}

    def day_events(window_h, reg_window_h, republish_from):
        seen.update(window_h=window_h, republish_from=republish_from)
        return [dict(e) for e in evs]

    async def merge(x):
        return x

    async def classify(cands, before):
        seen["before"] = before
        return {103664: {"kind": "repeat", "prev": {"date": "2026-09-27", "title": "Внесение", "url": "u"}},
                700: {"kind": "continuation", "new_fact": "утверждены", "episodes": [{"title": "Проект"}],
                      "prev": {"date": "2026-09-25", "title": "Проект", "url": "u2"}}}
    published = []
    monkeypatch.setattr(nf, "issue_scope", lambda day: {
        "after_off": True, "window_h": 75.0, "reg_window_h": 96.0,
        "republish_from": date(2026, 9, 26), "news_max": 20, "since": "2026-09-25T05:00:00+03:00",
        "days_off": ["2026-09-26", "2026-09-27"], "prev_workday": "2026-09-25"})
    monkeypatch.setattr(nf, "tick", tick)
    monkeypatch.setattr(nf, "day_events", day_events)
    monkeypatch.setattr(nf, "merge_events", merge)
    monkeypatch.setattr(nf, "classify_repeats", classify)
    monkeypatch.setattr(nf, "health", lambda: {"sources": [], "items_24h": 1, "relevant_24h": 1})
    monkeypatch.setattr(nf, "mark_published", lambda ids: published.extend(ids))
    from bank_audit.digest import news as news_mod
    monkeypatch.setattr(news_mod, "mark_published", lambda urls: None)

    out = asyncio.run(writer.news(monday))
    ids = [it["event_id"] for g in out["groups"] for it in g["items"]]
    assert 103664 not in ids and set(ids) == {91124, 700}
    assert sorted(published) == [700, 91124]                     # повтор не отмечаем вышедшим
    cont = next(it for g in out["groups"] for it in g["items"] if it["event_id"] == 700)
    assert cont["continues"]["date"] == "2026-09-25" and cont["new_fact"] == "утверждены"
    assert cont["story"] == [{"title": "Проект"}]
    assert out["triage"]["repeats"] == 1 and out["triage"]["continuations"] == 1
    assert out["repeats"][0]["prev"]["date"] == "2026-09-27"
    assert out["scope"]["days_off"] == ["2026-09-26", "2026-09-27"]
    assert seen["republish_from"] == date(2026, 9, 26) and seen["before"] == date(2026, 9, 26)


def test_story_that_led_before_does_not_lead_again(monkeypatch):
    """Понедельник не повторяет заголовок субботы, хотя новости выходных в ленте."""
    top = [{"ref": "news:1", "data": {"title": "С 15 октября операторы блокируют немаркированные звонки"}, "facts": ""},
           {"ref": "news:2", "data": {"title": "ЦБ выдаёт предписания против рассрочки со скрытой платой"}, "facts": ""}]
    prev = [{"date": date(2026, 9, 26), "headline": "С 15 октября операторы могут блокировать звонки банков",
             "lead_title": "С 15 октября операторы блокируют немаркированные звонки банков"}]
    seen = []

    async def chat(model, system, user, max_tokens):
        seen.append(user)
        return '{"items":[{"n":1,"v":"same"},{"n":2,"v":"diff"}]}', 1, 1
    monkeypatch.setattr(nf, "_chat", chat)
    asyncio.run(writer._mark_led_before(top, prev))
    assert top[0]["repeat"] and top[0]["led_before"] and not top[1].get("repeat")
    assert "26.09: С 15 октября" in seen[0]
    assert [x["ref"] for x in writer._fresh_first(top)] == ["news:2", "news:1"]

    async def down(*a, **k):
        raise RuntimeError("нет модели")
    monkeypatch.setattr(nf, "_chat", down)
    fresh = [{"ref": "news:1", "data": {"title": "x"}, "facts": ""}]
    asyncio.run(writer._mark_led_before(fresh, prev))
    assert not fresh[0].get("repeat")
