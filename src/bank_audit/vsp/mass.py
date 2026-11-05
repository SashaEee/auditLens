"""Массовый сбор и дописывание по всей сети: демон с расписанием.

Три независимых цикла в одном процессе (asyncio):
  • обслуживание — реестр ЦБ (раз в сутки: закрытые/новые ВСП), поиск отделений в 2ГИС
    и Яндексе (раз в месяц, с продолжением после остановки), привязка к реестру;
  • сбор 2ГИС и • сбор Яндекса — каждый берёт из базы, кому пора (Store.due):
    сначала «только новые» по расписанию, затем полные проходы (первичная загрузка,
    ежемесячное обновление правок/ответов/пропавших).

Первичная загрузка и дописывание — один и тот же механизм: у новой карточки нет
состояния → ей пора полный проход; после него расписание (sync.schedule_next) ставит
следующую проверку «только новых» по частоте отзывов отделения. Остановленный демон
при перезапуске продолжает с того же места (курсоры, прогресс обходов — в базе).

Блокировка источника (капча, 401/403): источник встаёт на паузу pause_on_block_h и
дальше работает с частотой × backoff_factor (частота хранится в базе, vsp_kv). Капчу
сборщик не решает, прокси не меняет.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func, select

from .collectors.base import Area, Collector
from .config import Settings
from .http import SourceHttp, set_source_rps
from .models import CollectorError, NotFound, SourceBlocked, utcnow
from .registry.apply import apply_formats, import_registry, link_profiles, match_all
from .registry.cbr import fetch_registry
from .registry.match import haversine_m
from .storage import Store, branches, reviews, source_branches, sync_state
from .sync import sync_many

log = logging.getLogger(__name__)

SOURCES = ("2gis", "yandex")
COVERED_M = 150.0        # точка поиска Яндекса считается покрытой, если карточка сети ближе


def make_collector(source: str, settings: Settings) -> Collector:
    if source == "2gis":
        from .collectors.twogis import TwoGisCollector
        return TwoGisCollector(settings)
    if source == "yandex":
        from .collectors.yandex import YandexMapsCollector
        return YandexMapsCollector(settings)
    raise ValueError(source)


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _older(store: Store, key: str, *, hours: float, now: datetime) -> bool:
    at = _dt(store.kv_get(key))
    return at is None or now - at >= timedelta(hours=hours)


# ── реестр ─────────────────────────────────────────────────────────────────────

async def refresh_registry(store: Store, settings: Settings) -> dict:
    http = SourceHttp("cbr", rps=0.9, timeout_s=60, max_retries=3, user_agent=settings.user_agent)
    try:
        rows = await fetch_registry(http)
    finally:
        await http.close()
    st = await asyncio.to_thread(import_registry, store, rows)
    st["rows"] = len(rows)
    store.kv_set("registry:last", st)
    store.kv_set("registry:at", utcnow().isoformat())
    log.info("реестр ЦБ: %s", st)
    return st


# ── поиск отделений ────────────────────────────────────────────────────────────

def _round_state(store: Store, key: str, every_d: float, now: datetime, force: bool) -> dict | None:
    """Состояние обхода {started, done, finished}. None — обход не нужен (свежий)."""
    st = store.kv_get(key) or {}
    fin = _dt(st.get("finished"))
    if fin and not force and now - fin < timedelta(days=every_d):
        return None
    if fin or not st:                                  # новый круг
        st = {"started": now.isoformat(), "done": [], "finished": None, "found": 0, "errors": 0}
        store.kv_set(key, st)
    return st


async def discover_2gis_all(store: Store, settings: Settings, *, force: bool = False,
                            factory: Callable[[str, Settings], Collector] = make_collector) -> dict:
    """Все населённые пункты 2ГИС (~1 100). Общий набор виденных id: пункты одного проекта
    видят одну выдачу, повтор останавливается через 2 страницы без нового."""
    now = utcnow()
    st = _round_state(store, "discover:2gis", settings.schedule.discover_every_d, now, force)
    if st is None:
        return {"skipped": "свежий обход"}
    apply_rps(store, settings, "2gis")
    g = factory("2gis", settings)
    done = set(st["done"])
    seen: set[str] = set()
    try:
        settl = await g.settlements()                       # type: ignore[attr-defined]
        if not settl:
            # заглушка вместо страницы (устаревший User-Agent) или смена вёрстки: круг не
            # засчитываем, иначе следующий обход — только через месяц
            raise CollectorError("2gis: список населённых пунктов пуст — страница не та (заглушка/вёрстка)")
        # сначала пункты внутри проектов (там крупные города и основная масса отделений)
        settl = sorted(settl, key=lambda s: (s["region_id"] == "0", s["region_id"], s["alias"]))
        for k, s in enumerate(settl, 1):
            if s["alias"] in done:
                continue
            if paused(store, "2gis", utcnow()):
                log.warning("2gis поиск: источник на паузе — продолжим после неё (%d/%d)", k, len(settl))
                return {"paused": True, "done": len(done)}
            n = 0
            try:
                async for b in g.discover(Area(city=s["alias"]), seen=seen):   # type: ignore[call-arg]
                    store.upsert_source_branch(b)
                    n += 1
            except SourceBlocked:
                raise
            except (CollectorError, NotFound) as e:
                st["errors"] += 1
                log.warning("2gis поиск %s: %s", s["alias"], e)
            done.add(s["alias"])
            st["done"] = sorted(done)
            st["found"] += n
            store.kv_set("discover:2gis", st)
            if k % 50 == 0:
                log.info("2gis поиск: пунктов %d/%d, карточек найдено %d", k, len(settl), st["found"])
        if not st["found"]:
            raise CollectorError("2gis: обход не нашёл ни одного отделения — круг не засчитан")
        st["finished"] = utcnow().isoformat()
        store.kv_set("discover:2gis", st)
        log.info("2gis поиск завершён: пунктов %d, карточек %d, ошибок %d", len(settl), st["found"], st["errors"])
        return {"settlements": len(settl), "found": st["found"], "errors": st["errors"]}
    finally:
        await g.close()


async def discover_2gis_private(store: Store, settings: Settings, *, force: bool = False,
                                factory: Callable[[str, Settings], Collector] = make_collector) -> dict:
    """Private Banking в 2ГИС — отдельная организация банка, в выдаче «СберБанк» её нет.
    Один поиск на проект 2ГИС (офисы есть только в крупных городах)."""
    now = utcnow()
    st = _round_state(store, "discover:2gis_private", settings.schedule.discover_every_d, now, force)
    if st is None:
        return {"skipped": "свежий обход"}
    apply_rps(store, settings, "2gis")
    g = factory("2gis", settings)
    done = set(st["done"])
    try:
        settl = await g.settlements()                       # type: ignore[attr-defined]
        if not settl:
            raise CollectorError("2gis: список населённых пунктов пуст — страница не та")
        per_project: dict[str, str] = {}
        for s in settl:
            if s["region_id"] != "0":
                per_project.setdefault(s["region_id"], s["alias"])
        aliases = sorted(per_project.values())
        seen: set[str] = set()
        for a in aliases:
            if a in done:
                continue
            if paused(store, "2gis", utcnow()):
                return {"paused": True, "done": len(done)}
            try:
                async for b in g.discover(Area(city=a), seen=seen, patience=1,   # type: ignore[call-arg]
                                          query="Sberbank Private Banking"):
                    if (b.raw or {}).get("format") == "private":
                        store.upsert_source_branch(b)
                        st["found"] += 1
            except SourceBlocked:
                raise
            except (CollectorError, NotFound) as e:
                st["errors"] += 1
                log.info("2gis Private Banking %s: %s", a, e)
            done.add(a)
            st["done"] = sorted(done)
            store.kv_set("discover:2gis_private", st)
        st["finished"] = utcnow().isoformat()
        store.kv_set("discover:2gis_private", st)
        log.info("2gis Private Banking: проектов %d, офисов %d", len(aliases), st["found"])
        return {"projects": len(aliases), "found": st["found"], "errors": st["errors"]}
    finally:
        await g.close()


async def refresh_banksite(store: Store, settings: Settings, *, force: bool = False) -> dict:
    """Паспорт ВСП с сайта банка (registry/banksite.py): плитки карты, где у нас есть ВСП,
    раз в месяц; затем привязка к реестру по коду ВСП и пересчёт форматов."""
    from .registry.banksite import BankSiteBrowser, fetch_offices, tiles_for_points
    if not settings.bank_site:
        return {"skipped": "VSP_BANK_SITE не задан"}
    now = utcnow()
    if not force and not _older(store, "banksite:at", hours=24 * settings.schedule.banksite_every_d, now=now):
        return {"skipped": "свежий обход"}
    with store.engine.connect() as c:
        pts = [(r[0], r[1]) for r in c.execute(select(source_branches.c.lat, source_branches.c.lon).where(
            source_branches.c.lat.isnot(None)))]
        pts += [(r[0], r[1]) for r in c.execute(select(branches.c.lat, branches.c.lon).where(
            branches.c.lat.isnot(None)))]
    tiles = tiles_for_points(pts)
    if not tiles:
        return {"skipped": "нет точек ВСП с координатами"}
    log.info("сайт банка: плиток %d", len(tiles))

    def progress(done, left, n):
        log.info("сайт банка: плиток пройдено %d, в очереди %d, офисов %d", done, left, n)
    async with BankSiteBrowser(settings.bank_site, rps=settings.bank_site_rps,
                               user_agent=settings.user_agent) as br:
        offices = await fetch_offices(br, tiles, progress=progress)
    if not offices:
        raise CollectorError("сайт банка: обход не вернул ни одного офиса — не засчитан")
    st = await asyncio.to_thread(store.save_profiles, offices, crawled_tiles=set(tiles))
    st["linked"] = await asyncio.to_thread(link_profiles, store)
    st["formats"] = await asyncio.to_thread(apply_formats, store)
    st.update(tiles=len(tiles), offices=len(offices), requests=br.requests)
    store.kv_set("banksite:last", st)
    store.kv_set("banksite:at", utcnow().isoformat())
    log.info("сайт банка: %s", st)
    return st


def search_points(store: Store) -> list[tuple[str, float, float]]:
    """Точки поиска Яндекса: карточки 2ГИС и отделения реестра с координатами,
    без повторов (сетка ~100 м)."""
    with store.engine.connect() as c:
        pts = [(r[0], r[1]) for r in c.execute(select(source_branches.c.lat, source_branches.c.lon).where(
            source_branches.c.source == "2gis", source_branches.c.lat.isnot(None)))]
        pts += [(r[0], r[1]) for r in c.execute(select(branches.c.lat, branches.c.lon).where(
            branches.c.status == "active", branches.c.lat.isnot(None)))]
    out: dict[str, tuple[str, float, float]] = {}
    for lat, lon in pts:
        key = f"{lat:.3f},{lon:.3f}"
        out.setdefault(key, (key, lat, lon))
    return sorted(out.values())


class _Grid:
    """Карточки сети Яндекса с координатами — для проверки «точка уже покрыта»."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], list[tuple[float, float]]] = {}

    @staticmethod
    def _c(lat: float, lon: float) -> tuple[int, int]:
        return int(lat * 100 // 1), int(lon * 50 // 1)

    def add(self, lat: float, lon: float) -> None:
        self.cells.setdefault(self._c(lat, lon), []).append((lat, lon))

    def near(self, lat: float, lon: float, m: float) -> bool:
        ci, cj = self._c(lat, lon)
        return any(haversine_m(lat, lon, a, b) <= m for di in (-1, 0, 1) for dj in (-1, 0, 1)
                   for a, b in self.cells.get((ci + di, cj + dj), ()))


async def discover_yandex_points(store: Store, settings: Settings, *, force: bool = False,
                                 factory: Callable[[str, Settings], Collector] = make_collector) -> dict:
    """Страница сети Яндекса вокруг каждой точки, ещё не покрытой найденными карточками.
    Каждый запрос даёт до 10–19 ближайших отделений, поэтому в плотных городах большая
    часть точек оказывается покрытой соседними запросами."""
    now = utcnow()
    st = _round_state(store, "discover:yandex", settings.schedule.discover_every_d, now, force)
    if st is None:
        return {"skipped": "свежий обход"}
    grid = _Grid()
    with store.engine.connect() as c:
        for lat, lon in c.execute(select(source_branches.c.lat, source_branches.c.lon).where(
                source_branches.c.source == "yandex", source_branches.c.lat.isnot(None))):
            grid.add(lat, lon)
    pts = search_points(store)
    if not pts:
        return {"skipped": "нет точек (карточек 2ГИС и ВСП с координатами)"}
    done = set(st["done"])
    apply_rps(store, settings, "yandex")
    y = factory("yandex", settings)
    queried = 0
    try:
        for k, (key, lat, lon) in enumerate(pts, 1):
            if key in done:
                continue
            if paused(store, "yandex", utcnow()):
                log.warning("yandex поиск: источник на паузе — продолжим после неё (%d/%d)", k, len(pts))
                return {"paused": True, "queried": queried}
            if not grid.near(lat, lon, COVERED_M):
                try:
                    async for b in y.discover(Area(city="", lat=lat, lon=lon, radius_m=500)):
                        store.upsert_source_branch(b)
                        st["found"] += 1
                        if b.lat is not None and b.lon is not None:
                            grid.add(b.lat, b.lon)
                except SourceBlocked:
                    raise
                except (CollectorError, NotFound) as e:
                    st["errors"] += 1
                    log.warning("yandex поиск у %s: %s", key, e)
                queried += 1
                done.add(key)
                if queried % 25 == 0:
                    st["done"] = sorted(done)
                    store.kv_set("discover:yandex", st)
            else:
                done.add(key)
            if k % 500 == 0:
                log.info("yandex поиск: точек %d/%d, запросов %d, карточек %d", k, len(pts), queried, st["found"])
        st["done"] = sorted(done)
        st["finished"] = utcnow().isoformat()
        store.kv_set("discover:yandex", st)
        log.info("yandex поиск завершён: точек %d, запросов %d, карточек %d", len(pts), queried, st["found"])
        return {"points": len(pts), "queried": queried, "found": st["found"], "errors": st["errors"]}
    finally:
        st["done"] = sorted(done)
        store.kv_set("discover:yandex", st)
        await y.close()


# ── циклы демона ───────────────────────────────────────────────────────────────

def _pause(store: Store, source: str, settings: Settings, reason: str) -> None:
    sch = settings.schedule
    if paused(store, source, utcnow()):
        return                                      # уже на паузе: второй цикл не снижает частоту ещё раз
    rps = float(store.kv_get(f"rps:{source}") or settings.source(source).rps)
    new_rps = max(sch.min_rps, round(rps * sch.backoff_factor, 3))
    until = utcnow() + timedelta(hours=sch.pause_on_block_h)
    store.kv_set(f"rps:{source}", new_rps)
    set_source_rps(source, new_rps)
    store.kv_set(f"pause:{source}", until.isoformat())
    store.kv_set(f"blocked:{source}", {"at": utcnow().isoformat(), "reason": reason, "rps_before": rps,
                                        "rps_after": new_rps})
    log.warning("%s: ограничил доступ (%s) — пауза до %s, частота %.3f → %.3f запроса/с",
                source, reason, until.isoformat(timespec="minutes"), rps, new_rps)


def apply_rps(store: Store, settings: Settings, source: str) -> None:
    """Частота источника после блокировок хранится в базе — её берут все клиенты источника."""
    rps = store.kv_get(f"rps:{source}")
    if rps:
        settings.source(source).rps = float(rps)
        set_source_rps(source, float(rps))


def paused(store: Store, source: str, now: datetime) -> float:
    """Сколько секунд ещё пауза у источника (0 — работает)."""
    until = _dt(store.kv_get(f"pause:{source}"))
    return max(0.0, (until - now).total_seconds()) if until else 0.0


async def source_cycle(store: Store, settings: Settings, source: str, *,
                       factory: Callable[[str, Settings], Collector] = make_collector,
                       col: Collector | None = None) -> dict:
    """Один проход цикла источника: кому пора «только новые», затем полные проходы.
    Возвращает сводку (пустую, если делать нечего). col — долгоживущий адаптер демона
    (сессия Яндекса и ключ 2ГИС переживают циклы); без него — разовый, закрывается здесь."""
    now = utcnow()
    if paused(store, source, now):
        return {"paused": True}
    due = store.due(source, now, limit=settings.schedule.batch)
    if not due["new"] and not due["full"]:
        return {}
    own = col is None
    if own:
        apply_rps(store, settings, source)
        col = factory(source, settings)
    out: dict = {}
    try:
        for mode in ("new", "full"):
            ids = due[mode]
            if not ids:
                continue
            run_id = store.start_run(f"daemon:{source}:{mode}", {"ids": len(ids)})
            st = await sync_many(col, store, ids, mode=mode, run_id=run_id,
                                 stop_check=lambda: paused(store, source, utcnow()) > 0)
            summary = st.summary()
            store.finish_run(run_id, "blocked" if st.blocked else "done", summary)
            out[mode] = summary
            if st.blocked:
                if st.blocked.get(source) != "пауза источника":
                    _pause(store, source, settings, st.blocked.get(source, "blocked"))
                break
    finally:
        if own:
            await col.close()
    return out


COLLECTOR_MAX_AGE_S = 6 * 3600     # адаптер демона пересоздаётся раз в 6 ч или при смене частоты


async def _forever(name: str, body: Callable[[], "asyncio.Future"], idle_s: float) -> None:
    """Повторять body; упавший шаг не роняет демон (журнал + пауза)."""
    while True:
        try:
            busy = await body()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("демон: цикл %s упал — повтор через %.0f с", name, idle_s)
            busy = False
        if not busy:
            await asyncio.sleep(idle_s)


async def daemon(store: Store, settings: Settings,
                 factory: Callable[[str, Settings], Collector] = make_collector) -> None:
    sch = settings.schedule

    async def step(name: str, coro_fn, retry_h: float = 1.0):
        """Шаг обслуживания: сбой одного шага не останавливает остальные; повтор не
        раньше чем через retry_h (а не каждые sleep_s)."""
        if not _older(store, f"retry:{name}", hours=retry_h, now=utcnow()) and store.kv_get(f"failed:{name}"):
            return None
        try:
            res = await coro_fn()
            store.kv_set(f"failed:{name}", None)
            return res
        except SourceBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("обслуживание: шаг %s упал — повтор через %.0f ч", name, retry_h)
            store.kv_set(f"failed:{name}", {"at": utcnow().isoformat(), "error": repr(e)[:500]})
            store.kv_set(f"retry:{name}", utcnow().isoformat())
            return None

    async def maintenance() -> bool:
        now = utcnow()
        if _older(store, "registry:at", hours=sch.registry_every_h, now=now):
            if await step("registry", lambda: refresh_registry(store, settings)) is not None:
                await step("match", lambda: _match(store))
        for source, fn in (("2gis", discover_2gis_all), ("2gis", discover_2gis_private),
                           ("yandex", discover_yandex_points)):
            if not settings.source(source).enabled or paused(store, source, now):
                continue
            if fn is not discover_2gis_all and not (store.kv_get("discover:2gis") or {}).get("finished"):
                continue                                   # после основного обхода 2ГИС
            try:
                res = await step(f"discover:{fn.__name__}", lambda fn=fn: fn(store, settings, factory=factory))
            except SourceBlocked as e:
                _pause(store, source, settings, e.reason)
                continue
            if res and not res.get("skipped") and not res.get("paused"):
                await step("match", lambda: _match(store))
        if _older(store, "match:at", hours=sch.match_every_h, now=now):
            await step("match", lambda: _match(store))
        # паспорт ВСП с сайта банка — когда есть точки (после обхода 2ГИС), раз в месяц
        if settings.bank_site and (store.kv_get("discover:2gis") or {}).get("finished"):
            await step("banksite", lambda: refresh_banksite(store, settings), retry_h=24)
        return False

    def src_loop(source: str):
        held: dict = {"col": None, "rps": None, "t": 0.0}

        async def collector() -> Collector:
            apply_rps(store, settings, source)
            rps = settings.source(source).rps
            loop_t = asyncio.get_running_loop().time()
            if held["col"] is None or held["rps"] != rps or loop_t - held["t"] > COLLECTOR_MAX_AGE_S:
                if held["col"] is not None:
                    await held["col"].close()
                held.update(col=factory(source, settings), rps=rps, t=loop_t)
            return held["col"]

        async def body() -> bool:
            if not settings.source(source).enabled:
                return False
            left = paused(store, source, utcnow())
            if left:
                await asyncio.sleep(min(left, sch.sleep_s))
                return True
            d = store.due(source, utcnow(), limit=1)
            if not d["full"] and not d["new"]:
                return False
            res = await source_cycle(store, settings, source, factory=factory, col=await collector())
            return bool(res) and not res.get("paused")
        return body

    log.info("демон ВСП запущен: источники %s, расписание %s", [s for s in SOURCES if settings.source(s).enabled],
             sch)
    await asyncio.gather(_forever("обслуживание", maintenance, sch.sleep_s),
                         *(_forever(s, src_loop(s), sch.sleep_s) for s in SOURCES))


async def _match(store: Store) -> dict:
    res = await asyncio.to_thread(match_all, store)
    res["formats"] = await asyncio.to_thread(apply_formats, store)
    store.kv_set("match:at", utcnow().isoformat())
    store.kv_set("match:last", res)
    log.info("привязка: %s", {k: v for k, v in res.items() if k != "rows_changed"})
    return res


# ── состояние ──────────────────────────────────────────────────────────────────

def status(store: Store) -> dict:
    """Сводка для наблюдения: реестр, карточки, полнота, расписание, паузы, обходы."""
    now = utcnow()
    out: dict = {"at": now.isoformat(timespec="seconds")}
    with store.engine.connect() as c:
        out["registry"] = dict(c.execute(select(branches.c.status, func.count()).group_by(branches.c.status)).all())
        for src in SOURCES:
            cards = c.execute(select(func.count()).where(source_branches.c.source == src)).scalar()
            linked = c.execute(select(func.count()).where(source_branches.c.source == src,
                                                          source_branches.c.branch_id.isnot(None))).scalar()
            states = dict(c.execute(select(sync_state.c.status, func.count()).where(sync_state.c.source == src)
                                    .group_by(sync_state.c.status)).all())
            compl = dict(c.execute(select(sync_state.c.completeness, func.count()).where(
                sync_state.c.source == src, sync_state.c.last_full_at.isnot(None))
                .group_by(sync_state.c.completeness)).all())
            decl, got = c.execute(select(func.sum(sync_state.c.declared_total), func.sum(sync_state.c.fetched_total))
                                  .where(sync_state.c.source == src, sync_state.c.last_full_at.isnot(None))).one()
            revs = c.execute(select(func.count()).where(reviews.c.source == src)).scalar()
            new24 = c.execute(select(func.count()).where(reviews.c.source == src,
                                                         reviews.c.first_seen_at >= now - timedelta(days=1))).scalar()
            full_done = c.execute(select(func.count()).where(sync_state.c.source == src,
                                                             sync_state.c.last_full_at.isnot(None))).scalar()
            out[src] = {"cards": cards, "linked": linked, "fully_synced": full_done, "states": states,
                        "completeness": {str(k): v for k, v in compl.items()},
                        "declared": int(decl or 0), "fetched": int(got or 0), "reviews": revs,
                        "reviews_new_24h": new24}
        cov = {}
        for src in SOURCES:
            cov[src] = c.execute(select(func.count(func.distinct(source_branches.c.branch_id))).where(
                source_branches.c.source == src, source_branches.c.branch_id.isnot(None))).scalar()
        out["registry_coverage"] = cov
    for src in SOURCES:
        due = store.due(src, now, limit=100000)
        out[src].update({"due_full": len(due["full"]), "due_new": len(due["new"]),
                         "rps": store.kv_get(f"rps:{src}"), "paused_s": int(paused(store, src, now)),
                         "blocked_last": store.kv_get(f"blocked:{src}")})
        d = store.kv_get(f"discover:{src}") or {}
        out[src]["discover"] = {k: (len(v) if k == "done" else v) for k, v in d.items()}
    out["registry_at"] = store.kv_get("registry:at")
    out["match_last"] = store.kv_get("match:last")
    return out
