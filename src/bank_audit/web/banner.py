"""Плашка для всех пользователей — «В облаках турбулентность».

Перебои у облачных провайдеров (ИИ-помощник, утренний выпуск) не предупреждают
заранее, и неизвестно, сколько они продлятся. Поэтому плашку включает и выключает
владелец в «Пульсе» (POST /api/admin/banner), без деплоя; срок — по желанию.

Текст по умолчанию живёт во фронте (app.jsx, TURB_COPY); title/body в таблице —
на случай другой формулировки. Таблица — migrations/097_app_banner.sql.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

BANNER_ID = "turbulence"
MAX_DAYS = 60
MAX_TEXT = 400


def _as_dt(v) -> datetime | None:
    """Postgres отдаёт datetime, SQLite (тесты) — строку."""
    if v is None or isinstance(v, datetime):
        return v
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _clip(v) -> str | None:
    v = " ".join(str(v or "").split())
    return v[:MAX_TEXT] or None


def state(now: datetime | None = None) -> dict:
    """Состояние плашки. on — включена владельцем; active — показывать сейчас
    (включена и срок не истёк); v — версия: после повторного включения свёрнутая
    плашка снова раскрывается у всех."""
    now = now or datetime.now(timezone.utc)
    row = None
    try:
        with db.session() as s:
            row = s.execute(text("""SELECT active, until, title, body, updated_at
                                      FROM app_banner WHERE banner_id = :id"""),
                            {"id": BANNER_ID}).first()
    except Exception:  # noqa: BLE001 — нет таблицы (миграция не накатана) = плашки нет
        log.warning("banner: таблица недоступна", exc_info=True)
    if not row:
        return {"id": BANNER_ID, "on": False, "active": False, "until": None,
                "title": None, "body": None, "v": None}
    until, upd = _as_dt(row[1]), _as_dt(row[4])
    return {"id": BANNER_ID, "on": bool(row[0]),
            "active": bool(row[0]) and (until is None or until > now),
            "until": until.isoformat() if until else None,
            "title": row[2] or None, "body": row[3] or None,
            "v": upd.isoformat() if upd else None}


def set_state(*, active: bool, days: int | None, by: str, title: str | None = None,
              body: str | None = None, now: datetime | None = None) -> dict:
    """Включить на days дней (None — пока не выключат) или выключить."""
    now = now or datetime.now(timezone.utc)
    until = None
    if active and days:
        until = now + timedelta(days=max(1, min(int(days), MAX_DAYS)))
    with db.session() as s:
        s.execute(text("""
            INSERT INTO app_banner (banner_id, active, until, title, body, updated_by, updated_at)
            VALUES (:id, :a, :u, :t, :b, :by, :now)
            ON CONFLICT (banner_id) DO UPDATE
               SET active = :a, until = :u, title = :t, body = :b, updated_by = :by, updated_at = :now
        """), {"id": BANNER_ID, "a": bool(active), "u": until, "t": _clip(title), "b": _clip(body),
               "by": by, "now": now})
    return state(now)
