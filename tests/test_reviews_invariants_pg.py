"""Инвариант «Аудита отзывов» на живом Postgres: один и тот же фильтр даёт одно и то
же число на плитке темы, в счётчике ленты, в строках Excel и в ответе ИИ; копии с
другой площадки и неразмеченные свежие учитываются везде одинаково.

Запуск: пустая база и INV_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db
pytest tests/test_reviews_invariants_pg.py — таблицы тест создаёт сам. Без
переменной тесты пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from bank_audit import db
from bank_audit.config import ROOT
from bank_audit.rag import bankiru_fts, review_annotate as RA, review_period as RP
from bank_audit.rag import reviews_dash as RD

PG = os.getenv("INV_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: INV_PG_TEST_URL")
B = "Сбербанк"
FIELD = "Выездной сервис — курьеры и выезд"

_SCHEMA = """
DROP TABLE IF EXISTS review_slice_feedback, review_slice_active, review_slice, review_index,
                     review_annotation, review_index_state CASCADE;
CREATE TABLE review_index (url text PRIMARY KEY, review_id bigint, source text, bank text, product text,
                           dt timestamptz, city text, rating real, kind text, issue text, issues2 text[],
                           esc boolean, ev_date date, slices text[], channels text[], dup_of text,
                           ann_at timestamptz, tsv tsvector);
