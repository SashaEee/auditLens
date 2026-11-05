"""Обогащение справочника ВСП: субъект РФ и часовой пояс.

Порядок для каждого ВСП:
1. написание региона из адреса ЦБ распознано (regions.subject_code) — «сильный» субъект;
2. региона в адресе не было (парсер подставил город) — субъект по городу считается
   «слабым»: город «Кировск» не Кировская область. Тогда решает ГОСБ: если 80 %+ его ВСП
   с сильным субъектом в одном субъекте — берём его, иначе слабый;
3. ничего не распознано — субъект большинства ГОСБ, иначе ближайшего ВСП с известным
   субъектом (по координатам, до 150 км).
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict

from sqlalchemy import select, update

from ..regions import subject_code, utc_offset
from ..storage import Store, branches
from .match import haversine_m

log = logging.getLogger(__name__)

GOSB_MAJORITY = 0.8
NEAREST_MAX_M = 150_000
WRITE_CHUNK = 500


def backfill_gosb(store: Store) -> int:
    """ГОСБ из кода ВСП в названии — для строк, загруженных до появления столбца."""
    from .banksite import vsp_key
    with store.engine.connect() as c:
        todo = [(bid, vsp_key(name)) for bid, name in c.execute(
            select(branches.c.branch_id, branches.c.name).where(branches.c.gosb.is_(None)))]
    todo = [(bid, k.split("/")[0]) for bid, k in todo if k]
    for k in range(0, len(todo), WRITE_CHUNK):
        with store.engine.begin() as c:
            for bid, g in todo[k:k + WRITE_CHUNK]:
                c.execute(update(branches).where(branches.c.branch_id == bid).values(gosb=g))
    return len(todo)


def enrich_branches(store: Store) -> dict:
    filled = backfill_gosb(store)
    with store.engine.connect() as c:
        rows = list(c.execute(select(branches.c.branch_id, branches.c.region, branches.c.city, branches.c.gosb,
                                     branches.c.lat, branches.c.lon, branches.c.subject, branches.c.utc_offset)))
    strong: dict[int, str] = {}
    weak: dict[int, str] = {}
    for r in rows:
        fallback = not r.region or (r.city and r.region == r.city)
        code = subject_code(r.region) or (subject_code(r.city) if r.city else None)
        if code:
            (weak if fallback else strong)[r.branch_id] = code
    by_gosb: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.gosb and r.branch_id in strong:
            by_gosb[r.gosb][strong[r.branch_id]] += 1
    gosb_major = {}
    for g, cnt in by_gosb.items():
        code, n = cnt.most_common(1)[0]
        if n >= GOSB_MAJORITY * sum(cnt.values()):
            gosb_major[g] = code
    st: Counter = Counter()
    out: dict[int, str | None] = {}
    for r in rows:
        if r.branch_id in strong:
            out[r.branch_id] = strong[r.branch_id]
            st["адрес"] += 1
        elif r.gosb in gosb_major:
            out[r.branch_id] = gosb_major[r.gosb]
            st["ГОСБ"] += 1
        elif r.branch_id in weak:
            out[r.branch_id] = weak[r.branch_id]
            st["город"] += 1
        else:
            out[r.branch_id] = None
    # ближайший сосед по координатам — для оставшихся
    grid: dict[tuple[int, int], list] = defaultdict(list)
    for r in rows:
        if out.get(r.branch_id) and r.lat is not None:
            grid[(int(r.lat), int(r.lon))].append((r.lat, r.lon, out[r.branch_id]))
    for r in rows:
        if out[r.branch_id] or r.lat is None:
            continue
        cand = [(haversine_m(r.lat, r.lon, la, lo), code)
                for i in (-1, 0, 1) for j in (-2, -1, 0, 1, 2)
                for la, lo, code in grid.get((int(r.lat) + i, int(r.lon) + j), ())]
        if cand:
            d, code = min(cand)
            if d <= NEAREST_MAX_M:
                out[r.branch_id] = code
                st["сосед"] += 1
    st["не определён"] = sum(1 for v in out.values() if not v)
    changed = [(r.branch_id, out[r.branch_id]) for r in rows
               if out[r.branch_id] != r.subject or utc_offset(out[r.branch_id]) != r.utc_offset]
    for k in range(0, len(changed), WRITE_CHUNK):
        with store.engine.begin() as c:
            for bid, code in changed[k:k + WRITE_CHUNK]:
                c.execute(update(branches).where(branches.c.branch_id == bid)
                          .values(subject=code, utc_offset=utc_offset(code)))
    st["записано"] = len(changed)
    st["ГОСБ из кода"] = filled
    st["субъектов"] = len({v for v in out.values() if v})
    return dict(st)


def centroids(store: Store) -> dict[str, tuple[float, float]]:
    """Средние координаты ВСП субъекта — для плиточной карты (regions.tile_grid)."""
    acc: dict[str, list] = defaultdict(list)
    with store.engine.connect() as c:
        for code, lat, lon in c.execute(select(branches.c.subject, branches.c.lat, branches.c.lon).where(
                branches.c.subject.isnot(None), branches.c.lat.isnot(None), branches.c.status != "closed")):
            acc[code].append((lat, lon))
    return {k: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for k, v in acc.items()}

