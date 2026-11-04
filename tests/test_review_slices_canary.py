"""Канарейка качества срезов (rag/review_slices_canary.py): БД и сеть не нужны —
модель, тексты, прошлый прогон и запись подставляются."""
from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402

from bank_audit.rag import review_slices as RS  # noqa: E402
from bank_audit.rag import review_slices_canary as C  # noqa: E402
from bank_audit.rag.bankiru_fts import Bodies  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ── подставные модель и тексты ───────────────────────────────────────────────

_MARK = {"курьер": "field", "банкомат": "us", "зарплат": "payroll", "самозанят": "selfemp"}


def _says(model: str, t: str) -> list[str]:
    """A ставит срез по слову; B и третья не считают терминал в магазине УС."""
    t = t.lower()
    keys = [k for w, k in _MARK.items() if w in t]
    if model != RS.STRONG_A and "магазин" in t:
        keys = [k for k in keys if k != "us"]
    return keys


class FakeCaller:
    """Отвечает по словам текста; «молчок» в тексте — модель A эту жалобу пропускает.
    Пишет, какая модель сколько жалоб получила."""

    def __init__(self, says=_says):
        self.says, self.seen, self.sys = says, [], None

    async def ask(self, model, user):
        parts = re.split(r"^\[(\d+)\] ", user, flags=re.M)[1:]
        cards = list(zip(parts[::2], parts[1::2]))
        self.seen.append((model, len(cards)))
        items = []
        for n, body in cards:
            if model == RS.STRONG_A and "молчок" in body:
                continue
            items.append({"n": int(n), "slices": [{"key": k, "clause": "1", "quote": body[:30]}
                                                   for k in self.says(model, body)]})
        return {"items": items}, 1000, 100


def _bodies(texts: dict[str, str], failed: set[str] = frozenset()):
    def fetch(rows):
        out = Bodies()
        for r in rows:
            if r["url"] in failed:
                out.failed.add(r["url"])
            elif r["url"] in texts:
                out[r["url"]] = {"text": texts[r["url"]], "product": None, "city": None}
        return out
    return fetch


def _row(url, rid=1, **labels):
    return {"url": url, "review_id": rid, "source": "bankiru", "bank": "Банк",
            "labels": {k: bool(labels.get(k)) for k in RS.KEYS}}


def _sample():
    """Эталон и тексты: курьеры банка (field), банкоматы (us), терминалы магазинов
    (A ошибается, B и третья — нет), курьеры маркетплейса (ошибаются все — fp field),
    пустые жалобы, плюс пропуски всех видов."""
    gold, texts = [], {}

    def add(url, t, **lab):
        gold.append(_row(url, **lab))
        texts[url] = t
    for i in range(6):
        add(f"https://r/field{i}", f"Курьер банка не привёз карту, номер {i}. Ждала весь день.", field=True)
        add(f"https://r/us{i}", f"Банкомат съел карту, номер {i}. Деньги не выдал.", us=True)
    for i in range(3):
        add(f"https://r/pos{i}", f"Банкомат? Нет, терминал в магазине не принял оплату {i}.")
    for i in range(2):
        add(f"https://r/mkt{i}", f"Курьер маркетплейса опоздал с посылкой {i}, банк ни при чём.")
    for i in range(4):
        add(f"https://r/neg{i}", f"Нагрубили в отделении, очередь два часа, номер {i}.")
    add("https://r/silent", "Курьер банка молчок не приехал, карту так и не получила.", field=True)
    gold.append({**_row("https://r/noindex", field=True), "review_id": None})
    add("https://r/down", "Курьер банка не приехал вовремя, хранилище упало.", field=True)
    add("https://r/empty", "коротко", us=True)
    return gold, texts


@pytest.fixture
def offline(monkeypatch):
    """Канарейка не должна писать разметку: любые обращения к разметке и базе — ошибка."""
    def boom(*a, **k):
        raise AssertionError("канарейка трогает разметку или базу")
    monkeypatch.setattr(RS, "_process", boom)
    monkeypatch.setattr(RS, "_log_spend", boom)
    monkeypatch.setattr(RS.db, "session", boom)
    saved = []
    monkeypatch.setattr(C, "_save", saved.append)
    monkeypatch.setattr(C, "_previous", lambda before: None)
    return saved


# ── выборка ──────────────────────────────────────────────────────────────────

