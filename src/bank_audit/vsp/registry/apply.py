"""Справочник отделений: загрузка реестра ЦБ и привязка карточек источников.

Порядок привязки карточки источника (2ГИС, Яндекс, Домклик) к отделению справочника:
1. ключ адреса (город|улица|дом) совпал ровно с одним отделением справочника —
   привязка auto (match_method=registry_address);
   совпал с несколькими (два ВСП по одному адресу) — conflict, без привязки;
2. иначе — через карточку другого источника, уже привязанную и стоящую в том же
   здании (registry/match.match_sources, взаимно-лучшая пара) — auto (cross_source);
3. иначе — без привязки (match_status=none): попадает в отчёт на разбор.
Отделение справочника получает координаты привязанной карточки (у ЦБ их нет).
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select, update

from ..models import utcnow
from ..storage import Store, branches, source_branches
from .cbr import RegistryRow
from .match import Item, address_key, match_sources

log = logging.getLogger(__name__)


def import_registry(store: Store, rows: list[RegistryRow], source: str = "cbr") -> dict:
    """Загрузить/обновить отделения справочника из реестра (upsert по registry_ref)."""
    st = {"new": 0, "updated": 0}
    with store.engine.begin() as c:
        have = {r.registry_ref: r.branch_id for r in c.execute(
            select(branches.c.registry_ref, branches.c.branch_id).where(branches.c.registry_source == source))}
        for r in rows:
            vals = dict(region=r.region, city=r.city, name=r.name, address=r.address, address_key=r.akey,
                        status="active" if r.kind == "office" else r.kind, updated_at=utcnow())
            if r.ref in have:
                c.execute(update(branches).where(branches.c.branch_id == have[r.ref]).values(**vals))
                st["updated"] += 1
            else:
                c.execute(branches.insert().values(registry_source=source, registry_ref=r.ref,
                                                   created_at=utcnow(), **vals))
                st["new"] += 1
    return st


def match_all(store: Store) -> dict:
    """Привязать все карточки источников к справочнику. Повторный запуск не трогает
    подтверждённые вручную привязки (match_status=confirmed)."""
    st: dict = defaultdict(int)
    with store.engine.connect() as c:
        reg = list(c.execute(select(branches.c.branch_id, branches.c.address_key, branches.c.lat,
                                    branches.c.lon).where(branches.c.status == "active")))
        cards = list(c.execute(select(source_branches)).mappings())
    by_key: dict[str, list[int]] = defaultdict(list)
    for b in reg:
        if b.address_key:
            by_key[b.address_key].append(b.branch_id)
    assign: dict[tuple[str, str], tuple[int | None, str, str, float | None]] = {}
    for x in cards:
        if x["match_status"] == "confirmed":
            continue
        k = address_key(x["address"], x["city"])
        ids = by_key.get(k or "", [])
        if len(ids) == 1:
            assign[(x["source"], x["source_branch_id"])] = (ids[0], "registry_address", "auto", 1.0)
        elif len(ids) > 1:
            assign[(x["source"], x["source_branch_id"])] = (None, "registry_address", "conflict", None)
    # перекрёстно: непривязанные карточки — через привязанные другого источника в том же здании
    by_src: dict[str, list[dict]] = defaultdict(list)
    for x in cards:
        by_src[x["source"]].append(x)
    srcs = sorted(by_src)
    for i, sa in enumerate(srcs):
        for sb in srcs[i + 1:]:
            A = [Item(sa, x["source_branch_id"], x["lat"], x["lon"], x["address"], x["city"], x["name"])
                 for x in by_src[sa]]
            B = [Item(sb, x["source_branch_id"], x["lat"], x["lon"], x["address"], x["city"], x["name"])
                 for x in by_src[sb]]
            for m in match_sources(A, B):
                if m.status != "auto":
                    st["cross_conflict"] += 1
                    continue
                ka, kb = (m.a.source, m.a.sid), (m.b.source, m.b.sid)
                ga, gb = assign.get(ka), assign.get(kb)
                if ga and ga[0] and not (gb and gb[0]):
                    assign[kb] = (ga[0], "cross_source", "auto", m.score)
                elif gb and gb[0] and not (ga and ga[0]):
                    assign[ka] = (gb[0], "cross_source", "auto", m.score)
    with store.engine.begin() as c:
        for x in cards:
            key = (x["source"], x["source_branch_id"])
            if x["match_status"] == "confirmed":
                st["confirmed"] += 1
                continue
            bid, method, status, score = assign.get(key, (None, None, "none", None))
            c.execute(update(source_branches).where(
                source_branches.c.source == key[0], source_branches.c.source_branch_id == key[1]
            ).values(branch_id=bid, match_method=method, match_status=status, match_score=score))
            st[f"{key[0]}:{status}"] += 1
            if bid and x["lat"] is not None:
                c.execute(update(branches).where(branches.c.branch_id == bid, branches.c.lat.is_(None))
                          .values(lat=x["lat"], lon=x["lon"]))
        # отзывы наследуют привязку карточки
        from ..storage import reviews
        for x in cards:
            bid = assign.get((x["source"], x["source_branch_id"]), (None,))[0]
            if bid:
                c.execute(update(reviews).where(reviews.c.source == x["source"],
                                                reviews.c.source_branch_id == x["source_branch_id"])
                          .values(branch_id=bid))
    return dict(st)
