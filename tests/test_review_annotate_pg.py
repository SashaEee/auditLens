"""Перенос разметки в индекс и ремонт ложного мусора — на живом Postgres
(rag/review_annotate.py). До 04.11.2026 перенос шёл по водяному знаку и терял
записи, а сбой чтения текста записывал жалобу в мусор навсегда.

Запуск: пустая база и ANNOT_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db
pytest tests/test_review_annotate_pg.py — таблицы тест создаёт сам. Без переменной
тесты пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import os

import pytest

from bank_audit import db
from bank_audit.config import ROOT
from bank_audit.rag import bankiru_fts
from bank_audit.rag import review_annotate as RA

PG = os.getenv("ANNOT_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: ANNOT_PG_TEST_URL")

_SCHEMA = """
DROP TABLE IF EXISTS review_annotation, review_index, review_index_state CASCADE;
CREATE TABLE review_index (url text PRIMARY KEY, review_id bigint, source text, bank text,
                           dt timestamptz, product text, channels text[], esc boolean, city text);
CREATE TABLE review_index_state (k text PRIMARY KEY, v text, updated_at timestamptz);
"""
TEXT = "Курьер банка так и не привёз карту, хотя доставку подтверждали трижды, деньги заблокированы."


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(_SCHEMA)
        for f in ("069_review_annotation", "070_review_index_annotation"):
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


def _ann(url, status="agree", kind="complaint", issue="card_delivery", cost=1.0, at="now()"):
    _sql(f"""INSERT INTO review_annotation (url, schema_version, status, kind, issue, product, cost_rub, created_at)
             VALUES (:u, :v, :s, :k, :i, 'debit_card', :c, {at})""",
         {"u": url, "v": RA.SCHEMA, "s": status, "k": kind, "i": issue, "c": cost})


def test_transfer_catches_rows_behind_the_old_watermark(pg):
    for u in ("a", "b", "c"):
        _sql("INSERT INTO review_index (url) VALUES (:u)", {"u": u})
    _ann("a", at="now() - interval '1 hour'")
    _ann("b")
    assert RA.apply_to_index()["applied"] == 2
    assert RA.apply_to_index()["applied"] == 0                         # идемпотентно
    # запись «из прошлого»: транзакция разметки зафиксировалась позже, чем ушёл знак
    _ann("c", at="now() - interval '2 hours'")
    assert RA.apply_to_index()["applied"] == 1
    got = dict(_sql("SELECT url, kind FROM review_index"))
    assert got == {"a": "complaint", "b": "complaint", "c": "complaint"}
    # разметку переписали (повтор после failed) — перенос видит новое время записи
    _sql("UPDATE review_annotation SET kind = 'question', created_at = now() WHERE url = 'a'")
    assert RA.apply_to_index()["applied"] == 1
    assert _sql("SELECT kind FROM review_index WHERE url = 'a'")[0][0] == "question"
    # строки индекса ещё не было — перенесётся, когда появится
    _ann("late")
    assert RA.apply_to_index()["applied"] == 0
    _sql("INSERT INTO review_index (url) VALUES ('late')")
    assert RA.apply_to_index()["applied"] == 1
    assert RA.apply_to_index(full=True)["applied"] == 4


def test_repair_marks_readable_free_junk_as_failed(pg, monkeypatch):
    for u, rid in (("ok", 1), ("css", 2), ("paid", 3), ("down", 4)):
        _sql("INSERT INTO review_index (url, review_id, source) VALUES (:u, :r, 'banki_reviews')",
             {"u": u, "r": rid})
    _ann("ok", status="junk", kind=None, cost=0)          # сбой чтения: текст есть
    _ann("css", status="junk", kind=None, cost=0)         # правда мусор: одни стили
    _ann("paid", status="junk", kind=None, cost=0.4)      # мусор по решению модели — не трогаем
    _ann("down", status="junk", kind=None, cost=0)        # хранилище и сейчас не отвечает

    def fake_bodies(rows):
        out = bankiru_fts.Bodies()
        for r in rows:
            if r["url"] == "down":
                out.failed.add("down")
            elif r["url"] == "css":
                out["css"] = {"text": ".a{color:red} .b{margin:0}"}
            else:
                out[r["url"]] = {"text": TEXT}
        return out
    monkeypatch.setattr(bankiru_fts, "bodies_for", fake_bodies)
    dry = RA.repair_false_junk()
    assert dry == {"junk_free": 3, "readable": 1, "really_junk": 1, "unread": 1, "applied": False}
    assert _sql("SELECT status FROM review_annotation WHERE url = 'ok'")[0][0] == "junk"
    RA.repair_false_junk(apply=True)
    got = dict(_sql("SELECT url, status FROM review_annotation"))
    assert got == {"ok": "failed", "css": "junk", "paid": "junk", "down": "junk"}