def test_pick_is_fixed_and_balanced():
    rnd = random.Random(7)
    gold = []
    for i in range(400):
        key = RS.KEYS[i % len(RS.KEYS)] if i < 220 else None
        if key == "youth" and i > 60:                    # у одного среза положительных мало
            key = None
        gold.append({"url": f"https://r/{i}", "labels": {k: k == key for k in RS.KEYS}})
    got = C.pick(gold)
    for key in RS.KEYS:
        n = sum(1 for g in got if g["labels"][key])
        assert n == (min(25, sum(1 for g in gold if g["labels"][key])))
    assert sum(1 for g in got if not any(g["labels"].values())) == 40
    assert sum(1 for g in gold if g["labels"]["youth"]) < 25         # берутся все, сколько есть
    shuffled = gold[:]
    rnd.shuffle(shuffled)
    assert [g["url"] for g in C.pick(shuffled)] == [g["url"] for g in got]   # от порядка не зависит
    assert C.pick(gold, per_slice=3, negatives=2) == C.pick(gold, per_slice=3, negatives=2)
    # строки без меток и без адреса не берутся; срез без ключа — не отрицательный пример
    assert C.pick([{"url": "x", "labels": None}, {"url": "", "labels": {}},
                   {"url": "y", "labels": {"nonsense": False}}]) == []


# ── счёт и тревога ───────────────────────────────────────────────────────────

def test_score_counts_only_labelled_and_decided():
    gold = {"a": {"field": True, "us": False}, "b": {"field": False, "us": True},
            "c": {"field": True}, "d": {"field": True, "us": True}}
    got = {"a": {"field": True, "us": True}, "b": {"field": True, "us": True},
           "c": {"field": False, "us": True}}                       # d — без решения каскада
    m = C.score(gold, got)
    assert m["field"] == {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5}
    # у c нет эталона по us — в счёт не идёт
    assert m["us"] == {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0}
    assert m["svo"] == {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None}


def _m(tp, fp, fn=0):
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None}


def test_alerts_floor_drop_and_silence():
    ok = {"field": _m(19, 1), "us": _m(9, 1)}
    assert C.alerts(ok) == [] and C.alerts(ok, ok) == []
    # ниже порога
    r = C.alerts({"field": _m(7, 3)})
    assert len(r) == 1 and "ниже 0,80" in r[0] and RS.LABELS["field"] in r[0]
    # упала больше чем на 0,10 против прошлого (сама выше порога)
    r = C.alerts({"field": _m(17, 3)}, {"field": _m(20, 0)})
    assert len(r) == 1 and "упала с 1,00" in r[0]
    # ровно на 0,10 — не тревога; прошлый прогон без «да» — не с чем сравнивать
    assert C.alerts({"field": _m(17, 3)}, {"field": _m(19, 1)}) == []
    assert C.alerts({"field": _m(17, 3)}, {"field": _m(0, 0, 9)}) == []
    # мало решений «да» — точность шумная, тревоги нет ни по порогу, ни по падению
    assert C.alerts({"field": _m(1, 3)}) == []
    assert C.alerts({"field": _m(3, 1)}, {"field": _m(20, 0)}) == []
    # срез замолчал: положительные есть, «да» нет — точности нет, но это тревога
    r = C.alerts({"svo": _m(0, 0, 6)})
    assert len(r) == 1 and "замолчал" in r[0]
    assert C.alerts({"svo": _m(0, 0, 2)}) == []


# ── каскад ───────────────────────────────────────────────────────────────────

def test_cascade_matches_labelling(monkeypatch):
    """Канарейка обязана решать ровно как разметка: те же ответы моделей — те же срезы."""
    gold, texts = _sample()
    items = [{"url": g["url"], "bank": "Банк", "text": texts[g["url"]]} for g in gold
             if g["url"] in texts and len(texts[g["url"]]) >= 20]
    # часть отрицательных A — в контрольную долю второй модели, как на проде
    monkeypatch.setattr(RS, "confirm_negative", lambda url, share=0.1: url.endswith(("neg1", "neg3")))

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): return None
    monkeypatch.setattr(RS.db, "session", lambda: S())
    for k in range(0, len(items), RS.BATCH):
        part = items[k:k + RS.BATCH]
        rows, rub = asyncio.run(RS._process(FakeCaller(), part))
        got, rub2 = asyncio.run(C._cascade(FakeCaller(), part))
        idx = {x["url"]: i for i, x in enumerate(part)}
        want = {idx[r["url"]]: r["tags"] for r in rows if r["status"] not in C._NO_DECISION}
        assert {i: [key for key in RS.KEYS if v[key]] for i, v in got.items()} == want
        assert rub == pytest.approx(rub2)


