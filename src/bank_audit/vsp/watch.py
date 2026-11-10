"""Сторож чтения моделью: видит ли вкладка свежие истории.

Отдельно от демона, чтобы приложение могло спросить состояние, не подтягивая сборщики."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable

LLM_DAILY_RUB = float(os.getenv("VSP_LLM_DAILY_RUB", "300"))      # потолок трат на модели в сутки
LLM_STALE_H = float(os.getenv("VSP_LLM_STALE_H", "26"))           # столько часов без «всё прочитано» — тревога


def _dt(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def llm_watch(kv_get: Callable[[str], object], now: datetime | None = None) -> dict:
    """«ok» — всё подходящее прочитано недавно; «late» — отзывы ждут дольше суток (вкладка не видит
    свежие истории); «off» — шаг чтения ещё ни разу не шёл (читает внешний запуск или никто)."""
    now = now or datetime.now(timezone.utc)
    last = kv_get("llm:last") or {}
    at, clean = _dt(kv_get("llm:at")), _dt(kv_get("llm:clean_at"))
    waiting = (last.get("waiting") if isinstance(last, dict) else None) or {}
    n = sum(int(v or 0) for v in waiting.values())
    stale = timedelta(hours=LLM_STALE_H)
    if at is None:
        state = "off"
    elif (now - at) > stale or (n and (clean is None or (now - clean) > stale)):
        state = "late"
    else:
        state = "ok"
    spent = kv_get("llm:spent") or {}
    return {"state": state, "waiting": waiting, "waiting_total": n, "at": at.isoformat() if at else None,
            "clean_at": clean.isoformat() if clean else None,
            "spent_today": float((spent if isinstance(spent, dict) else {}).get(now.strftime("%Y-%m-%d"), 0.0)),
            "daily_cap": LLM_DAILY_RUB, "failed": kv_get("failed:llm")}
