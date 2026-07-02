"""Хранилище дайджеста: daily_digest / digest_run + межпроцессный lock.

Почему Postgres, а не файл/кэш процесса: контейнер перезапускается по несколько
раз в день (деплой) — процесс-кэш пересчитывал бы дайджест, сжигая токены; файл
не даёт архива по датам и транзакционного claim'а от stampede.

Мьютекс генерации — сессионный pg_try_advisory_lock: держим ВЫДЕЛЕННЫЙ коннект
всю генерацию, краш процесса = лок отпущен автоматически (нет вечного
«in-progress», в отличие от голого флага в таблице). digest_run.status —
витрина для UI, не мьютекс.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

# Класс advisory-lock'а приложения (int4). Второй ключ — day.toordinal().
DIGEST_LOCK_CLASS = 219_970_701

# Прогон «висит» дольше N минут → считаем упавшим (advisory lock всё равно
# защищает от реальной гонки — это только про витрину digest_run).
RUN_STUCK_MIN = 15


def _eng():
    if db._engine is None:
        db.init()
    return db._engine


# ── запись ────────────────────────────────────────────────────────────────────

def upsert(day: date, section: str, payload: dict, *, status: str = "ok",
           llm_model: str | None = None, tokens_in: int | None = None,
           tokens_out: int | None = None, gen_ms: int | None = None,
           error: str | None = None, stale_from: date | None = None) -> None:
    with db.session() as s:
        s.execute(text("""
            INSERT INTO daily_digest (digest_date, section, payload, status, stale_from,
                                      generated_at, llm_model, tokens_in, tokens_out, gen_ms, error)
            VALUES (:d, :sec, CAST(:p AS jsonb), :st, :sf, now(), :m, :ti, :to, :ms, :err)
            ON CONFLICT (digest_date, section) DO UPDATE SET
                payload = EXCLUDED.payload, status = EXCLUDED.status,
                stale_from = EXCLUDED.stale_from, generated_at = now(),
                llm_model = EXCLUDED.llm_model, tokens_in = EXCLUDED.tokens_in,
                tokens_out = EXCLUDED.tokens_out, gen_ms = EXCLUDED.gen_ms,
                error = EXCLUDED.error
        """), {"d": day, "sec": section, "p": json.dumps(payload, ensure_ascii=False),
               "st": status, "sf": stale_from, "m": llm_model, "ti": tokens_in,
               "to": tokens_out, "ms": gen_ms, "err": error})


def copy_forward(day: date, section: str, *, error: str) -> bool:
    """Копирует ПОСЛЕДНЮЮ живую (не failed) версию секции из прошлых дней со
    status='stale'. True — скопировано; False — копировать нечего."""
    with db.session() as s:
        row = s.execute(text("""
            SELECT digest_date, payload::text, llm_model
              FROM daily_digest
             WHERE section = :sec AND digest_date < :d AND status IN ('ok','degraded','stale')
             ORDER BY digest_date DESC LIMIT 1
        """), {"sec": section, "d": day}).first()
    if not row:
        return False
    src_date, payload_txt, llm_model = row[0], row[1], row[2]
    upsert(day, section, json.loads(payload_txt), status="stale",
           stale_from=src_date, llm_model=llm_model, error=error[:300])
    return True


# ── чтение ────────────────────────────────────────────────────────────────────

def _read_day_rows(day: date) -> dict[str, dict]:
    with db.session() as s:
        rows = s.execute(text("""
            SELECT section, payload::text, status, stale_from, generated_at,
                   llm_model, tokens_in, tokens_out, error
              FROM daily_digest WHERE digest_date = :d
        """), {"d": day}).all()
    out = {}
    for sec, payload_txt, status, stale_from, gen_at, model, ti, to, err in rows:
        out[sec] = {
            "status": status, "payload": json.loads(payload_txt),
            "generated_at": gen_at.isoformat() if gen_at else None,
            **({"stale_from": stale_from.isoformat()} if stale_from else {}),
            **({"error": err} if err else {}),
            **({"llm_model": model} if model else {}),
            **({"tokens": {"in": ti or 0, "out": to or 0}} if (ti or to) else {}),
        }
    return out


def latest_day(upto: date) -> date | None:
    with db.session() as s:
        row = s.execute(text(
            "SELECT max(digest_date) FROM daily_digest WHERE digest_date <= :d"
        ), {"d": upto}).first()
    return row[0] if row else None


def list_dates(limit: int = 90) -> list[str]:
    with db.session() as s:
        rows = s.execute(text("""
            SELECT DISTINCT digest_date FROM daily_digest
             ORDER BY digest_date DESC LIMIT :n
        """), {"n": limit}).all()
    return [r[0].isoformat() for r in rows]


def read_latest(today: date, want: date | None = None) -> dict:
    """Собранный документ дайджеста: указанный день, или последний ≤ today.
    Никогда не кидает — на девственной БД вернёт {sections:{}, meta:{empty}}."""
    day = want or latest_day(today)
    sections = _read_day_rows(day) if day else {}
    tokens_in = sum((v.get("tokens") or {}).get("in", 0) for v in sections.values())
    tokens_out = sum((v.get("tokens") or {}).get("out", 0) for v in sections.values())
    gen_ts = [v["generated_at"] for v in sections.values() if v.get("generated_at")]
    return {
        "date": day.isoformat() if day else None,
        "meta": {
            "today": bool(day == today),
            "empty": not sections,
            "refreshing": run_in_progress(today),
            "generated_at": max(gen_ts) if gen_ts else None,
            "tokens": {"in": tokens_in, "out": tokens_out},
        },
        "sections": sections,
    }


def live_sections(day: date) -> set[str]:
    """Секции дня со «живым» статусом (ok/degraded/stale — есть что показать)."""
    with db.session() as s:
        rows = s.execute(text("""
            SELECT section FROM daily_digest
             WHERE digest_date = :d AND status IN ('ok','degraded','stale')
        """), {"d": day}).all()
    return {r[0] for r in rows}


def day_complete(day: date, required: tuple[str, ...]) -> bool:
    """Все обязательные секции дня существуют и живые (не failed)."""
    have = live_sections(day)
    return all(sec in have for sec in required)


def last_finished_age_s(day: date) -> float | None:
    """Секунды с завершения последнего прогона дня (None — не завершался)."""
    with db.session() as s:
        row = s.execute(text("""
            SELECT extract(epoch FROM now() - finished_at)
              FROM digest_run WHERE digest_date = :d AND finished_at IS NOT NULL
        """), {"d": day}).first()
    return float(row[0]) if row and row[0] is not None else None


# ── advisory lock (stampede-защита) ──────────────────────────────────────────

@contextmanager
def try_acquire_day_lock(day: date):
    """Сессионный advisory try-lock на (класс, день). Держим выделенный коннект
    всю генерацию: закрылся коннект (краш процесса) — лок отпущен автоматически.
    AUTOCOMMIT: иначе коннект висит «idle in transaction» все минуты генерации.
    yield True — лок наш; False — кто-то уже генерит, молча выходим."""
    conn = _eng().connect().execution_options(isolation_level="AUTOCOMMIT")
    got = False
    try:
        got = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(:c, :d)"),
            {"c": DIGEST_LOCK_CLASS, "d": day.toordinal()}).scalar())
        yield got
    finally:
        released = not got
        if got:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:c, :d)"),
                             {"c": DIGEST_LOCK_CLASS, "d": day.toordinal()})
                released = True
            except Exception:  # noqa: BLE001 — обработка ниже: invalidate
                pass
        try:
            if released:
                conn.close()
            else:
                # unlock не прошёл, а коннект жив → session-lock утёк бы в пул
                # и заблокировал день до рестарта; invalidate рвёт сам коннект,
                # и Postgres снимает лок вместе с сессией
                conn.invalidate()
        except Exception:  # noqa: BLE001
            pass


# ── digest_run (витрина прогона) ─────────────────────────────────────────────

def mark_run(day: date, trigger: str) -> None:
    with db.session() as s:
        s.execute(text("""
            INSERT INTO digest_run (digest_date, trigger)
            VALUES (:d, :t)
            ON CONFLICT (digest_date) DO UPDATE SET
                started_at = now(), finished_at = NULL,
                status = 'running', trigger = EXCLUDED.trigger
        """), {"d": day, "t": trigger})


def finish_run(day: date, results: dict[str, str]) -> None:
    vals = set(results.values())
    live = {"ok", "kept", "degraded", "stale"}
    status = ("ok" if vals <= {"ok", "kept"} else
              "failed" if vals and not (vals & live)
              else "partial")
    with db.session() as s:
        s.execute(text("""
            UPDATE digest_run SET finished_at = now(), status = :st,
                                  detail = CAST(:det AS jsonb)
             WHERE digest_date = :d
        """), {"d": day, "st": status, "det": json.dumps(results, ensure_ascii=False)})


def run_in_progress(day: date) -> bool:
    """running и started_at свежий (< RUN_STUCK_MIN) — иначе считаем упавшим."""
    with db.session() as s:
        row = s.execute(text("""
            SELECT status, started_at FROM digest_run WHERE digest_date = :d
        """), {"d": day}).first()
    if not row or row[0] != "running":
        return False
    started: datetime = row[1]
    try:
        age = datetime.now(started.tzinfo) - started
    except Exception:  # noqa: BLE001
        return False
    return age < timedelta(minutes=RUN_STUCK_MIN)
