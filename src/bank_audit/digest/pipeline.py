"""Оркестрация дневного прогона: реестр секций, per-section timeout, деградация.

Границы отказа = секция, не прогон: упавшая секция получает copy_forward
(вчерашняя копия со status='stale') или status='failed', остальные живут.
Этап 1 — все независимые секции ПАРАЛЛЕЛЬНО; headline — последним (читает
уже записанные секции).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date
from typing import Awaitable, Callable

from . import aggregator, store, writer

log = logging.getLogger(__name__)

SECTION_TIMEOUT_S = float(os.getenv("DIGEST_SECTION_TIMEOUT_S", "150"))

# Реестр: имя → корутина. «Лазейки» соседней команды = ещё одна пара
# ключ/корутина (+ generic-рендер на фронте), без миграций и перевёрстки.
SECTIONS: dict[str, Callable[[date], Awaitable[dict]]] = {
    "reviews_pulse": aggregator.reviews_pulse,   # SQL
    "tariff_moves":  aggregator.tariff_moves,    # SQL (+SOAP ключевая ставка)
    "quality_ops":   aggregator.quality_ops,     # SQL
    "reviews_brief": writer.reviews_brief,       # 1 LLM (smart)
    "news":          writer.news,                # fetch → 1 LLM (smart)
    "headline":      writer.headline,            # 1 LLM (fast), читает секции выше
}
REQUIRED: tuple[str, ...] = tuple(SECTIONS.keys())


async def _run_section(day: date, name: str,
                       fn: Callable[[date], Awaitable[dict]]) -> str:
    t0 = time.monotonic()
    try:
        payload = await asyncio.wait_for(fn(day), timeout=SECTION_TIMEOUT_S)
        status = payload.pop("_status", "ok")
        llm_model = payload.pop("_llm_model", None)
        ti = payload.pop("_tokens_in", None)
        to = payload.pop("_tokens_out", None)
        await asyncio.to_thread(
            store.upsert, day, name, payload,
            status=status, llm_model=llm_model, tokens_in=ti, tokens_out=to,
            gen_ms=int((time.monotonic() - t0) * 1000))
        return status
    except Exception as e:  # noqa: BLE001 — деградирует секция, не прогон
        log.warning("digest section %s failed: %s", name, e)
        err = f"{type(e).__name__}: {str(e)[:250]}"
        # у дня УЖЕ есть живая версия секции (перегенерация упала на транзиенте) —
        # не перетираем хорошее содержимое failed'ом, оставляем как было
        if name in await asyncio.to_thread(store.live_sections, day):
            return "kept"
        copied = await asyncio.to_thread(store.copy_forward, day, name, error=err)
        if not copied:
            await asyncio.to_thread(store.upsert, day, name, {},
                                    status="failed", error=err)
        return "stale" if copied else "failed"


async def run_daily(day: date, force: bool = False,
                    only: list[str] | None = None) -> dict[str, str]:
    """Прогон дня. force=False → регенерируем ТОЛЬКО недостающие/failed секции
    (иначе lazy-триггер при одной перманентно падающей секции гонял бы полный
    пайплайн — LLM-вызовы и фетчи источников — по кругу весь день)."""
    names = [n for n in SECTIONS if (not only or n in only)]
    if not force and not only:
        live = await asyncio.to_thread(store.live_sections, day)
        names = [n for n in names if n not in live]
        if not names:                       # всё живое — закрываем прогон как no-op
            await asyncio.to_thread(store.finish_run, day, {})
            return {}
    results: dict[str, str] = {}

    # этап 1: всё, кроме headline, параллельно
    stage1 = [n for n in names if n != "headline"]
    if stage1:
        statuses = await asyncio.gather(
            *(_run_section(day, n, SECTIONS[n]) for n in stage1))
        results.update(dict(zip(stage1, statuses)))

    # этап 2: headline поверх записанных секций
    if "headline" in names:
        results["headline"] = await _run_section(day, "headline", SECTIONS["headline"])

    await asyncio.to_thread(store.finish_run, day, results)
    log.info("digest run %s: %s", day, results)
    return results