# ── прогон ───────────────────────────────────────────────────────────────────

def test_run_scores_against_gold_without_writing_labels(offline):
    gold, texts = _sample()
    c = FakeCaller()
    res = asyncio.run(C.run(gold, caller=c, bodies=_bodies(texts, failed={"https://r/down"}),
                            budget_rub=1000))
    assert c.sys == RS.SYSTEM                                  # промпт разметки
    m = res["metrics"]
    # field: 6 курьеров банка верно, 2 курьера маркетплейса — лишние, «молчок» без решения
    assert m["field"] == {"tp": 6, "fp": 2, "fn": 0, "precision": 0.75, "recall": 1.0}
    # us: терминалы магазинов отсеяли вторая и третья модели
    assert m["us"] == {"tp": 6, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0}
    assert res["missing"] == ["https://r/noindex"] and res["failed"] == ["https://r/down"]
    assert res["empty"] == ["https://r/empty"] and res["unanswered"] == ["https://r/silent"]
    assert res["n"] == 21 and res["stop"] is None and not res["not_reached"]
    assert res["alert"] and any("ниже 0,80" in r for r in res["reasons"])
    assert res["saved"] and len(offline) == 1
    row = offline[0]
    assert row["schema"] == RS.SCHEMA and row["n"] == 21 and row["metrics"] == m
    assert row["rub"] > 0 and "нет в индексе: 1" in row["note"] and "тревога" in row["note"]
    # B — только по положительным A (и контрольной доле), третья — только по спорным
    by = {}
    for model, n in c.seen:
        by[model] = by.get(model, 0) + n
    assert by[RS.STRONG_A] == 22 and by[RS.STRONG_TIE] == 3


def test_run_compares_with_previous(offline, monkeypatch):
    gold, texts = _sample()
    gold = [g for g in gold if "mkt" not in g["url"]]          # без лишних — field чистый
    prev = {"run_at": None, "schema": "s7",
            "metrics": {"us": _m(6, 0), "field": _m(6, 0)}}
    monkeypatch.setattr(C, "_previous", lambda before: prev)

    def worse(model, t):                                       # B и третья перестали отсеивать
        return [k for w, k in _MARK.items() if w in t.lower()]
    res = asyncio.run(C.run(gold, caller=FakeCaller(worse), bodies=_bodies(texts), budget_rub=1000))
    assert res["metrics"]["us"]["precision"] == pytest.approx(6 / 9, abs=1e-4)
    assert any("упала с 1,00" in r and RS.LABELS["us"] in r for r in res["reasons"])
    assert not any(RS.LABELS["field"] in r for r in res["reasons"])


def test_run_stops_at_budget(offline, monkeypatch):
    gold, texts = _sample()
    monkeypatch.setattr(C, "INFLIGHT", 1)
    c = FakeCaller()
    res = asyncio.run(C.run(gold, caller=c, bodies=_bodies(texts), budget_rub=0.5))
    assert res["stop"] == "budget"
    # первый пакет оплачен целиком, дальше — ни одного запроса
    assert [m for m, _n in c.seen][0] == RS.STRONG_A and sum(n for m, n in c.seen if m == RS.STRONG_A) == RS.BATCH
    assert res["n"] + len(res["unanswered"]) == RS.BATCH
    assert len(res["not_reached"]) == 23 - RS.BATCH            # 23 жалобы с текстом
    assert res["saved"] and "не дошли — бюджет" in offline[0]["note"]
    # нулевой бюджет — ни одного запроса, ни одной строки
    c = FakeCaller()
    res = asyncio.run(C.run(gold, caller=c, bodies=_bodies(texts), budget_rub=0))
    assert c.seen == [] and not res["saved"] and len(offline) == 1


def test_run_without_texts_saves_nothing(offline):
    gold, texts = _sample()
    res = asyncio.run(C.run(gold, caller=FakeCaller(), bodies=_bodies({}, failed={g["url"] for g in gold})))
    assert res["n"] == 0 and not res["saved"] and offline == []
    assert len(res["failed"]) == len(gold) - 1                 # без review_id — в missing


# ── тик ──────────────────────────────────────────────────────────────────────

