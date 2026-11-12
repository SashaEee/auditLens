"""Долгое ожидание: отбор кандидатов словами, чтение моделью, дословная цитата, таблица отделений."""
from __future__ import annotations

import asyncio
import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from bank_audit.vsp.models import Review
from bank_audit.vsp.signals import llm_read as lr
from bank_audit.vsp.signals import wait as w
from bank_audit.vsp.storage import Store

UTC = timezone.utc
D = datetime(2026, 5, 1, tzinfo=UTC)
QUEUE = "Из 15 окошек работает 4,снять деньги ждать полтора часа, не рекомендую это отделение никому"
TALON = "Взял талон, пригласили к окну, а там нахамили и отказали без объяснения причин, ужасное отношение"
FRAUD = "Оформили кредит без моего согласия, менеджер обманула, теперь долг 50 тысяч рублей, верните деньги"


class FakeReader(lr.Reader):
    def __init__(self, model="fake", system=""):
        self.model, self.system, self.rub, self.calls, self.errors = model, system, 0.0, 0, 0
        self.drop, self.sem = set(), asyncio.Semaphore(4)

    async def ask(self, user, system=None):
        self.calls += 1
        self.rub += 0.2
        items = []
        for k, chunk in enumerate(user.split("\n\n[")[0:] if user.startswith("[") else user.split("\n\n[")[1:]):
            hit = "ждать полтора часа" in chunk
            items.append({"n": k + 1, "wait": hit, "minutes": 90 if hit else None, "cause": "Мало окон",
                          "left": False, "quote": "работает 4, снять деньги ждать полтора часа" if hit else None})
        return {"items": items}, 0.2


def test_normalize_and_words():
    assert w.normalize({"wait": True, "minutes": "90", "cause": "Мало окон", "left": "true", "quote": "ждал"}) == {
        "wait": 1, "minutes": 90, "cause": "мало окон", "left": 1, "quote": "ждал"}
    assert w.normalize({"wait": True, "minutes": "полтора часа", "cause": "выдумка"})["minutes"] is None
    assert w.normalize({"wait": True, "cause": "выдумка"})["cause"] == "не сказано"
    assert w.normalize({"wait": False, "minutes": 30}) == {"wait": 0, "minutes": None, "cause": None, "left": 0,
                                                            "quote": None}
    assert w.normalize({"minutes": 30}) is None and w.normalize(None) is None
    assert w.normalize({"wait": True, "minutes": 100000})["minutes"] is None        # больше суток — не время ожидания
    assert w.RX.search("Отвратительное невыносимо долгое ожидание") and w.RX.search("Простояли два часа")
    assert not w.RX.search("стоимость услуги высокая") and not w.RX.search(FRAUD)
    assert set(w.CAUSES) >= {"мало окон", "касса или проверка", "не сказано"}


def test_excerpt_is_verbatim_text():
    t = "Сделали ремонт,всё красиво,но толку нет.сотрудников мало,просидела в очереди 40 минут. Организации ноль."
    got = w.excerpt(t, "сотрудников мало, просидела в очереди 40 минут")      # модель поставила пробел после запятой
    assert got == "сотрудников мало,просидела в очереди 40 минут." and got in t
    assert w.excerpt(t, "такого в отзыве нет совсем") == got                    # не нашли — предложение со словом об ожидании
    long = "слово " * 40 + "ждал час " + "дальше " * 120
    cut = w.excerpt(long, None)
    assert cut.startswith("… ") and cut.endswith(" …") and "ждал час" in cut and len(cut) < 330
    assert w.excerpt("Долгое ожидание", "Долгое ожидание") == "Долгое ожидание"
    assert w.excerpt("Талон на установку, ожидание более часа (!) Ужас.", "ожидание более часа") == \
        "Талон на установку, ожидание более часа (!)"
    assert w.excerpt("Ждали час!!! Потом ушли.", "Ждали час") == "Ждали час!!!"
    assert w.excerpt("", None) == ""


def test_run_reads_only_candidates_once(db_url, monkeypatch):
    st = Store(db_url)
    old = datetime(2022, 5, 1, tzinfo=UTC)
    st.save_reviews([
        Review("2gis", "q0", "g1", 1, QUEUE, D, author_ref="a0"),
        Review("yandex", "q1", "y1", 2, QUEUE, D, author_ref="a1"),          # тот же текст — читается один раз
        Review("2gis", "t0", "g1", 1, TALON, D, author_ref="a2"),            # слово есть, жалобы на время нет
        Review("2gis", "f0", "g1", 1, FRAUD, D, author_ref="a3"),            # слов об ожидании нет — не кандидат
        Review("2gis", "p0", "g1", 5, QUEUE, D, author_ref="a4"),            # пятёрка — не жалоба
        Review("2gis", "o0", "g1", 1, QUEUE + " давно", old, author_ref="a5"),   # старше границы чтения
    ])
    monkeypatch.setattr(lr, "Reader", FakeReader)
    res = asyncio.run(w.run(st, limit=100))
    assert res["read_unique"] == 2 and res["saved"] == 3 and res["hits"] == 1 and res["waiting"] == 0
    with st.engine.connect() as c:
        rows = {r.review_id: r for r in c.execute(select(lr.review_llm).where(
            lr.review_llm.c.prompt_version == w.VERSION))}
    assert set(rows) == {"2gis:q0", "yandex:q1", "2gis:t0"}
    hit = rows["2gis:q0"]
    data = json.loads(hit.data_json)
    assert hit.relevant == 1 and hit.staff == "мало окон" and hit.scheme == "90" and data["minutes"] == 90
    assert data["quote_ok"] is False                       # модель «причесала» цитату — дословной её не считаем
    assert rows["yandex:q1"].relevant == 1 and rows["yandex:q1"].cost_rub == 0.0
    assert rows["2gis:t0"].relevant == 0
    again = asyncio.run(w.run(st, limit=100))
    assert again["read_unique"] == 0 and again["calls"] == 0 and w.unread(st) == 0


