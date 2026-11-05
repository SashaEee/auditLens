"""Новые поля «Пульса» v2 на живом Postgres: SQL лент состояния служб, активности за
14 дней, прошлого периода и оценок по дням выполняется по-настоящему (часовой пояс,
наивный dt площадки, generate_series, FILTER, UNION с признаком успеха).

Запуск: пустая база и PULSE_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db
pytest tests/test_pulse_v2_api_pg.py — таблицы тест создаёт сам (только нужные столбцы).
Без переменной тесты пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

from bank_audit import db
from bank_audit.clock import MSK
from bank_audit.web import telemetry as T

PG = os.getenv("PULSE_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: PULSE_PG_TEST_URL")

_DDL = """
DROP TABLE IF EXISTS usage_event, user_event, app_user, report, item_feedback, daily_digest,
    daily_digest_archive, review_index, review, news_item, review_slice_gold,
    review_slice_canary, document_origin CASCADE;
CREATE TABLE usage_event (id bigserial PRIMARY KEY, username text, kind text NOT NULL, page text,
    dur_ms integer, status integer, payload jsonb, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE user_event (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username text NOT NULL, ts timestamptz NOT NULL DEFAULT now(), kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE app_user (username text PRIMARY KEY, display_name text,
    prefs jsonb NOT NULL DEFAULT '{}'::jsonb, interests jsonb NOT NULL DEFAULT '{}'::jsonb,
    profile_note text, profile_note_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE report (report_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username text NOT NULL, question text NOT NULL DEFAULT '', title text,
    body text NOT NULL DEFAULT '', payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    banks text[] NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE item_feedback (id bigserial PRIMARY KEY, username text NOT NULL, kind text NOT NULL,
    item_key text NOT NULL, verdict smallint NOT NULL, payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (username, kind, item_key));
CREATE TABLE daily_digest (digest_date date NOT NULL, section text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb, status text NOT NULL DEFAULT 'ok',
    generated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (digest_date, section));
CREATE TABLE daily_digest_archive (digest_date date NOT NULL, section text NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text, generated_at timestamptz, PRIMARY KEY (digest_date, section, archived_at));
CREATE TABLE review_index (url text PRIMARY KEY, source text NOT NULL DEFAULT 'bankiru',
    dt timestamp, kind text, ingested_at timestamptz DEFAULT now());
CREATE TABLE review (id bigserial PRIMARY KEY, source text NOT NULL, posted_at timestamptz);
CREATE TABLE news_item (id bigserial PRIMARY KEY, source text NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now());
CREATE TABLE review_slice_gold (url text PRIMARY KEY, labels jsonb NOT NULL DEFAULT '{}',
    added_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE review_slice_canary (run_at timestamptz PRIMARY KEY, schema text NOT NULL DEFAULT 'v',
    n int NOT NULL DEFAULT 0, metrics jsonb NOT NULL DEFAULT '{}', rub numeric(10, 2) NOT NULL DEFAULT 0,
    note text);
CREATE TABLE document_origin (origin_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url text NOT NULL, kind text NOT NULL DEFAULT 'report', skipped_reason text,
    created_at timestamptz NOT NULL DEFAULT now());
"""


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        raw.cursor().execute(_DDL)
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    monkeypatch.setattr(T, "_review_disabled", lambda: frozenset())
    T._HEALTH_CACHE.clear()
    yield eng
    T._HEALTH_CACHE.clear()
    eng.dispose()


def _strict(sql: str, params: dict | None = None) -> list[dict]:
    """Как T._rows, но ошибка SQL роняет тест, а не превращается в пустой блок."""
    from sqlalchemy import text
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params or {}).mappings().all()]


def _exec(sql: str, rows: list[dict]) -> None:
    from sqlalchemy import text
    with db.session() as s:
        s.execute(text(sql), rows)


def _at(days_ago: int, hour: int = 12) -> datetime:
    """Момент days_ago дней назад, hour часов по МСК."""
    d = datetime.now(MSK).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hour, tzinfo=MSK)


def _seed() -> None:
    ev = []
    for ago in range(14):
        ev += [{"u": "anna", "k": "api_request", "s": None, "t": _at(ago)}]
    ev += [{"u": "anna", "k": "api_error", "s": None, "t": _at(1)} for _ in range(12)]   # вчера сбой
    ev += [{"u": None, "k": "web_search", "s": "down", "t": _at(2)} for _ in range(6)]
    ev += [{"u": None, "k": "web_search", "s": "ok", "t": _at(2)} for _ in range(4)]
    ev += [{"u": None, "k": "web_search_chain", "s": "ok", "t": _at(3)}]
    ev += [{"u": "anna", "k": "rag_ingest", "s": None, "t": _at(4)}]
    # 23:30 МСК позавчера — это ещё позавчера по МСК, хотя по UTC уже другой час
    ev += [{"u": "anna", "k": "page_view", "s": None, "t": _at(2, 23) + timedelta(minutes=30)}
           for _ in range(31)]
    ev += [{"u": "anna", "k": "page_leave", "s": None, "t": _at(0)}]
    for e in ev:
        st = e.pop("s")
        e["p"] = json.dumps({"status": st} if st else {})
        e["dur"] = 1900000 if e["k"] == "page_leave" else None      # 31 минута на странице
    _exec("""INSERT INTO usage_event (username, kind, payload, dur_ms, created_at)
             VALUES (:u, :k, CAST(:p AS jsonb), :dur, :t)""", ev)
    _exec("""INSERT INTO user_event (username, kind, payload, ts) VALUES (:u, :k, CAST(:p AS jsonb), :t)""",
          [{"u": "anna", "k": "ai_query", "p": "{}", "t": _at(1)},
           {"u": "anna", "k": "ai_query", "p": "{}", "t": _at(20)},                 # прошлый период
           {"u": "anna", "k": "reviews_search", "p": '{"error": "x"}', "t": _at(0, 9)}])
    _exec("INSERT INTO app_user (username, display_name) VALUES (:u, :n)",
          [{"u": "anna", "n": "Анна"}, {"u": "boris", "n": "Борис"}])
    _exec("""INSERT INTO report (username, payload, created_at) VALUES ('anna', CAST(:p AS jsonb), :t)""",
          [{"p": "{}", "t": _at(16)}, {"p": '{"mode": "quick"}', "t": _at(17)}])
    _exec("""INSERT INTO item_feedback (username, kind, item_key, verdict, created_at)
             VALUES ('anna', 'ai_answer', :k, :v, :t)""",
          [{"k": "m1", "v": 1, "t": _at(1)}, {"k": "m2", "v": -1, "t": _at(1)},
           {"k": "m3", "v": 1, "t": _at(3)}])
    today = datetime.now(MSK).date()
    _exec("""INSERT INTO daily_digest (digest_date, section, status) VALUES (:d, :s, :st)""",
          [{"d": today - timedelta(days=a), "s": "headline", "st": "failed" if a == 5 else "ok"}
           for a in range(1, 10) if a != 3])
    _exec("""INSERT INTO daily_digest_archive (digest_date, section, status) VALUES (:d, 'headline', 'failed')""",
          [{"d": today - timedelta(days=6)}])
    # корпус banki.ru: наивное МСК-время площадки, свеж по вчера
    _exec("INSERT INTO review_index (url, dt, kind) VALUES (:u, :dt, 'complaint')",
          [{"u": f"u{a}-{j}", "dt": _at(a).replace(tzinfo=None)} for a in range(1, 80) for j in range(3)])
    # 60 неразмеченных за 3 дня до сегодня — в хвосте разметки (другая площадка, чтобы
    # не исказить неделю корпуса)
    _exec("""INSERT INTO review_index (url, source, dt, kind, ingested_at)
             VALUES (:u, 'banki_reviews', :dt, NULL, :ing)""",
          [{"u": f"p{j}", "dt": _at(3).replace(tzinfo=None), "ing": _at(3, 13)} for j in range(60)])
    _exec("INSERT INTO news_item (source, first_seen) VALUES (:s, :t)",
          [{"s": "feed", "t": _at(a)} for a in range(1, 42) for _ in range(5)])
    _exec("INSERT INTO document_origin (url, skipped_reason, created_at) VALUES ('x', :r, :t)",
          [{"r": "captcha", "t": _at(5)} for _ in range(6)])


def test_health_sql_on_postgres(pg, monkeypatch):
    _seed()
    monkeypatch.setattr(T, "_rows", _strict)
    now = datetime.now(MSK)
    h = T._health_calc(T._health_raw(), now.date(), now)
    assert len(h["days"]) == 14 and h["days"][-1] == now.date().isoformat()
    assert h["server"][-2] == "b" and h["server"][-3] == "o"                     # 12 ошибок вчера
    assert h["web"][-3] == "b" and h["web"][-4] == "o"
    dg = h["digest"]
    assert dg[-4] == "b"                                       # позавчера-1: выпуска не было
    assert dg[-6] == "b" and dg[-7] == "w" and dg[-2] == "o"   # сбой; исправленный сбой; норма
    assert dg[-11] == "n"                                      # раньше первого выпуска
    assert set(h["reviews"][:-1]) == {"o"} and h["reviews"][-1] == "o"
    assert h["slices"][-1] == "b" and h["slices"][-5] == "o"   # хвост — с вечера 3 дня назад
    assert h["news"][-2] == "o" and h["news"][-1] == "n"       # сегодня новостей ещё нет
    assert h["rsearch"][-1] in ("w", "n")                      # «n», если сегодня до 04.11.2026
    assert h["ingest"][-5] == "o" and h["ingest"][-6] == "b"
    assert all(len(h[k]) == 14 for k in T._HEALTH_KEYS)


def test_activity_and_previous_period_on_postgres(pg, monkeypatch):
    _seed()
    orig = T._rows

    def rows(sql, params=None):                # новые запросы — строго, прочие — как в проде
        if "AS reports" in sql or "AS good" in sql or "UNION ALL" in sql:
            return _strict(sql, params)
        return orig(sql, params)
    monkeypatch.setattr(T, "_rows", rows)
    d = T.users_directory(30, [])
    anna = next(r for r in d["users"] if r["username"] == "anna")
    # позавчера 31 просмотр (23:30 МСК — ещё позавчера), вчера вопрос ИИ, сегодня 31 минута
    assert anna["act14"][-3] == "3" and anna["act14"][-2] == "2" and anna["act14"][-1] == "3"
    assert next(r for r in d["users"] if r["username"] == "boris")["act14"] == "0" * 14
    m = T.metrics(14, [])
    assert m["features"]["ai_total_prev"] == 1 and m["features"]["reports_prev"] == 1
    fb = m["ai_feedback"]
    assert (fb["likes"], fb["dislikes"]) == (2, 1)
    assert fb["by_day"][-2] == {"d": fb["by_day"][-2]["d"], "good": 1, "bad": 1}
    assert fb["by_day"][-4]["good"] == 1 and len(fb["by_day"]) == 14
