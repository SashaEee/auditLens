"""Канарейка качества срезов на живом Postgres: миграция эталона и прогонов, загрузчик
scripts/load_slice_gold.py, прогон с записью строки, история с тревогой, недельный тик
(rag/review_slices_canary.py). Модель и тексты подставные — сети нет.

Запуск: пустая база и CANARY_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db
pytest tests/test_review_slices_canary_pg.py — таблицы и миграции тест создаёт сам. Без
переменной тесты пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re

import pytest

from bank_audit import db
from bank_audit.config import ROOT
from bank_audit.rag import review_slices as RS
from bank_audit.rag import review_slices_canary as C
from bank_audit.rag.bankiru_fts import Bodies

PG = os.getenv("CANARY_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: CANARY_PG_TEST_URL")

_BASE = """
DROP TABLE IF EXISTS review_slice_canary, review_slice_gold, review_slice_feedback,
                     review_slice_active, review_slice, review_slice_spend, review_index,
                     review_index_state CASCADE;
CREATE TABLE review_index (url text PRIMARY KEY, review_id bigint, source text, bank text,
                           kind text, dt timestamptz, tsv tsvector, channels text[], issue text,
                           product text);
CREATE TABLE review_index_state (k text PRIMARY KEY, v text NOT NULL,
                                 updated_at timestamptz NOT NULL DEFAULT now());
