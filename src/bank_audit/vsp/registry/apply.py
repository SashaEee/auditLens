"""Справочник отделений: загрузка реестра ЦБ и привязка карточек источников.

Порядок привязки карточки источника (2ГИС, Яндекс, Домклик) к отделению справочника:
0. номер подразделения по ЦБ в самой карточке (у Яндекса — признак nomer_sp_cbr,
   совпадает с FILIAL_NUM реестра) — привязка auto (match_method=registry_code), точная;
1. ключ адреса (город|улица|дом) совпал ровно с одним отделением справочника —
   привязка auto (match_method=registry_address);
   совпал с несколькими (два ВСП по одному адресу) — conflict, без привязки;
2. иначе — через карточку другого источника, уже привязанную и стоящую в том же
   здании (registry/match.match_sources, взаимно-лучшая пара) — auto (cross_source);
3. иначе — без привязки (match_status=none): попадает в отчёт на разбор.
Отделение справочника получает координаты привязанной карточки (у ЦБ их нет).
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select, update

from ..models import utcnow
from ..storage import Store, branches, source_branches
from .cbr import RegistryRow
from .match import Item, address_key, match_sources, norm_city

log = logging.getLogger(__name__)


WRITE_CHUNK = 500   # карточек на транзакцию записи привязки
CLOSE_GUARD = 0.9   # закрываем пропавшие, только если выгрузка не меньше 90 % прежней


def import_registry(store: Store, rows: list[RegistryRow], source: str = "cbr") -> dict:
    """Загрузить/обновить отделения справочника из реестра (upsert по registry_ref).
    Отделение, пропавшее из реестра ЦБ, получает status=closed (ЦБ закрытые просто
    удаляет из списка — без ежедневного снимка закрытие не увидеть). Защита от неполной
    выгрузки: если строк меньше 90 % прежнего, закрытие не делается."""
    st = {"new": 0, "updated": 0, "closed": 0}
    with store.engine.begin() as c:
        have = {r.registry_ref: r.branch_id for r in c.execute(
            select(branches.c.registry_ref, branches.c.branch_id).where(branches.c.registry_source == source))}
        open_now = {r[0] for r in c.execute(select(branches.c.registry_ref).where(
            branches.c.registry_source == source, branches.c.status != "closed"))}
        refs = {r.ref for r in rows}
        if open_now and len(refs) >= CLOSE_GUARD * len(open_now):
            gone = open_now - refs
            if gone:
                c.execute(update(branches).where(branches.c.registry_source == source,
                                                 branches.c.registry_ref.in_(gone))
                          .values(status="closed", updated_at=utcnow()))
                st["closed"] = len(gone)
        elif open_now:
            log.warning("реестр: выгрузка %d строк при %d открытых — закрытие пропущено", len(refs), len(open_now))
        for r in rows:
            vals = dict(region=r.region, city=r.city, name=r.name, address=r.address, address_key=r.akey,
                        registry_num=(r.number or "").strip() or None,
                        status="active" if r.kind == "office" else r.kind, updated_at=utcnow())
            if r.ref in have:
                c.execute(update(branches).where(branches.c.branch_id == have[r.ref]).values(**vals))
                st["updated"] += 1
            else:
                c.execute(branches.insert().values(registry_source=source, registry_ref=r.ref,
                                                   created_at=utcnow(), **vals))
                st["new"] += 1
    return st


def _raw(x) -> dict:
    try:
        return json.loads(x["raw_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def match_all(store: Store) -> dict:
    """Привязать все карточки источников к справочнику. Повторный запуск не трогает
    подтверждённые вручную привязки (match_status=confirmed)."""
    st: dict = defaultdict(int)
    with store.engine.connect() as c:
        reg = list(c.execute(select(branches.c.branch_id, branches.c.address_key, branches.c.lat,
                                    branches.c.lon).where(branches.c.status == "active")))
        by_num = {r.registry_num: (r.branch_id, r.status, r.city) for r in c.execute(
            select(branches.c.registry_num, branches.c.branch_id, branches.c.status, branches.c.city)
            .where(branches.c.registry_num.isnot(None)))}
        closed = {r[0] for r in c.execute(select(branches.c.branch_id).where(branches.c.status == "closed"))}
        cards = list(c.execute(select(source_branches)).mappings())
    by_key: dict[str, list[int]] = defaultdict(list)
    for b in reg:
        if b.address_key:
            by_key[b.address_key].append(b.branch_id)
    assign: dict[tuple[str, str], tuple[int | None, str, str, float | None]] = {}
    for x in cards:
        if x["match_status"] == "confirmed":
            continue
        num = _raw(x).get("cbr_num")
        if num and num in by_num:
            bid, status, rcity = by_num[num]
            same_city = not (rcity and x["city"]) or norm_city(rcity) == norm_city(x["city"])
            # закрытый ВСП — тоже он: история отзывов закрытого отделения остаётся при нём
            if status in ("active", "closed") and same_city:
                assign[(x["source"], x["source_branch_id"])] = (bid, "registry_code", "auto", 1.0)
                st["by_code"] += 1
            else:
                # номер есть, но по реестру это передвижной пункт / КИЦ / другой город —
                # карточку не склеиваем, отдаём на разбор (пример: номер автомобиля-офиса,
                # который по реестру ездит по сёлам района, у стационарной карточки в городе)
                assign[(x["source"], x["source_branch_id"])] = (None, "registry_code", "conflict", None)
                st["code_conflict"] += 1
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
    # ВСП закрылся (пропал из реестра ЦБ): его карточки и вся история отзывов остаются
    # привязанными к нему — новой привязки нет, а прежняя указывает на закрытое отделение
    for x in cards:
        key = (x["source"], x["source_branch_id"])
        if x["match_status"] != "confirmed" and not (assign.get(key) or (None,))[0] \
                and x["branch_id"] in closed:
            assign[key] = (x["branch_id"], x["match_method"], x["match_status"], x["match_score"])
            st["kept_closed"] += 1
    # пишем только изменившееся: на всей сети карточек ~20 тыс., отзывов сотни тысяч.
    # Короткими транзакциями по WRITE_CHUNK карточек: сборщик параллельно пишет отзывы тех же
    # отделений и не должен ждать одну многоминутную транзакцию привязки
    from ..storage import reviews
    with store.engine.connect() as c:
        no_geo = {r[0] for r in c.execute(select(branches.c.branch_id).where(branches.c.lat.is_(None)))}
    for k0 in range(0, len(cards), WRITE_CHUNK):
        with store.engine.begin() as c:
            for x in cards[k0:k0 + WRITE_CHUNK]:
                key = (x["source"], x["source_branch_id"])
                if x["match_status"] == "confirmed":
                    st["confirmed"] += 1
                    continue
                bid, method, status, score = assign.get(key, (None, None, "none", None))
                st[f"{key[0]}:{status}"] += 1
                changed_bid = bid != x["branch_id"]
                if changed_bid or method != x["match_method"] or status != x["match_status"] \
                        or score != x["match_score"]:
                    c.execute(update(source_branches).where(
                        source_branches.c.source == key[0], source_branches.c.source_branch_id == key[1]
                    ).values(branch_id=bid, match_method=method, match_status=status, match_score=score))
                    st["rows_changed"] += 1
                if bid and bid in no_geo and x["lat"] is not None:
                    c.execute(update(branches).where(branches.c.branch_id == bid, branches.c.lat.is_(None))
                              .values(lat=x["lat"], lon=x["lon"]))
                    no_geo.discard(bid)
                # отзывы наследуют привязку карточки (и отвязку, если привязка снята)
                if changed_bid:
                    c.execute(update(reviews).where(reviews.c.source == key[0],
                                                    reviews.c.source_branch_id == key[1])
                              .values(branch_id=bid))
    # сверка: отзывы, записанные сборщиком, пока шла привязка (параллельно, другой поток),
    # могли получить прежнюю привязку карточки — доводим до текущей
    sb = source_branches
    cur = (select(sb.c.branch_id).where(sb.c.source == reviews.c.source,
                                        sb.c.source_branch_id == reviews.c.source_branch_id)
           .scalar_subquery())
    with store.engine.begin() as c:
        st["reviews_relinked"] = c.execute(update(reviews).where(
            reviews.c.first_seen_at >= utcnow() - timedelta(days=2),
            reviews.c.branch_id.is_distinct_from(cur)).values(branch_id=cur)).rowcount or 0
    return dict(st)