def test_tick_weekly_only_with_gold(monkeypatch):
    marks, runs = [], []
    state = {"last": 0.0, "gold": 0, "res": {"saved": True, "alert": False, "reasons": []}}
    monkeypatch.setattr(C, "_last_at", lambda: state["last"])
    monkeypatch.setattr(C, "_gold_count", lambda: state["gold"])
    monkeypatch.setattr(C, "_mark", marks.append)

    async def fake_run():
        runs.append(1)
        if isinstance(state["res"], Exception):
            raise state["res"]
        return state["res"]
    monkeypatch.setattr(C, "run", fake_run)
    now = 1_800_000_000.0
    assert asyncio.run(C.tick(now)) == {"skipped": "no_gold"} and not marks and not runs
    state["gold"] = 260
    assert asyncio.run(C.tick(now))["saved"] and marks == [now]
    state["last"] = now
    assert asyncio.run(C.tick(now + 6 * 86400)) == {"skipped": "not_due"} and len(runs) == 1
    # не вышло (ни одного решения) — повтор через сутки, а не через неделю и не каждый тик
    state["res"] = {"saved": False}
    later = now + C.EVERY_S
    asyncio.run(C.tick(later))
    assert marks[-1] == later - C.EVERY_S + C.RETRY_S
    state["last"] = marks[-1]
    assert asyncio.run(C.tick(later + 3600)) == {"skipped": "not_due"}
    # упал — тоже повтор через сутки, ошибка идёт наверх (цикл её запишет)
    state["res"] = RuntimeError("модель недоступна")
    with pytest.raises(RuntimeError):
        asyncio.run(C.tick(later + C.RETRY_S))
    assert marks[-1] == later + C.RETRY_S - C.EVERY_S + C.RETRY_S


# ── загрузка эталона ─────────────────────────────────────────────────────────

def test_parse_gold_formats():
    gold = {"gold": {"g1": {"field": {"v": True, "clause": "1"}, "us": {"v": False, "clause": "а"},
                            "typo": {"v": True}},
                     "g2": {"payroll": False, "us": True},
                     "g3": {"field": {"v": False}},
                     "g4": {"nonsense": {"v": True}}},
            "agreeStats": {}}
    sample = [{"id": "g1", "url": "https://r/1", "text": "…"}, {"id": "g2", "url": "https://r/2"},
              {"id": "g4", "url": "https://r/4"}]
    rows, st = C.parse_gold(gold, sample)
    assert {r["url"]: r["labels"] for r in rows} == {
        "https://r/1": {"field": True, "us": False}, "https://r/2": {"payroll": False, "us": True}}
    assert st["no_url"] == ["g3", "g4"] and st["unknown_keys"] == ["nonsense", "typo"]
    assert st["positives"]["field"] == 1 and st["positives"]["us"] == 1 and st["negatives"] == 0
    # выборка словарём, решения без обёртки gold
    rows, _ = C.parse_gold({"g1": {"svo": {"v": True}}}, {"g1": {"url": "https://r/1"}})
    assert rows == [{"url": "https://r/1", "labels": {"svo": True}}]
    rows, _ = C.parse_gold({"g1": {"svo": True}}, {"g1": "https://r/1"})
    assert rows == [{"url": "https://r/1", "labels": {"svo": True}}]


# ── обвязка ──────────────────────────────────────────────────────────────────

def test_migration_and_hooks():
    sql = next((ROOT / "migrations").glob("1[0-9][0-9]_review_slice_gold.sql")).read_text(encoding="utf-8")
    assert "lock_timeout" in sql and "%" not in sql
    assert "CREATE TABLE IF NOT EXISTS review_slice_gold" in sql
    assert "CREATE TABLE IF NOT EXISTS review_slice_canary" in sql and "run_at  timestamptz PRIMARY KEY" in sql
    sched = (ROOT / "src/bank_audit/digest/scheduler.py").read_text(encoding="utf-8")
    assert "await review_slices_canary.tick()" in sched
    tel = (ROOT / "src/bank_audit/web/telemetry.py").read_text(encoding="utf-8")
    assert '"slice_canary": _slice_canary()' in tel


def test_telemetry_block(monkeypatch):
    from bank_audit.web import telemetry as T
    runs = [{"run_at": "2026-11-04T07:00:00+00:00", "metrics": {}, "alert": False}]
    monkeypatch.setattr(C, "history", lambda limit=4: runs[:limit])
    assert T._slice_canary() == runs

    def boom(limit=4):
        raise RuntimeError("relation review_slice_canary does not exist")
    monkeypatch.setattr(C, "history", boom)
    assert T._slice_canary() == []                             # до миграции «Пульс» не падает