"""


def _migration() -> str:
    return next((ROOT / "migrations").glob("1[0-9][0-9]_review_slice_gold.sql")).read_text(encoding="utf-8")


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(_BASE)
        # разметка — чтобы проверить, что канарейка её не трогает
        for f in ("099_review_slices", "102_review_slices_registry", "105_review_slice_spend"):
            cur.execute((ROOT / "migrations" / f"{f}.sql").read_text(encoding="utf-8"))
        cur.execute(_migration())
        cur.execute(_migration())                              # идемпотентна
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    yield eng
    eng.dispose()


def _sql(sql: str, p: dict | None = None):
    from sqlalchemy import text
    with db.session() as s:
        r = s.execute(text(sql), p or {})
        return r.all() if r.returns_rows else None


# ── подставные модель и тексты ───────────────────────────────────────────────

class FakeCaller:
    """A ставит «курьер» → field, «банкомат» → us; B и третья — так же, но терминал в
    магазине УС не считают (если strict)."""

    def __init__(self, strict=True):
        self.strict, self.sys, self.calls = strict, None, 0

    async def ask(self, model, user):
        self.calls += 1
        parts = re.split(r"^\[(\d+)\] ", user, flags=re.M)[1:]
        items = []
        for n, body in zip(parts[::2], parts[1::2]):
            t = body.lower()
            keys = [k for w, k in (("курьер", "field"), ("банкомат", "us")) if w in t]
            if self.strict and model != RS.STRONG_A and "магазин" in t:
                keys = [k for k in keys if k != "us"]
            items.append({"n": int(n), "slices": [{"key": k, "clause": "1", "quote": ""} for k in keys]})
        return {"items": items}, 2000, 200


_TEXTS = {}
_GOLD = {"gold": {}}
_SAMPLE = []
for _i in range(6):
    for _kind, _text, _lab in (
            ("field", "Курьер банка не привёз карту в срок, номер {i}.", {"field": {"v": True}}),
            ("us", "Банкомат съел карту и не вернул, номер {i}.", {"us": {"v": True}}),
            ("pos", "Банкомат рядом, но терминал в магазине не принял оплату, номер {i}.", {}),
            ("neg", "Нагрубили в отделении и не помогли, номер {i}.", {})):
        _id = f"g-{_kind}-{_i}"
        _url = f"https://r/{_kind}{_i}"
        _GOLD["gold"][_id] = {k: _lab.get(k, {"v": False, "clause": "нет"}) for k in RS.KEYS}
        _SAMPLE.append({"id": _id, "url": _url, "text": "…"})
        _TEXTS[_url] = _text.format(i=_i)


def _fetch(rows):
    out = Bodies()
    for r in rows:
        out[r["url"]] = {"text": _TEXTS[r["url"]], "product": None, "city": None}
    return out


def _load(tmp_path, *args):
    g, s = tmp_path / "gold_set_test.json", tmp_path / "sample.json"
    g.write_text(json.dumps(_GOLD, ensure_ascii=False), encoding="utf-8")
    s.write_text(json.dumps(_SAMPLE, ensure_ascii=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("load_slice_gold", ROOT / "scripts" / "load_slice_gold.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main([str(g), str(s), *args])


def _index_all():
    for k, x in enumerate(_SAMPLE):
        _sql("INSERT INTO review_index (url, review_id, source, bank, kind) VALUES (:u, :i, 'bankiru', 'Банк', 'complaint')",
             {"u": x["url"], "i": 1000 + k})


# ── тесты ────────────────────────────────────────────────────────────────────

def test_tables_shape(pg):
    cols = dict(_sql("""SELECT column_name, data_type FROM information_schema.columns
                         WHERE table_name = 'review_slice_canary'"""))
    assert cols == {"run_at": "timestamp with time zone", "schema": "text", "n": "integer",
                    "metrics": "jsonb", "rub": "numeric", "note": "text"}
    cols = dict(_sql("""SELECT column_name, data_type FROM information_schema.columns
                         WHERE table_name = 'review_slice_gold'"""))
    assert cols == {"url": "text", "labels": "jsonb", "source": "text",
                    "added_at": "timestamp with time zone"}


def test_loader_upserts(pg, tmp_path, capsys):
    assert _load(tmp_path, "--dry-run") == 0
    assert _sql("SELECT count(*) FROM review_slice_gold")[0][0] == 0      # сухой прогон не пишет
    assert "эталон: 24 жалоб" in capsys.readouterr().out
    _index_all()
    _sql("DELETE FROM review_index WHERE url = 'https://r/neg5'")
    assert _load(tmp_path) == 0
    out = capsys.readouterr().out
    assert "записано: 24; в таблице: 24, из них в индексе отзывов: 23" in out
    assert "field 6" in out and "без единого среза: 12" in out
    got = dict(_sql("SELECT url, labels FROM review_slice_gold"))
    assert got["https://r/field0"]["field"] is True and got["https://r/pos0"]["us"] is False
    assert set(got["https://r/field0"]) == set(RS.KEYS)
    assert {r[0] for r in _sql("SELECT DISTINCT source FROM review_slice_gold")} == {"gold_set_test"}
    # повторная загрузка с исправленной меткой: строк столько же, метка новая
    _GOLD["gold"]["g-pos-0"]["us"] = {"v": True}
    try:
        _load(tmp_path, "--source", "gold-v2")
    finally:
        _GOLD["gold"]["g-pos-0"]["us"] = {"v": False, "clause": "нет"}
    assert _sql("SELECT count(*) FROM review_slice_gold")[0][0] == 24
    assert _sql("SELECT labels->>'us', source FROM review_slice_gold WHERE url = 'https://r/pos0'")[0] == ("true", "gold-v2")


def test_run_writes_canary_not_labels(pg, tmp_path):
    _index_all()
    _load(tmp_path)
    r1 = asyncio.run(C.run(caller=FakeCaller(), bodies=_fetch, budget_rub=1000))
    assert r1["saved"] and r1["n"] == 24 and not r1["missing"] and r1["prev"] is None
    assert r1["metrics"]["us"] == {"tp": 6, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0}
    assert r1["metrics"]["field"]["precision"] == 1.0 and not r1["alert"]
    # разметка и её траты не тронуты
    assert _sql("SELECT count(*) FROM review_slice")[0][0] == 0
    assert _sql("SELECT count(*) FROM review_slice_spend")[0][0] == 0
    row = _sql("SELECT schema, n, metrics, rub, note FROM review_slice_canary")[0]
    assert row[0] == RS.SCHEMA and row[1] == 24 and row[2] == r1["metrics"]
    assert float(row[3]) == r1["rub"] > 0 and row[4].startswith("выборка 24, решений 24")
    # вторая неделя: вторая модель перестала отсеивать терминалы магазинов
    r2 = asyncio.run(C.run(caller=FakeCaller(strict=False), bodies=_fetch, budget_rub=1000))
    assert r2["prev"] == r1["run_at"]
    assert r2["metrics"]["us"]["precision"] == 0.5 and r2["alert"]
    assert any("упала с 1,00" in x for x in r2["reasons"])
    # история для «Пульса»: свежий первым, тревога — против предыдущего прогона
    h = C.history(4)
    assert [x["run_at"] for x in h] == [r2["run_at"], r1["run_at"]]
    assert h[0]["alert"] and h[0]["reasons"] == r2["reasons"] and not h[1]["alert"]
    assert h[0]["metrics"] == r2["metrics"] and isinstance(h[0]["rub"], float)
    assert len(C.history(1)) == 1 and C.history(1)[0]["alert"]               # тревога и без «хвоста»
    json.dumps(h)                                                            # уходит в «Пульс» как есть


def test_tick_weekly_on_state(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_caller", FakeCaller)
    monkeypatch.setattr(C, "_fetch", _fetch)
    now = 1_800_000_000.0
    # эталона нет — пропуск, метка не ставится
    assert asyncio.run(C.tick(now)) == {"skipped": "no_gold"}
    assert _sql("SELECT count(*) FROM review_index_state")[0][0] == 0
    _index_all()
    _load(tmp_path)
    res = asyncio.run(C.tick(now))
    assert res["saved"] and res["n"] == 24
    assert _sql("SELECT v FROM review_index_state WHERE k = 'review_slices_canary_at'")[0][0] == f"{now:.0f}"
    assert asyncio.run(C.tick(now + 6 * 86400)) == {"skipped": "not_due"}
    assert _sql("SELECT count(*) FROM review_slice_canary")[0][0] == 1
    assert asyncio.run(C.tick(now + 7 * 86400))["saved"]
    assert _sql("SELECT count(*) FROM review_slice_canary")[0][0] == 2
    # без таблицы эталона (миграция не накатана) — тоже «нет эталона», а не падение
    _sql("DROP TABLE review_slice_gold")
    assert asyncio.run(C.tick(now + 30 * 86400)) == {"skipped": "no_gold"}
