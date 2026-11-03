"""Реестр срезов на живом Postgres: версия по срезу, откат к прежней, отметки
«не относится», основание для карточки (rag/review_slices.py, миграции 099/102).
SQL здесь — главное: CTE _DESIRED и выбор основания по активной версии.

Запуск: пустая база и SLICES_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db
pytest tests/test_review_slices_pg.py — таблицы и миграции тест создаёт сам. Без
переменной тесты пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import json
import os

import pytest

from bank_audit import db
from bank_audit.config import ROOT
from bank_audit.rag import review_slices as RS

PG = os.getenv("SLICES_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: SLICES_PG_TEST_URL")

# индекс в тесте — только нужные срезам столбцы
_INDEX = """
DROP TABLE IF EXISTS review_slice_feedback, review_slice_active, review_slice, review_index,
                     review_annotation CASCADE;
CREATE TABLE review_index (url text PRIMARY KEY, kind text, dt timestamptz, tsv tsvector,
                           channels text[], issue text, product text, bank text);
CREATE TABLE review_annotation (url text, summary text, created_at timestamptz DEFAULT now());
"""


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(_INDEX)
        for f in ("099_review_slices", "102_review_slices_registry"):
            cur.execute((ROOT / "migrations" / f"{f}.sql").read_text(encoding="utf-8"))
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


def _slice(url, schema, tags, status="agree", evidence=None, quote=None):
    _sql("""INSERT INTO review_slice (url, schema, tags, status, evidence, quote)
            VALUES (:u, :s, :t, :st, CAST(:e AS jsonb), :q)""",
         {"u": url, "s": schema, "t": tags, "st": status,
          "e": json.dumps(evidence, ensure_ascii=False) if evidence else None, "q": quote})


def _tags(url):
    return _sql("SELECT slices FROM review_index WHERE url = :u", {"u": url})[0][0]


def test_migration_moves_pk_to_url_schema(pg):
    got = _sql("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conname = 'review_slice_pkey'""")[0][0]
    assert got == "PRIMARY KEY (url, schema)"
    act = dict(_sql("SELECT key, schema FROM review_slice_active"))
    assert act == {"payroll": "s6", "us": "s6", "field": "s6",
                   "selfemp": "s7", "youth": "s7", "svo": "s7"}
    assert RS.active() == act
    # фильтр продукта знает все срезы реестра
    for key, (label, _d) in RS.SLICES.items():
        assert _sql("SELECT al_product_is('x', ARRAY[:k], :w)", {"k": key, "w": label})[0][0], key


def test_index_takes_active_version_per_key_with_fallback(pg):
    for u in ("u1", "u2", "u3"):
        _sql("INSERT INTO review_index (url, kind) VALUES (:u, 'complaint')", {"u": u})
    # u1: s6 говорит field, s7 — нет; field активен на s6 → field остаётся
    _slice("u1", "s6", ["field"])
    _slice("u1", "s7", [])
    # u2: s7 говорит selfemp (активен на s7) и payroll (активен на s6, а s6 — нет) → только selfemp
    _slice("u2", "s6", [])
    _slice("u2", "s7", ["selfemp", "payroll"])
    # u3: есть только s6 с payroll, а s7 у него не размечен
    _slice("u3", "s6", ["payroll"])
    assert RS.apply_to_index() == 3
    assert _tags("u1") == ["field"] and _tags("u2") == ["selfemp"] and _tags("u3") == ["payroll"]
    # переключили field и payroll на s7: u1 теряет field (s7 сказал «нет»), u2 получает
    # payroll, у u3 s7 ещё нет — payroll берётся из прежней версии, жалоба не выпадает
    _sql("UPDATE review_slice_active SET schema = 's7' WHERE key IN ('field', 'payroll')")
    RS.apply_to_index()
    assert _tags("u1") == [] and _tags("u2") == ["payroll", "selfemp"] and _tags("u3") == ["payroll"]
    assert RS.apply_to_index() == 0                          # идемпотентно
    # неудавшаяся разметка активной версии не считается — откат к прежней
    _slice("u3", "s7", [], status="failed_tie")
    RS.apply_to_index()
    assert _tags("u3") == ["payroll"]


def test_evidence_follows_active_version(pg):
    _sql("INSERT INTO review_index (url, kind) VALUES ('e1', 'complaint'), ('e2', 'complaint')")
    _slice("e1", "s6", ["field"], quote="курьер не приехал")
    _slice("e1", "s7", ["field", "svo"], evidence={
        "field": {"clause": "1", "quote": "курьер так и не привёз карту", "quote_ok": True},
        "svo": {"clause": "2", "quote": "выдумано моделью", "quote_ok": False}})
    _slice("e2", "s6", ["us"], quote="банкомат съел карту")
    RS.apply_to_index()
    ev = RS.evidence_for(["e1", "e2", "нет такого"])
    # field активен на s6 → основание s6 (общая цитата строки), svo — из s7, но
    # цитата не сверилась с текстом — не показывается
    e1 = {x["key"]: x for x in ev["e1"]}
    assert e1["field"]["schema"] == "s6" and e1["field"]["quote"] == "курьер не приехал"
    assert e1["svo"]["clause"] == "2" and e1["svo"]["quote"] is None
    assert [x["key"] for x in ev["e1"]] == ["field", "svo"]               # порядок реестра
    assert ev["e2"][0]["label"] == RS.LABELS["us"] and "нет такого" not in ev
    _sql("UPDATE review_slice_active SET schema = 's7' WHERE key = 'field'")
    RS.apply_to_index()
    e1 = {x["key"]: x for x in RS.evidence_for(["e1"])["e1"]}
    assert e1["field"]["schema"] == "s7" and e1["field"]["quote"] == "курьер так и не привёз карту"


