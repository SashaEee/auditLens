"""Фоновые циклы дайджеста + ежедневный автосбор тарифов.

  digest_background_loop — генерация выпуска в DIGEST_GEN_HOUR_MSK (07:00) +
                           catch-up после рестарта контейнера
  ensure_digest          — идемпотентный запуск генерации (утро/lazy/manual);
                           stampede-защита: asyncio.Lock (процесс) +
                           pg advisory lock (межпроцессный, auto-release)
  ingest_background_loop — автосбор тарифов в INGEST_HOUR_MSK (05:00) + quality:
                           до этого сбор запускался только кнопкой → change_history
                           не наполнялся, и «Тарифные движения недели» были бы пусты
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta

from sqlalchemy import text

from .. import db
from ..clock import MSK
from . import pipeline, store

log = logging.getLogger(__name__)

GEN_HOUR = int(os.getenv("DIGEST_GEN_HOUR_MSK", "7"))
INGEST_HOUR = int(os.getenv("INGEST_HOUR_MSK", "5"))
INGEST_DAILY = os.getenv("INGEST_DAILY", "1") == "1"
# lazy-прогоны не чаще раза в N секунд: одна перманентно падающая секция иначе
# гоняла бы регенерацию по кругу (каждый GET с поллинга)
LAZY_COOLDOWN_S = int(os.getenv("DIGEST_LAZY_COOLDOWN_S", "600"))

_proc_lock = asyncio.Lock()


def _today_msk() -> date:
    return datetime.now(MSK).date()


def lazy_allowed() -> bool:
    """Можно ли lazy-генерить СЕГОДНЯШНИЙ выпуск прямо сейчас.
    Ночью (до GEN_HOUR МСК) — нет: визит в 00:30 иначе генерил бы «утренний»
    выпуск до автосбора тарифов 05:00, а прогон 07:00 становился бы no-op;
    до утра честно показываем вчерашний («действует до утра»)."""
    return datetime.now(MSK).hour >= GEN_HOUR


async def ensure_digest(trigger: str, day: date | None = None, force: bool = False,
                        sections: list[str] | None = None) -> bool:
    """Идемпотентно: дайджест дня есть и не force → no-op.
    True = генерация реально выполнена этим вызовом."""
    day = day or _today_msk()
    if not force and await asyncio.to_thread(store.day_complete, day, pipeline.REQUIRED):
        return False
    if trigger == "lazy":
        if not lazy_allowed():
            return False
        age = await asyncio.to_thread(store.last_finished_age_s, day)
        if age is not None and age < LAZY_COOLDOWN_S:
            return False           # недавно уже пробовали — cooldown
    if _proc_lock.locked():        # в этом процессе уже генерится
        return False
    async with _proc_lock:
        def _locked_run() -> bool:
            with store.try_acquire_day_lock(day) as got:
                if not got:        # другой процесс/реплика уже генерит
                    return False
                if not force and store.day_complete(day, pipeline.REQUIRED):
                    return False   # перепроверка под локом
                store.mark_run(day, trigger)
                # отдельный event-loop в worker-потоке: генерация (LLM, fetch)
                # не блокирует основной цикл FastAPI
                asyncio.run(pipeline.run_daily(day, force=force, only=sections))
                return True
        return await asyncio.to_thread(_locked_run)


async def digest_background_loop():
    """Утренняя генерация + catch-up. Паттерн alerts_background_loop."""
    await asyncio.sleep(90)                      # не толкаться на старте
    try:                                         # рестарт после GEN_HOUR → догоняем
        if datetime.now(MSK).hour >= GEN_HOUR:
            ran = await ensure_digest("morning-catchup")
            if ran:
                log.info("digest catch-up: сгенерирован выпуск %s", _today_msk())
    except Exception as e:  # noqa: BLE001
        log.warning("digest catch-up failed: %s", e)
    while True:
        now = datetime.now(MSK)
        nxt = now.replace(hour=GEN_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await ensure_digest("morning")
        except Exception as e:  # noqa: BLE001
            log.warning("digest morning run failed: %s", e)


# ── ежедневный автосбор тарифов ──────────────────────────────────────────────

# Единый мьютекс сбора: автосбор ↔ ручной запуск из UI. Два параллельных сбора
# = два Chromium на ОДНОМ browser-профиле (launch_persistent_context упадёт или
# профиль залочится). web.app._do_ingest* берёт этот же лок.
import threading
INGEST_MUTEX = threading.Lock()


def _ingest_ran_today() -> bool:
    """Был ли сегодня (МСК) ЗАВЕРШЁННЫЙ прогон сбора (или живой свежий).
    Убитый деплоем прогон (вечный running) не считаем — иначе полдня без сбора
    «засчитывается» как выполненный."""
    with db.session() as s:
        row = s.execute(text("""
            SELECT count(*) FROM extraction_run
             WHERE started_at >= (now() AT TIME ZONE 'Europe/Moscow')::date
                                  AT TIME ZONE 'Europe/Moscow'
               AND (finished_at IS NOT NULL
                    OR started_at > now() - interval '30 minutes')
        """)).scalar()
    return bool(row and int(row) > 0)


def _captcha_pending() -> bool:
    """Решается капча (in-process флаг веб-слоя) → сбор не запускаем.
    Lazy-импорт: web.app сам импортирует scheduler (иначе цикл)."""
    try:
        from ..web import app as webapp
        return bool(getattr(webapp, "_CAPTCHA_LOCK", False))
    except Exception:  # noqa: BLE001
        return False


def _run_ingest_all() -> None:
    """Все источники последовательно + quality-чеки. Каждый источник пишет свой
    статус в extraction_run — упавший не валит остальных."""
    if _captcha_pending():
        log.info("daily ingest: пропуск — решается капча")
        return
    if not INGEST_MUTEX.acquire(blocking=False):
        log.info("daily ingest: пропуск — сбор уже идёт")
        return
    try:
        from ..config import load_sources
        from ..orchestrator.runner import ingest
        sources = list(load_sources().keys())
        log.info("daily ingest: старт, источники: %s", sources)
        for src in sources:
            try:
                ingest(src, None)
            except Exception as e:  # noqa: BLE001
                log.warning("daily ingest %s failed: %s", src, e)
        try:
            from ..quality.checks import run_quality
            res = run_quality()
            log.info("daily ingest: quality %s", res)
        except Exception as e:  # noqa: BLE001
            log.warning("daily quality failed: %s", e)
    finally:
        INGEST_MUTEX.release()


async def ingest_background_loop():
    """Автосбор тарифов раз в день в INGEST_HOUR (МСК, до генерации дайджеста):
    change_history наполняется сам, а не только по кнопке. INGEST_DAILY=0 — выкл."""
    if not INGEST_DAILY:
        log.info("daily ingest: выключен (INGEST_DAILY=0)")
        return
    await asyncio.sleep(120)
    try:
        # catch-up ТОЛЬКО в окне [INGEST_HOUR, +4ч): рестарт/деплой посреди
        # рабочего дня не должен внезапно запускать полный сбор (браузер, капчи).
        # Первый сбор после дневного деплоя случится завтра в INGEST_HOUR — или
        # вручную кнопкой «Запустить весь сбор».
        now_h = datetime.now(MSK).hour
        if INGEST_HOUR <= now_h < INGEST_HOUR + 4 and not (
                await asyncio.to_thread(_ingest_ran_today)):
            await asyncio.to_thread(_run_ingest_all)
    except Exception as e:  # noqa: BLE001
        log.warning("daily ingest catch-up failed: %s", e)
    while True:
        now = datetime.now(MSK)
        nxt = now.replace(hour=INGEST_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            if not await asyncio.to_thread(_ingest_ran_today):
                await asyncio.to_thread(_run_ingest_all)
        except Exception as e:  # noqa: BLE001
            log.warning("daily ingest failed: %s", e)
