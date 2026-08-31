"""Cron-планировщик парсеров: asyncio-тикер + фаза самовосстановления.

Паттерн digest/scheduler.py: цикл в FastAPI-процессе, asyncio.Lock от
stampede внутри процесса (веб-процесс один). Тик каждые PARSER_SCHED_TICK_S
секунд (default 60). Отключение: PARSER_SCHEDULER_ENABLED=0.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from ... import db
from ...clock import MSK
from .. import repository as repo
from . import healer as healer_mod
from . import runner as runner_mod

log = logging.getLogger(__name__)

TICK_S = int(os.getenv("PARSER_SCHED_TICK_S", "60"))
# По умолчанию ВЫКЛЮЧЕН: цикл запускает сгенерированный код по расписанию и
# поднимает «лечение» (вызовы модели + pip install). Включать осознанно,
# после проверки, что генерация и запуск ведут себя предсказуемо.
ENABLED = os.getenv("PARSER_SCHEDULER_ENABLED", "0") == "1"

_lock = asyncio.Lock()


def next_run(cron_expr: str, base: datetime | None = None) -> datetime:
    """Следующее время запуска по cron. ValueError при невалидном выражении."""
    from croniter import croniter

    base = base or datetime.now(MSK)
    try:
        return croniter(cron_expr, base).get_next(datetime)
    except (ValueError, KeyError) as e:
        raise ValueError(f"invalid cron expression: {cron_expr!r}") from e


def _parse_dt(value) -> datetime | None:
    """datetime/ISO-строка → aware datetime (naive считаем МСК)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=MSK)
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=MSK)


async def tick(now: datetime | None = None) -> list[int]:
    """Один тик: запуск due-парсеров + heal-фаза. Возвращает parser_id запущенных."""
    now = now or datetime.now(MSK)
    started: list[int] = []
    async with _lock:
        with db.session() as s:
            parsers = repo.list_auto_parsers(session=s)
        for p in parsers:
            pid = p["parser_id"]
            if pid in runner_mod._RUNNING:
                continue
            due_at = _parse_dt(p.get("next_run_at"))
            if due_at is None or due_at > now:
                continue
            try:
                await runner_mod.run(pid, "cron")
                started.append(pid)
                log.info("[parsers.scheduler] cron-запуск parser_id=%s", pid)
            except Exception as e:
                log.warning("[parsers.scheduler] run failed parser_id=%s: %s", pid, e)
            try:
                nxt = next_run(p["cron_expr"], now)
            except ValueError:
                log.warning("[parsers.scheduler] невалидный cron parser_id=%s", pid)
                nxt = None
            try:
                with db.session() as s:
                    repo.update_parser_next_run(pid, nxt, session=s)
            except Exception:
                log.exception("[parsers.scheduler] update next_run failed parser_id=%s", pid)
        try:
            await healer_mod.heal_tick(now)
        except Exception:
            log.exception("[parsers.scheduler] heal_tick failed")
    return started


async def parser_scheduler_loop() -> None:
    log.info("[parsers.scheduler] старт, тик %ss", TICK_S)
    while True:
        try:
            await tick()
        except Exception:
            log.exception("[parsers.scheduler] tick failed")
        await asyncio.sleep(TICK_S)