def test_flag_needs_team_decision_and_exclusion_sticks(pg):
    _sql("INSERT INTO review_index (url, kind, dt) VALUES ('f1', 'complaint', now())")
    _slice("f1", "s6", ["field", "us"])
    RS.apply_to_index()
    assert RS.flag("f1", "field", "auditor1", "это курьер маркетплейса")
    assert RS.flag("f1", "field", "auditor2", None)
    assert not RS.flag("f1", "nonsense", "auditor1")
    # отметка сама срез не снимает
    RS.apply_to_index(urls=["f1"])
    assert _tags("f1") == ["field", "us"]
    ev = {x["key"]: x for x in RS.evidence_for(["f1"])["f1"]}
    assert ev["field"]["flagged"] == 2 and ev["us"]["flagged"] == 0
    flags = RS.open_flags()
    assert {f["username"] for f in flags} == {"auditor1", "auditor2"}
    assert flags[0]["label"] == RS.LABELS["field"] and flags[0]["evidence"]["key"] == "field"
    # решение по одной отметке закрывает обе и сразу снимает срез в индексе
    got = RS.decide_flag(flags[0]["id"], "excluded", "team")
    assert got == {"url": "f1", "key": "field", "decision": "excluded"}
    assert _tags("f1") == ["us"] and RS.open_flags() == []
    # переразметка срез не вернёт, повторная отметка решение не откроет
    _slice("f1", "s7", ["field", "us"])
    _sql("UPDATE review_slice_active SET schema = 's7' WHERE key IN ('field', 'us')")
    RS.apply_to_index()
    assert _tags("f1") == ["us"]
    RS.flag("f1", "field", "auditor1", "ещё раз")
    assert RS.open_flags() == []
    assert RS.decide_flag(10 ** 9, "excluded", "team") is None
    assert RS.decide_flag(flags[0]["id"], "maybe", "team") is None


def test_kept_decision_leaves_slice_and_reflag_reopens(pg):
    _sql("INSERT INTO review_index (url, kind) VALUES ('k1', 'complaint')")
    _slice("k1", "s6", ["payroll"])
    RS.apply_to_index()
    RS.flag("k1", "payroll", "auditor1")
    fid = RS.open_flags()[0]["id"]
    RS.decide_flag(fid, "kept", "team")
    assert _tags("k1") == ["payroll"] and RS.open_flags() == []
    RS.flag("k1", "payroll", "auditor1", "всё-таки не зарплатный")
    assert [f["note"] for f in RS.open_flags()] == ["всё-таки не зарплатный"]


def test_coverage_per_key(pg):
    _sql("""INSERT INTO review_index (url, kind, dt, tsv) VALUES
            ('c1', 'complaint', now() - interval '1 day', to_tsvector('russian', 'курьер не приехал')),
            ('c2', 'complaint', now() - interval '1 day', to_tsvector('russian', 'курьер опоздал')),
            ('c3', 'complaint', now() - interval '1 day', to_tsvector('russian', 'самозанятый налог'))""")
    _slice("c1", "s6", ["field"])
    _slice("c3", "s7", ["selfemp"])
    cov = RS.coverage(30)
    assert cov["keys"]["field"]["candidates"] == 2 and cov["keys"]["field"]["tagged"] == 1
    assert cov["keys"]["field"]["ready"] is False and cov["keys"]["selfemp"]["ready"] is True
    assert cov["keys"]["youth"] == {"candidates": 0, "tagged": 0, "share": 1.0, "ready": True, "schema": "s7"}
    assert cov["ready"] is False


def test_status_for_pulse(pg):
    _sql("INSERT INTO review_index (url, kind, dt, tsv) VALUES ('s1', 'complaint', now(), to_tsvector('russian', 'курьер'))")
    _slice("s1", "s6", ["field"])
    RS.flag("s1", "field", "auditor1")
    st = RS.status()
    by = {k["key"]: k for k in st["keys"]}
    assert set(by) == set(RS.KEYS) and by["field"]["schema"] == "s6" and by["field"]["open_flags"] == 1
    assert by["field"]["gold"]["precision"] == RS.GOLD_0411["field"][0] and by["field"]["candidates"] == 1
    assert st["spend"]["s6"]["rows"] == 1
