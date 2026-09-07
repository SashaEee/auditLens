"""Ограниченный offload синхронных сетевых чтений инструментов агента."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_Result = TypeVar("_Result")
NETWORK_CONCURRENCY = 4
NETWORK_TIMEOUT_SECONDS = 45.0

# Ограничение общее для процесса и всех event loop. asyncio.Semaphore нельзя
# освобождать при отмене await: выполняющийся sync-вызов продолжает занимать слот.
_NETWORK_SLOTS = threading.BoundedSemaphore(NETWORK_CONCURRENCY)
_NETWORK_POOL = ThreadPoolExecutor(
    max_workers=NETWORK_CONCURRENCY, thread_name_prefix="loophole-network",
)


async def run_blocking_network(
    operation: Callable[..., _Result], /, *args: Any, **kwargs: Any,
) -> _Result:
    """Выполняет сетевое чтение без Session/ToolContext исследовательского запуска.

    Слот освобождает concurrent Future при физическом завершении worker, включая
    отменённые ожидания. Очередь executor не растёт сверх общего числа слотов.
    Служебный RAG-кэш вправе открыть собственную короткую сессию внутри worker;
    отмена ожидания не отменяет такое независимое заполнение кэша.
    """
    async with asyncio.timeout(NETWORK_TIMEOUT_SECONDS):
        slots = _NETWORK_SLOTS
        while not slots.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            # Не копируем ContextVar: контекст запроса и его Session остаются
            # у владельца event loop. В worker передаются только аргументы I/O.
            future = _NETWORK_POOL.submit(operation, *args, **kwargs)
        except BaseException:
            slots.release()
            raise
        future.add_done_callback(lambda _future: slots.release())
        return await asyncio.wrap_future(future)
