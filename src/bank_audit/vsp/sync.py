"""Синхронизация отзывов: проход по отделениям источника с прогрессом и полнотой.

Режимы:
  full — полный проход выдачи отделения с начала до конца: обновляет ответы
         организации, ловит правки, помечает пропавшие отзывы (gone_at);
  new  — только новые: выдача «сначала новые» (по дате правки) до первой страницы, где
         все отзывы уже известны, без правок, И старше прошлой успешной синхронизации
         минус запас NEW_OVERLAP. Одного «всё знакомо» мало: отзыв, отредактированный без
         смены текста, стоит выше новых, созданных раньше его правки.

Прерванный полный проход продолжается с сохранённого курсора (resume), а начало
прохода хранится в состоянии — поэтому и после продолжения пропавшие отзывы
определяются верно. Блокировка источника (SourceBlocked) останавливает все задачи
этого источника в прогоне; остальные источники работают дальше.

Полнота — по факту, а не по ожиданию:
  complete       — дошли до конца выдачи источника с полной историей, получили
                   не меньше заявленного (с допуском);
  below_declared — дошли до конца, но источник заявляет больше, чем отдаёт;
  window         — дошли до конца выдачи, у которой есть потолок (часть истории
                   источник не показывает);
  partial        — проход прерван (ошибка, предохранитель по страницам);
  blocked        — источник ограничил доступ.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .collectors.base import Collector
from .models import CollectorError, NotFound, SourceBlocked, utcnow
from .storage import Store

# запас по времени для режима new: часовые пояса источников (2ГИС отдаёт +07:00),
# задержка индексации, правки «задним числом»
NEW_OVERLAP = timedelta(days=3)

log = logging.getLogger(__name__)

DECLARED_TOLERANCE = 0.02     # «получили не меньше заявленного» — с допуском 2 %
GONE_MAX_SHARE = 0.3          # пропало больше трети хранимого за один проход — сбой, не удаления
GONE_MIN_SUSPICIOUS = 5


def schedule_next(store: Store, collector: Collector, sid: str, *, full_pass: bool,
                  now: datetime | None = None) -> dict:
    """Когда проверить отделение снова. Интервал «только новых» — по частоте отзывов
    за последние rate_window_d дней (ожидаем ~due_target новых к следующей проверке),
    в пределах [due_min_h, due_max_h]. Полный проход — через full_every_d дней с
    постоянным для отделения сдвигом до 3 суток, чтобы проходы не сбивались в один день."""
    sch = collector.settings.schedule
    now = now or utcnow()
    n = store.recent_count(collector.source, sid, now - timedelta(days=sch.rate_window_d))
    per_day = n / max(1, sch.rate_window_d)
    hours = sch.due_max_h if per_day <= 0 else min(sch.due_max_h, max(sch.due_min_h, 24 * sch.due_target / per_day))
    out = {"next_due_at": now + timedelta(hours=hours)}
    if full_pass:
        spread = int(hashlib.sha1(f"{collector.source}:{sid}".encode()).hexdigest()[:8], 16) % 72
        out["next_full_at"] = now + timedelta(days=sch.full_every_d, hours=spread)
    return out


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class BranchResult:
    source: str
    source_branch_id: str
    status: str                     # done | not_found | blocked | failed
    completeness: str | None = None
    declared: int | None = None
    fetched: int = 0
    new: int = 0
    updated: int = 0
    gone: int = 0
    pages: int = 0
    dupes: int = 0                  # строк страниц сверх разных отзывов (повторы в выдаче)
    stop: str | None = None
    seconds: float = 0.0
    error: str | None = None


@dataclass
class RunStats:
    results: list[BranchResult] = field(default_factory=list)
    blocked: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict:
        out: dict = {"branches": len(self.results), "blocked": self.blocked}
        for k in ("new", "updated", "gone", "fetched", "pages", "dupes"):
            out[k] = sum(getattr(r, k) for r in self.results)
        by: dict[str, int] = {}
        for r in self.results:
            by[r.completeness or r.status] = by.get(r.completeness or r.status, 0) + 1
        out["by_status"] = by
        return out


def completeness_of(stop: str, *, full_history: bool, declared: int | None, fetched: int) -> str:
    if stop != "end":
        return "partial"
    enough = declared is not None and fetched >= declared * (1 - DECLARED_TOLERANCE)
    if not full_history:
        # источник с потолком: если отдал не меньше заявленного — собрано всё
        return "complete" if enough else "window"
    if declared is None or enough:
        return "complete"
    return "below_declared"


async def sync_branch(collector: Collector, store: Store, sid: str, *, mode: str = "full",
                      run_id: int | None = None) -> BranchResult:
    """Один проход по одному отделению одного источника."""
    src = collector.source
    t0 = asyncio.get_running_loop().time()
    res = BranchResult(src, sid, status="failed")
    prev = store.get_state(src, sid) or {}
    try:
        # «лёгкая» карточка для проверки только новых (Яндекс с подписью: одна JSON-страница);
        # при полном проходе — полная карточка (закрытие, рейтинг, номер ЦБ). Внутри одного
        # sync_many режим общий, так что флаг на адаптере не гоняется между отделениями
        collector.light_card = mode == "new"
        card = await collector.branch(sid)
        store.upsert_source_branch(card)
        res.declared = card.declared_reviews
    except NotFound as e:
        store.set_availability(src, sid, "not_found")
        later = utcnow() + timedelta(days=collector.settings.schedule.full_every_d)
        store.put_state(src, sid, status="failed", completeness="unknown", last_error=str(e),
                        last_sync_at=utcnow(), next_due_at=later, next_full_at=later)
        res.status, res.error = "not_found", str(e)
        return res
    except SourceBlocked as e:
        store.set_availability(src, sid, "blocked")
        store.put_state(src, sid, status="blocked", completeness="blocked", last_error=e.reason,
                        last_sync_at=utcnow())
        if run_id is not None:
            store.log_error(run_id, src, sid, "blocked", f"{e.reason} {e.url or ''}")
        raise
    except CollectorError as e:
        # сбой карточки (сеть, 5xx, заглушка вместо данных) — как сбой прохода: состояние
        # и повтор через retry_h, иначе отделение без состояния выбирается снова и снова
        store.put_state(src, sid, status="failed", completeness="partial", last_error=str(e),
                        errors=int(prev.get("errors") or 0) + 1, last_sync_at=utcnow(),
                        next_due_at=utcnow() + timedelta(hours=collector.settings.schedule.retry_h))
        if run_id is not None:
            store.log_error(run_id, src, sid, "error", str(e))
        res.status, res.completeness, res.error = "failed", "partial", str(e)
        return res

    caps = collector.capabilities
    sort = caps.sorts[0] if caps.sorts else None
    if mode == "new" and caps.extra.get("newest_sort"):
        sort = caps.extra["newest_sort"]          # своя сортировка «сначала новые»
    elif mode == "new" and not caps.extra.get("newest_first", True):
        # выдача не «сначала новые» (Яндекс — по релевантности): новое может стоять
        # на любой странице, поэтому «только новые» = короткий полный проход
        mode = "full"
    # отсечка режима new — от прошлой УСПЕШНОЙ синхронизации; без неё (первый раз,
    # после сбоя) new идёт до конца выдачи — дороже, но ничего не теряет
    cutoff = None
    if mode == "new" and prev.get("status") == "done" and prev.get("last_sync_at"):
        cutoff = _aware(prev["last_sync_at"]) - NEW_OVERLAP
    resume = mode == "full" and prev.get("status") == "running" and prev.get("cursor")
    cursor = prev.get("cursor") if resume else None
    pass_started = _aware(prev.get("pass_started_at")) if resume else utcnow()
    store.put_state(src, sid, status="running", sort=sort, declared_total=res.declared,
                    pass_started_at=pass_started, cursor=cursor, last_error=None)
    fetched = int(prev.get("fetched_total") or 0) if resume else 0
    pages = int(prev.get("pages") or 0) if resume else 0
    oldest_in_pass: datetime | None = None
    stop = "end"
    try:
        async for page in collector.pages(sid, sort=sort, cursor=cursor):
            pages += 1
            fetched += len(page.reviews)
            res.pages += 1
            if res.declared is None and page.declared_total is not None:
                res.declared = page.declared_total
            st = store.save_reviews(page.reviews, seen_at=utcnow())
            res.new += st["new"]
            res.updated += st["updated"]
            for r in page.reviews:
                d = _aware(r.review_date)
                if d and (oldest_in_pass is None or d < oldest_in_pass):
                    oldest_in_pass = d
            # прогресс после каждой страницы — прерывание не теряет пройденное (только у
            # полного прохода: короткий «только новые» не продолжается и не должен затирать
            # «получено за полный проход»)
            if mode == "full":
                store.put_state(src, sid, cursor=page.next_cursor, pages=pages, fetched_total=fetched,
                                declared_total=res.declared)
            # «только новые»: страница целиком знакома, без правок и старше отсечки —
            # дальше только старое (правка старого отзыва поднимает его наверх, поэтому
            # «всё знакомо» без отсечки по времени — не повод остановиться)
            if mode == "new" and page.reviews and st["new"] == 0 and st["updated"] == 0:
                eds = [_aware(r.edited_at or r.review_date) for r in page.reviews]
                eds = [d for d in eds if d]
                if cutoff is not None and (not eds or min(eds) < cutoff):
                    stop = "caught_up"
                    break
            if pages >= collector.cfg.max_pages:
                stop = "max_pages"
                break
    except SourceBlocked as e:
        store.set_availability(src, sid, "blocked")
        store.put_state(src, sid, status="blocked", completeness="blocked", last_error=e.reason,
                        last_sync_at=utcnow())
        if run_id is not None:
            store.log_error(run_id, src, sid, "blocked", f"{e.reason} {e.url or ''}")
        res.status, res.completeness, res.error = "blocked", "blocked", e.reason
        raise
    except CollectorError as e:
        store.put_state(src, sid, status="failed", completeness="partial", last_error=str(e),
                        errors=int(prev.get("errors") or 0) + 1, last_sync_at=utcnow(),
                        next_due_at=utcnow() + timedelta(hours=collector.settings.schedule.retry_h))
        if run_id is not None:
            store.log_error(run_id, src, sid, "error", str(e))
        res.status, res.completeness, res.error = "failed", "partial", str(e)
        res.seconds = round(asyncio.get_running_loop().time() - t0, 1)
        return res

    # «получено» — разные отзывы за проход (с продолжением), а не строки страниц:
    # у Яндекса по релевантности один отзыв бывает на двух страницах
    rows_on_pages = fetched
    fetched = store.count_seen(src, sid, pass_started)
    res.fetched = fetched
    res.dupes = max(0, rows_on_pages - fetched)
    res.stop = stop
    full_pass = mode == "full" and stop == "end"
    if full_pass:
        # пропавшие — только после полного прохода с начала (в т. ч. продолженного) и только
        # если проход видел ВСЁ заявленное: у выдачи с потолком (Яндекс, >600 отзывов, порядок
        # по релевантности) невиденный отзыв не значит удалённый. Предохранители: пустой проход
        # ничего не помечает; «пропало» больше трети хранимого — сбой выдачи, а не удаления
        saw_all = caps.full_history or (res.declared is not None and fetched >= res.declared)
        if fetched > 0 and saw_all:
            cand = store.gone_candidates(src, sid, pass_started)
            stored = store.counts(src, sid)["stored"]
            if cand > max(GONE_MIN_SUSPICIOUS, GONE_MAX_SHARE * stored):
                log.warning("%s %s: «пропало» %d из %d — похоже на сбой выдачи, не помечаю", src, sid, cand, stored)
                if run_id is not None:
                    store.log_error(run_id, src, sid, "gone_guard", f"кандидатов {cand} из {stored}")
            elif cand:
                res.gone = store.mark_gone(src, sid, pass_started)
    cnt = store.counts(src, sid)
    vals = dict(status="done", cursor=None, last_sync_at=utcnow(), stored_total=cnt["stored"],
                newest_review_date=cnt["newest"], oldest_review_date=cnt["oldest"])
    if mode == "full":
        res.completeness = completeness_of(stop, full_history=caps.full_history,
                                           declared=res.declared, fetched=fetched)
        vals.update(completeness=res.completeness, fetched_total=fetched)
        if full_pass:
            vals["last_full_at"] = utcnow()
    else:
        res.completeness = prev.get("completeness") or "incremental"
    vals.update(schedule_next(store, collector, sid, full_pass=full_pass))
    store.put_state(src, sid, **vals)
    res.status = "done"
    res.seconds = round(asyncio.get_running_loop().time() - t0, 1)
    log.info("%s %s: %s, заявлено %s, получено %s (новых %s, изменено %s, пропало %s, повторов %s), "
             "стр. %s, %.1f с", src, sid, res.completeness, res.declared, fetched, res.new, res.updated,
             res.gone, res.dupes, res.pages, res.seconds)
    return res


async def sync_many(collector: Collector, store: Store, ids: list[str], *, mode: str = "full",
                    run_id: int | None = None, stop_check=None) -> RunStats:
    """Много отделений одного источника: параллельность из настроек источника,
    блокировка останавливает оставшиеся задачи этого источника."""
    stats = RunStats()
    sem = asyncio.Semaphore(max(1, collector.cfg.concurrency))
    blocked = asyncio.Event()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    req0 = getattr(getattr(collector, "http", None), "requests", 0)
    last_log = [t0]

    def progress() -> None:
        done, now = len(stats.results), loop.time()
        if done % 25 and now - last_log[0] < 300 and done != len(ids):
            return
        last_log[0] = now
        el = max(1e-6, now - t0)
        req = getattr(getattr(collector, "http", None), "requests", 0) - req0
        left = el / done * (len(ids) - done) if done else 0
        log.info("%s %s: %d/%d отделений, новых отзывов %d, %.2f запроса/с, осталось ~%.1f ч",
                 collector.source, mode, done, len(ids), sum(r.new for r in stats.results),
                 req / el, left / 3600)

    async def one(sid: str) -> None:
        if blocked.is_set():
            return
        async with sem:
            if blocked.is_set():
                return
            # источник поставлен на паузу другим циклом (поиск поймал капчу) — не продолжаем
            if stop_check is not None and stop_check():
                blocked.set()
                stats.blocked.setdefault(collector.source, "пауза источника")
                return
            try:
                stats.results.append(await sync_branch(collector, store, sid, mode=mode, run_id=run_id))
            except SourceBlocked as e:
                stats.blocked[collector.source] = e.reason
                stats.results.append(BranchResult(collector.source, sid, "blocked", "blocked",
                                                  error=e.reason))
                blocked.set()
                log.warning("%s: источник ограничил доступ (%s) — источник остановлен в этом прогоне",
                            collector.source, e.reason)
            except Exception as e:  # noqa: BLE001 — одно отделение не роняет прогон
                log.exception("%s %s: сбой", collector.source, sid)
                if run_id is not None:
                    store.log_error(run_id, collector.source, sid, "crash", repr(e))
                # непредвиденная ошибка (не CollectorError): тоже повтор через retry_h,
                # иначе демон выбирает это отделение снова в каждом цикле
                try:
                    prev = store.get_state(collector.source, sid) or {}
                    store.put_state(collector.source, sid, status="failed", completeness="partial",
                                    last_error=repr(e)[:500], errors=int(prev.get("errors") or 0) + 1,
                                    last_sync_at=utcnow(), cursor=None,
                                    next_due_at=utcnow() + timedelta(hours=collector.settings.schedule.retry_h))
                except Exception:  # noqa: BLE001
                    log.exception("%s %s: не удалось записать состояние сбоя", collector.source, sid)
                stats.results.append(BranchResult(collector.source, sid, "failed", "partial",
                                                  error=repr(e)[:300]))
            progress()

    await asyncio.gather(*(one(s) for s in ids))
    return stats