def test_run_without_candidates_needs_no_model(db_url):
    st = Store(db_url)
    st.save_reviews([Review("2gis", "f0", "g1", 1, FRAUD, D, author_ref="a3")])
    res = asyncio.run(w.run(st, limit=100))                # ключей модели нет — и не понадобились
    assert res["read_unique"] == 0 and res["rub"] == 0.0


def test_load_dedupes_and_counts_complaints(db_url, monkeypatch):
    st = Store(db_url)
    st.save_reviews([
        Review("2gis", "q0", "g1", 1, QUEUE, D, author_ref="a0"),
        Review("2gis", "q1", "g1", 2, QUEUE, D + timedelta(days=1), author_ref="a1"),    # тот же текст, то же отделение
        Review("2gis", "q2", "g2", 1, QUEUE, D, author_ref="a2"),                         # тот же текст, другое отделение
        Review("2gis", "t0", "g1", 1, TALON, D, author_ref="a3"),
        Review("2gis", "f0", "g1", 3, FRAUD, D, author_ref="a4"),
        Review("2gis", "p0", "g1", 5, "Всё отлично, очередей нет совсем, спасибо большое сотрудникам", D),
    ])
    monkeypatch.setattr(lr, "Reader", FakeReader)
    asyncio.run(w.run(st, limit=100))
    d = w.load(st.engine)
    assert [h["point"] for h in d["hits"]] in (["2gis:g1", "2gis:g2"], ["2gis:g2", "2gis:g1"])
    h = next(x for x in d["hits"] if x["point"] == "2gis:g1")
    assert h["minutes"] == 90 and h["cause"] == "мало окон" and h["left"] is False and h["src"] == "2gis"
    assert d["neg"]["2gis:g1"]["2026-05"] == 4 and d["neg"]["2gis:g2"]["2026-05"] == 1      # пятёрка в жалобы не идёт
    assert d["read"] == 4 and d["waiting"] == 0
    t = w.branch_table(d["hits"], d["neg"], m0="2026-01")
    row = next(r for r in t["rows"] if r["point"] == "2gis:g1")
    assert row["n"] == 1 and row["neg"] == 4 and row["long"] == 1 and row["median"] == 90
    assert t["total"] == 2 and t["neg"] == 5
    assert w.branch_table(d["hits"], d["neg"], m0="2026-06")["rows"] == []                  # период позже — пусто


def _hit(point, at, minutes=None, left=False, cause="не сказано"):
    return {"rid": f"{point}:{at.isoformat()}:{random.random()}", "point": point, "at": at, "minutes": minutes,
            "left": left, "cause": cause, "text": "", "quote": ""}


def test_branch_table_marks_real_excess_only():
    rng = random.Random(7)
    neg: dict = {}
    hits = []
    for i in range(400):                                    # фон: у каждого отделения 30 % жалоб про ожидание
        p = f"b{i}"
        n = rng.randint(3, 40)
        neg[p] = Counter({"2026-03": n})
        hits += [_hit(p, datetime(2026, 3, 5, tzinfo=UTC)) for _ in range(sum(rng.random() < 0.3 for _ in range(n)))]
    neg["bad"] = Counter({"2026-03": 40})                    # отделение, где ждут все
    hits += [_hit("bad", datetime(2026, 3, 9, tzinfo=UTC), minutes=60 + i, left=i % 4 == 0, cause="мало окон")
             for i in range(34)]
    t = w.branch_table(hits, neg, m0="2026-01")
    marked = {r["point"] for r in t["rows"] if r["sig"]}
    assert "bad" in marked and len(marked) <= 3 and t["tested"] == 401
    bad = next(r for r in t["rows"] if r["point"] == "bad")
    assert bad["long"] == 34 and bad["left"] == 9 and bad["cause"] == "мало окон" and bad["ratio"] > 2
    assert 0.28 < t["share"] < 0.36


def test_uniform_background_rarely_marks_branches():
    """На ровном фоне отметок почти нет: поправка идёт по всем отделениям с жалобами."""
    total = 0
    for seed in range(12):
        rng = random.Random(seed)
        neg, hits = {}, []
        for i in range(600):
            p = f"b{i}"
            n = rng.randint(1, 30)
            neg[p] = Counter({"2026-03": n})
            hits += [_hit(p, datetime(2026, 3, 5, tzinfo=UTC)) for _ in range(sum(rng.random() < 0.3 for _ in range(n)))]
        total += w.branch_table(hits, neg, m0="2026-01")["sig"]
    assert total <= 6                                         # в среднем не больше половины отметки на прогон
