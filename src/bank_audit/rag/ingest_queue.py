"""Очередь фоновой индексации.

Зачем. Раньше каждое чтение страницы агентом запускало отдельный daemon-поток
с полной индексацией (fetch → parse → chunk → embed → insert). У этого три беды:
потоков столько, сколько агент прочитал страниц (в большом отчёте — десятки);
при остановке контейнера daemon-потоки убиваются на полуслове, и документ
остаётся в базе без фрагментов — навсегда, потому что повторный ingest отсечёт
его как дубль; а падение потока гасилось в лог, наружу ничего не выходило.

Теперь: ограниченная очередь и постоянные воркеры. Ключевые решения.

  • Переполнение — отбрасываем, а не ждём. Агент не должен стоять из-за того,
    что индексация не успевает: его ответ пользователю важнее полноты архива.
    Отброшенное видно в счётчиках и в телеметрии.
  • Воркеров мало (2 по умолчанию). Пул соединений к БД общий с горячим путём
    поиска; фон не должен отбирать у него соединения.
  • Дедупликация на лету: один и тот же URL, поставленный дважды за отчёт,
    качается один раз.
  • Воркеры НЕ daemon, и при остановке приложения очередь допивается (drain).
    Иначе получаем ровно ту же полу-запись, ради которой всё затевалось.
  • Готовое содержимое передаётся вместе с заданием. Горячий путь уже скачал
    страницу — качать её второй раз бессмысленно, а для банковских SPA ещё и
    вредно: второй заход без браузера приносит JS-заглушку вместо тарифов.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

QUEUE_MAX = int(os.getenv("INGEST_QUEUE_MAX", "300"))
WORKERS = int(os.getenv("INGEST_QUEUE_WORKERS", "2"))


@dataclass
class Job:
    url: str
    bank_slug_hint: str | None = None
    prefer_browser: bool = False
    # уже скачанное горячим путём: не ходим в сеть второй раз
    content: bytes | None = None
    content_type: str | None = None
    final_url: str | None = None
    # для журнала происхождения
    origin: dict[str, Any] = field(default_factory=dict)


_q: "queue.Queue[Job | None]" = queue.Queue(maxsize=QUEUE_MAX)
_inflight: set[str] = set()
_lock = threading.Lock()
_workers: list[threading.Thread] = []
_started = False

_stats = {"enqueued": 0, "done": 0, "failed": 0, "dropped": 0, "duplicate": 0}


def stats() -> dict:
    with _lock:
        return {**_stats, "depth": _q.qsize(), "inflight": len(_inflight),
                "workers": len([w for w in _workers if w.is_alive()])}


def submit(url: str, **kw) -> bool:
    """Поставить URL в очередь. Возвращает False, если отброшено.

    Никогда не бросает исключений и никогда не блокирует вызывающего —
    это горячий путь агента.
    """
    if not url:
        return False
    key = f"{url}|{kw.get('prefer_browser', False)}"
    with _lock:
        if key in _inflight:
            _stats["duplicate"] += 1
            return False
        if not _started:
            start()
        _inflight.add(key)
    try:
        _q.put_nowait(Job(url=url, **kw))
    except queue.Full:
        with _lock:
            _inflight.discard(key)
            _stats["dropped"] += 1
        log.warning("очередь индексации переполнена, пропущен %s", url[:90])
        return False
    with _lock:
        _stats["enqueued"] += 1
    return True


def _record_origin(job: Job, document_id: int | None, skipped: str | None) -> None:
    """Журнал происхождения. Пишем и при неудаче: без записи «пробовали, но
    была капча» карта пробелов не отличит недоступный документ от несуществующего."""
    o = job.origin or {}
    if not o.get("kind"):
        return
    try:
        from .. import db
        from sqlalchemy import text as _t
        with db.session() as s:
            s.execute(_t("""
                INSERT INTO document_origin(document_id, url, kind, run_id,
                                            username, session_id, question,
                                            fetch_mode, skipped_reason)
                VALUES (:d,:u,:k,:r,:un,:sid,:q,:fm,:sr)
            """), {"d": document_id, "u": job.final_url or job.url,
                   "k": o.get("kind"), "r": o.get("run_id"),
                   "un": o.get("username"), "sid": o.get("session_id"),
                   "q": (o.get("question") or "")[:500] or None,
                   "fm": "browser" if job.prefer_browser else "http",
                   "sr": skipped})
    except Exception as e:
        log.info("не записал происхождение %s: %s", job.url[:70], e)


def _handle(job: Job) -> None:
    from .indexer import ingest_document_from_url
    t0 = time.time()
    res = ingest_document_from_url(
        job.url, bank_slug_hint=job.bank_slug_hint,
        prefer_browser=job.prefer_browser,
        content=job.content, content_type=job.content_type,
        final_url=job.final_url,
    )
    _record_origin(job, res.document_id, res.skipped_reason)
    dur = int((time.time() - t0) * 1000)
    try:
        from ..web import telemetry
        telemetry.log_event(
            (job.origin or {}).get("username"), "rag_ingest",
            dur_ms=dur,
            status=200 if res.chunks_added else 204,   # 204 — дошло, но нечего индексировать
            payload={"url": job.url[:200], "chunks": res.chunks_added,
                     "trust": float(res.trust_score or 0),
                     "skipped_reason": res.skipped_reason,
                     "origin": (job.origin or {}).get("kind")})
    except Exception:
        pass          # телеметрия не должна ломать индексацию
    log.info("проиндексировано %s: фрагментов %s, %s мс%s",
             job.url[:80], res.chunks_added, dur,
             f", пропущено: {res.skipped_reason}" if res.skipped_reason else "")


def _worker(idx: int) -> None:
    while True:
        job = _q.get()
        if job is None:                      # сигнал остановки
            _q.task_done()
            return
        key = f"{job.url}|{job.prefer_browser}"
        try:
            _handle(job)
            with _lock:
                _stats["done"] += 1
        except Exception as e:
            with _lock:
                _stats["failed"] += 1
            log.warning("индексация %s не удалась: %s: %s",
                        job.url[:80], type(e).__name__, str(e)[:160])
        finally:
            with _lock:
                _inflight.discard(key)
            _q.task_done()


def start() -> None:
    """Поднять воркеров. Идемпотентно; вызывается из lifespan и лениво из submit."""
    global _started
    if _started:
        return
    _started = True
    for i in range(max(1, WORKERS)):
        t = threading.Thread(target=_worker, args=(i,),
                             name=f"ingest-worker-{i}", daemon=False)
        t.start()
        _workers.append(t)
    log.info("очередь индексации: %s воркеров, потолок %s", len(_workers), QUEUE_MAX)


def drain(timeout: float = 10.0) -> None:
    """Дать очереди доработать при остановке приложения.

    Воркеры не daemon, поэтому без этого контейнер не остановится; а без
    ожидания вовсе мы вернулись бы к обрыву индексации на полуслове.
    """
    global _started
    if not _started:
        return
    deadline = time.time() + timeout
    for _ in _workers:
        try:
            _q.put_nowait(None)
        except queue.Full:
            pass
    for t in _workers:
        t.join(timeout=max(0.1, deadline - time.time()))
    _started = False
    log.info("очередь индексации остановлена: %s", stats())
