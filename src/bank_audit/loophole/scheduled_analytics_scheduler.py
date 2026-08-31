"""Фоновый тикер разрешённых внутренних аналитических расписаний."""
from __future__ import annotations

import asyncio
import logging
import os

from .. import db
from .scheduled_analytics import ScheduledAnalyticsService

log = logging.getLogger(__name__)

TICK_S = int(os.getenv("SCHEDULED_ANALYTICS_TICK_S", "60"))
# Автовыполнение данных включается отдельным явным флагом окружения.
ENABLED = os.getenv("SCHEDULED_ANALYTICS_ENABLED", "0") == "1"
_lock = asyncio.Lock()


async def tick() -> list[int]:
    """Выполняет due-контракты, не допуская конкуренции внутри процесса."""
    async with _lock:
        with db.session() as session:
            return ScheduledAnalyticsService(session).run_due()


async def scheduled_analytics_loop() -> None:
    log.info("[scheduled_analytics] старт, тик %ss", TICK_S)
    while True:
        try:
            completed = await tick()
            if completed:
                log.info("[scheduled_analytics] выполнены контракты %s", completed)
        except Exception:
            log.exception("[scheduled_analytics] tick завершился ошибкой")
        await asyncio.sleep(TICK_S)