CREATE TABLE review_index_state (k text PRIMARY KEY, v text, updated_at timestamptz);
"""


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(_SCHEMA)
        for f in ("069_review_annotation", "099_review_slices", "102_review_slices_registry",
                  "109_product_directions"):
            cur.execute((ROOT / "migrations" / f"{f}.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    monkeypatch.setattr(RD, "resolve_bank", lambda b: b if b else None)
    monkeypatch.setattr(RD, "_cached", lambda key, f: f())
    monkeypatch.setattr(bankiru_fts, "_source_engine", lambda: None)
    now = datetime.now(timezone.utc)
    rows = []
    # 12 жалоб «chargeback» (2 из них — копии с sravni), 7 «fees», 3 смешанные «fees»,
    # 4 похвалы, 5 свежих неразмеченных; 4 жалобы в срезе «выездной», одна из них копия
    plan = ([("chargeback", "complaint", None)] * 10 + [("chargeback", "complaint", "dup")] * 2
            + [("fees", "complaint", None)] * 7 + [("fees", "mixed", None)] * 3
            + [(None, "praise", None)] * 4 + [(None, None, None)] * 5)
    for k, (issue, kind, dup) in enumerate(plan):
        url = f"https://r/{k}"
        src = "sravni_reviews" if dup else "bankiru"
        sl = ["field"] if k in (0, 1, 12, 10) else None
        rows.append({"u": url, "rid": k + 1, "src": src, "dt": now - timedelta(days=1 + k % 50),
                     "kind": kind, "issue": issue, "dup": "https://r/0" if dup else None, "sl": sl,
                     "at": now if kind else None})
    with eng.begin() as c:
        c.execute(text("""INSERT INTO review_index (url, review_id, source, bank, product, dt, kind, issue,
                                                    issues2, esc, slices, dup_of, ann_at)
                          VALUES (:u, :rid, :src, 'Сбербанк', 'Дебетовая карта', :dt, :kind, :issue,
                                  '{}', false, :sl, :dup, :at)"""), rows)
        c.execute(text("""INSERT INTO review_annotation (url, schema_version, status, kind, issue, product,
                                                         summary, quote, quote_ok, created_at)
                          SELECT url, :v, 'agree', kind, issue, 'debit_card', 'пересказ', 'цитата', true, ann_at
                            FROM review_index WHERE kind IS NOT NULL"""), {"v": RA.SCHEMA})
        c.execute(text("""INSERT INTO review_slice (url, schema, tags, status)
                          SELECT url, 's6', slices, 'agree' FROM review_index WHERE slices IS NOT NULL"""))
    yield eng
    eng.dispose()


def test_theme_tile_feed_excel_and_ai_agree(pg, monkeypatch):
    per = RP.parse("90d")
    tiles = {t["key"]: t["n"] for t in RD.themes(B, period=per)["themes"]}
    assert tiles == {"chargeback": 10, "fees": 10}             # копии и похвала не считаются
    for theme, n in tiles.items():
        feed = RD.list_reviews_ex(B, theme=theme, period=per, limit=5)
        rows = RD.export_rows(B, theme=theme, period=per)
        assert feed["total"] == n == len(rows), theme
        assert all(r["разметка"] == "" for r in rows)            # с темой — только размеченные
    # без темы: лента = размеченные жалобы + свежие на разметке = строки Excel
    feed = RD.list_reviews_ex(B, period=per, limit=5)
    rows = RD.export_rows(B, period=per)
    assert (feed["total"], feed["pending"]) == (20, 5)
    assert len(rows) == 25 and sum(1 for r in rows if r["разметка"] == "на разметке") == 5
    # ИИ называет то же число, что плитка
    from bank_audit.ai import agent_tools as T
    from bank_audit.rag import reviews_work as RW
    monkeypatch.setattr(RW, "clusters", lambda *a, **k: {"clusters": []})
    monkeypatch.setattr(RD, "weekly_signals", lambda *a, **k: {})
    monkeypatch.setattr(RD, "week_pulse", lambda *a, **k: {})
    res = json.loads(T.tool_complaint_theme("chargeback", days=90))
    assert res["total"] == tiles["chargeback"]


def test_slice_product_counts_agree(pg):
    got = RD.products(B, 365, top=60)
    n = {x["product"]: x["n"] for x in got["items"]}
    assert n.get(FIELD) == 3                                    # копия из среза не считается
    feed = RD.list_reviews_ex(B, product=FIELD, period=RP.parse("365d"), limit=5)
    rows = RD.export_rows(B, product=FIELD, period=RP.parse("365d"))
    assert feed["total"] == 3 and len([r for r in rows if r["разметка"] == ""]) == 3
    # заметка «срез стал строже» — только своя («Срез…») и свежая
    assert got["slice_notes"] == {}
    from sqlalchemy import text
    with db.session() as s:
        s.execute(text("UPDATE review_slice_active SET note = 'Срез стал строже', switched_at = now() WHERE key = 'field'"))
        s.execute(text("UPDATE review_slice_active SET note = 'Срез давний', switched_at = now() - interval '90 days' "
                       "WHERE key = 'us'"))
    assert RD.products(B, 365, top=60)["slice_notes"] == {FIELD: "Срез стал строже"}


def test_copy_counts_when_its_canonical_is_not_a_complaint(pg):
    """Копия выпадает из чисел, только если её каноническая — жалоба: каноническую,
    размеченную мусором или ещё не размеченную, заменяет копия (ревью 04.11.2026)."""
    from sqlalchemy import text
    per = RP.parse("90d")
    assert {t["key"]: t["n"] for t in RD.themes(B, period=per)["themes"]}["chargeback"] == 10
    with db.session() as s:
        s.execute(text("UPDATE review_index SET kind = 'junk' WHERE url = 'https://r/0'"))
    n = {t["key"]: t["n"] for t in RD.themes(B, period=per)["themes"]}["chargeback"]
    assert n == 11                       # 9 своих + 2 копии, чья каноническая теперь мусор
    assert RD.list_reviews_ex(B, theme="chargeback", period=per, limit=5)["total"] == 11


def test_direction_filter_matches_its_product_or_slice(pg):
    """Направление 'dir:<код>' считает то же, что его продукт или срез, — на плитке,
    в ленте и в Excel (миграция 109, directions.py)."""
    per = RP.parse("365d")
    feed_dir = RD.list_reviews_ex(B, product="dir:field", period=per, limit=5)["total"]
    feed_slice = RD.list_reviews_ex(B, product=FIELD, period=per, limit=5)["total"]
    assert feed_dir == feed_slice == 3
    got = RD.products(B, 365, top=60)
    dirs = {d["product"]: d["n"] for d in got["directions"]}
    assert dirs["dir:field"] == 3 and "dir:mortgage" in dirs
    rows = RD.export_rows(B, product="dir:field", period=per)
    assert len([r for r in rows if r["разметка"] == ""]) == 3
    assert RD.data_start_for("dir:field") == RD.data_start_for(FIELD)

