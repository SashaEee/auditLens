"""Траты на модели для «Пульса»: сколько тратит отдел и сколько из этого — AuditLens.

Источник — сервис учёта расходов облака (один GET с токеном отдела: траты за
день, месяц или год с разбивкой по ключам и моделям, порог отдела и темп).
Траты по дням складываются в cloud_spend_day: прошлые дни не перезапрашиваются,
сегодняшний и вчерашний — раз в час, пока не закрыты. Синхронизация ленивая:
её запускает открытие «Пульса» в фоне, ответ не ждёт облака.

Настройка — только через env (репозиторий публичный, адресов и ключей в коде нет):
  CLOUD_COSTS_URL        адрес сервиса;
  CLOUD_COSTS_TOKEN      токен отдела;
  CLOUD_COSTS_DEPARTMENT отдел токена;
  CLOUD_COSTS_APP_KEYS   имена ключей AuditLens через запятую (остальные ключи
                         отдела — «другие задачи отдела»);
  CLOUD_COSTS_SINCE      с какого дня собирать историю (по умолчанию 2026-06-01).
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import os
import threading
import time

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)
MSK = dt.timezone(dt.timedelta(hours=3))


def _cfg() -> dict:
    return {"url": os.getenv("CLOUD_COSTS_URL", "").strip(),
            "token": os.getenv("CLOUD_COSTS_TOKEN", "").strip(),
            "dept": os.getenv("CLOUD_COSTS_DEPARTMENT", "").strip(),
            "app": [k.strip() for k in os.getenv("CLOUD_COSTS_APP_KEYS", "").split(",") if k.strip()],
            "since": dt.date.fromisoformat(os.getenv("CLOUD_COSTS_SINCE", "2026-06-01"))}


def configured() -> bool:
    c = _cfg()
    return bool(c["url"] and c["token"] and c["dept"])


def _get(aggregation: str, period: str) -> dict:
    import httpx
    c = _cfg()
    r = httpx.get(c["url"], params={"aggregation": aggregation, "period": period, "department": c["dept"]},
                  headers={"Authorization": "Bearer " + c["token"]}, timeout=30)
    r.raise_for_status()
    return r.json()


def _today() -> dt.date:
    return dt.datetime.now(MSK).date()


# ── синхронизация по дням ───────────────────────────────────────────────────
_SYNC_LOCK = threading.Lock()
_LAST_SYNC = [0.0]


def _store_day(day: dt.date, j: dict) -> None:
    rows = [(s["api_key"], m["name"], m["cost"]) for s in j.get("spend") or [] for m in s.get("models") or []]
    tot = j.get("total") or {}
    with db.session() as s:
        s.execute(text("DELETE FROM cloud_spend_day WHERE day = :d"), {"d": day})
        for k, m, c in rows:
            s.execute(text("INSERT INTO cloud_spend_day (day, api_key, model, cost) VALUES (:d, :k, :m, :c)"),
                      {"d": day, "k": k, "m": m, "c": c})
        s.execute(text("""
            INSERT INTO cloud_spend_meta (day, total, threshold, as_of, fetched_at)
            VALUES (:d, :t, :th, :a, now())
            ON CONFLICT (day) DO UPDATE SET total = EXCLUDED.total, threshold = EXCLUDED.threshold,
                as_of = EXCLUDED.as_of, fetched_at = now()"""),
                  {"d": day, "t": tot.get("current"), "th": tot.get("threshold"), "a": j.get("as_of")})


def _closed_days() -> set[dt.date]:
    """Дни, собранные целиком: as_of дошёл до полуночи следующего дня."""
    with db.session() as s:
        rows = s.execute(text("SELECT day, as_of FROM cloud_spend_meta")).all()
    out = set()
    for d, a in rows:
        if a is not None and a.astimezone(MSK) >= dt.datetime.combine(d + dt.timedelta(days=1), dt.time(), MSK):
            out.add(d)
    return out


def sync(max_days: int = 400) -> int:
    """Дособрать дни, которые ещё не закрыты. Возвращает число запросов к сервису."""
    if not configured() or not _SYNC_LOCK.acquire(blocking=False):
        return 0
    n = 0
    try:
        closed = _closed_days()
        d, today = _cfg()["since"], _today()
        while d <= today and n < max_days:
            if d not in closed:
                try:
                    _store_day(d, _get("day", d.isoformat()))
                except Exception as e:  # noqa: BLE001 — один день не должен ронять остальные
                    log.warning("cloud_costs: день %s не собрался: %s", d, e)
                n += 1
            d += dt.timedelta(days=1)
        _LAST_SYNC[0] = time.time()
    finally:
        _SYNC_LOCK.release()
    return n


def sync_soon() -> None:
    """Фоновая синхронизация, если с прошлой прошло больше 50 минут."""
    if configured() and time.time() - _LAST_SYNC[0] > 3000 and not _SYNC_LOCK.locked():
        threading.Thread(target=sync, name="cloud-costs-sync", daemon=True).start()


# ── сводка ──────────────────────────────────────────────────────────────────
_MONTH_CACHE: dict[str, tuple[float, dict]] = {}


def _month_live(period: str) -> dict | None:
    """Месяц целиком из сервиса: порог, темп и as_of (кэш 10 минут)."""
    hit = _MONTH_CACHE.get(period)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        j = _get("month", period)
    except Exception as e:  # noqa: BLE001
        log.warning("cloud_costs: месяц %s не получен: %s", period, e)
        return hit[1] if hit else None
    _MONTH_CACHE[period] = (time.time(), j)
    return j


def _family(model: str) -> str:
    return model.split("/")[-1]


def summary() -> dict:
    """Всё для карточки «Траты на модели»: месяц против порога с прогнозом,
    доля AuditLens, по дням, по месяцам и модели AuditLens."""
    c = _cfg()
    if not configured():
        return {"configured": False}
    sync_soon()
    app = c["app"]
    today = _today()
    m0 = today.replace(day=1)
    with db.session() as s:
        days = s.execute(text("""
            SELECT day, sum(cost) FILTER (WHERE api_key = ANY(:app)), sum(cost)
            FROM cloud_spend_day WHERE day >= :d0 GROUP BY 1 ORDER BY 1"""),
            {"app": app, "d0": m0 - dt.timedelta(days=62)}).all()
        months = s.execute(text("""
            SELECT to_char(day, 'YYYY-MM'), sum(cost) FILTER (WHERE api_key = ANY(:app)), sum(cost)
            FROM cloud_spend_day GROUP BY 1 ORDER BY 1"""), {"app": app}).all()
        models = s.execute(text("""
            SELECT model, sum(cost) FILTER (WHERE day >= :m0), sum(cost) FILTER (WHERE day >= :p0 AND day < :m0)
            FROM cloud_spend_day WHERE api_key = ANY(:app) AND day >= :p0
            GROUP BY 1 ORDER BY 2 DESC NULLS LAST"""),
            {"app": app, "m0": m0, "p0": (m0 - dt.timedelta(days=1)).replace(day=1)}).all()
        synced = s.execute(text("SELECT max(fetched_at), count(*) FROM cloud_spend_meta")).one()
    live = _month_live(m0.strftime("%Y-%m")) or {}
    tot = live.get("total") or {}
    dept_month = float(tot.get("current") or sum(float(r[2] or 0) for r in days if r[0] >= m0))
    app_month = sum(float(r[1] or 0) for r in days if r[0] >= m0)
    if live.get("spend"):
        app_month = sum(float(k.get("cost") or 0) for k in live["spend"] if k.get("api_key") in app)
    threshold = float(tot["threshold"]) if tot.get("threshold") is not None else None
    as_of = live.get("as_of")
    dim = calendar.monthrange(today.year, today.month)[1]
    if as_of:
        a = dt.datetime.fromisoformat(as_of).astimezone(MSK)
        elapsed = max((a - dt.datetime.combine(m0, dt.time(), MSK)).total_seconds() / 86400, 0.5)
    else:
        elapsed = max(today.day - 0.5, 0.5)
    forecast = dept_month / elapsed * dim
    app_forecast = app_month / elapsed * dim
    # будни прошлых 28 дней — устойчивый темп AuditLens без разовых прогонов не отличить,
    # поэтому показываем медиану дня: разовый прогон медиану почти не двигает
    lastd = sorted(float(r[1] or 0) for r in days if today - dt.timedelta(days=28) <= r[0] < today)
    median_day = lastd[len(lastd) // 2] if lastd else None
    return {
        "configured": True, "department": c["dept"], "as_of": as_of,
        "synced_at": synced[0].isoformat() if synced[0] else None, "days_stored": int(synced[1] or 0),
        "month": {"period": m0.strftime("%Y-%m"), "dept": round(dept_month), "app": round(app_month),
                  "threshold": threshold, "pace": (live.get("pace") or {}).get("current"),
                  "forecast": round(forecast), "app_forecast": round(app_forecast),
                  "util": round(dept_month / threshold, 4) if threshold else None,
                  "util_forecast": round(forecast / threshold, 4) if threshold else None,
                  "app_share": round(app_month / dept_month, 4) if dept_month else None,
                  "app_median_day": round(median_day) if median_day is not None else None},
        "days": [{"day": d.isoformat(), "app": round(float(a or 0)), "dept": round(float(t or 0))}
                 for d, a, t in days if d >= m0 - dt.timedelta(days=31)],
        "months": [{"month": m, "app": round(float(a or 0)), "dept": round(float(t or 0))} for m, a, t in months],
        "models": [{"model": _family(mo), "month": round(float(a or 0)), "prev": round(float(p or 0))}
                   for mo, a, p in models if (a or 0) >= 1 or (p or 0) >= 1][:12],
    }
