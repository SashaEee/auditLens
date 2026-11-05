"""Справочник отделений: загрузка реестра ЦБ и привязка карточек источников.

Порядок привязки карточки источника (2ГИС, Яндекс, Домклик) к отделению справочника:
0. номер подразделения по ЦБ в самой карточке (у Яндекса — признак nomer_sp_cbr,
   совпадает с FILIAL_NUM реестра) — привязка auto (match_method=registry_code), точная;
1. ключ адреса (город|улица|дом) совпал ровно с одним отделением справочника —
   привязка auto (match_method=registry_address);
   совпал с несколькими (два ВСП по одному адресу) — conflict, без привязки;
2. иначе — по офису с сайта банка: ближайший стационарный офис с кодом ВСП не дальше
   SITE_GEO_M и без соседа-другого ВСП рядом — auto (site_geo). Контроль 05.11 на уже
   привязанных карточках 2ГИС: при расстоянии до 60 м тот же ВСП в 3 244 из 3 258 (99,6 %);
3. иначе — через карточку другого источника, уже привязанную и стоящую в том же
   здании (registry/match.match_sources, взаимно-лучшая пара) — auto (cross_source);
4. иначе — без привязки (match_status=none): попадает в отчёт на разбор.
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
from .banksite import vsp_key
from .cbr import RegistryRow
from .match import GEO_STRONG_M, Item, address_key, haversine_m, match_sources, norm_city

log = logging.getLogger(__name__)


WRITE_CHUNK = 500   # карточек на транзакцию записи привязки
SITE_GEO_M = 60.0   # офис сайта банка не дальше — та же точка обслуживания
SITE_GEO_STRICT_M = 30.0   # для карточек с конфликтом по адресу — только совсем рядом
SITE_GEO_GAP_M = 30.0      # другой ВСП рядом: второй офис должен быть дальше на столько и вдвое
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
            key = vsp_key(r.name)
            vals = dict(region=r.region, city=r.city, name=r.name, address=r.address, address_key=r.akey,
                        registry_num=(r.number or "").strip() or None,
                        gosb=key.split("/")[0] if key else None, open_date=r.open_date,
                        parent_name=(r.parent or "").strip() or None,
                        status="active" if r.kind == "office" else r.kind, updated_at=utcnow())
            if r.ref in have:
                c.execute(update(branches).where(branches.c.branch_id == have[r.ref]).values(**vals))
                st["updated"] += 1
            else:
                c.execute(branches.insert().values(registry_source=source, registry_ref=r.ref,
                                                   created_at=utcnow(), **vals))
                st["new"] += 1
    return st


class _Offices:
    """Стационарные офисы сайта банка, привязанные к ВСП, — в сетке для поиска ближайшего."""

    def __init__(self, rows):
        self.grid: dict[tuple[int, int], list] = defaultdict(list)
        for bid, lat, lon in rows:
            self.grid[(round(lat * 100), round(lon * 50))].append((bid, lat, lon))

    def nearest(self, lat, lon, limit_m: float):
        """(branch_id, метры) ближайшего офиса не дальше limit_m; "ambiguous" — рядом другой
        ВСП почти так же близко (два офиса в одном торговом центре); None — ничего рядом."""
        if lat is None or lon is None:
            return None
        ci, cj = round(lat * 100), round(lon * 50)
        cand = sorted((haversine_m(lat, lon, la, lo), bid)
                      for i in (ci - 1, ci, ci + 1) for j in (cj - 1, cj, cj + 1)
                      for bid, la, lo in self.grid.get((i, j), ()))
        if not cand or cand[0][0] > limit_m:
            return None
        d, bid = cand[0]
        other = next((dd for dd, b in cand[1:] if b != bid), None)
        if other is not None and other < max(2 * d, d + SITE_GEO_GAP_M):
            return "ambiguous"
        return bid, d


def _site_offices(store: Store) -> _Offices:
    from ..storage import branch_profiles as bp
    rows = []
    with store.engine.connect() as c:
        for bid, lat, lon, fm in c.execute(select(bp.c.branch_id, bp.c.lat, bp.c.lon, bp.c.formats).where(
                bp.c.branch_id.isnot(None), bp.c.status == "active", bp.c.lat.isnot(None))):
            if "mobile" not in json.loads(fm or "[]"):
                rows.append((bid, lat, lon))
    return _Offices(rows)


CHILD_FORMATS = ("sber_first", "private")


def is_child(x) -> bool:
    """Дочерняя карточка особого формата (СберПервый, Private Banking) внутри ВСП."""
    return _raw(x).get("format") in CHILD_FORMATS


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
    # по офисам сайта банка: точные координаты и код ВСП в названии
    offices = _site_offices(store)
    for x in cards:
        key = (x["source"], x["source_branch_id"])
        cur = assign.get(key)
        if x["match_status"] == "confirmed" or is_child(x) or (cur and cur[0]):
            continue
        if cur and cur[1] == "registry_code":      # номер ЦБ указал на передвижной пункт / другой город
            continue
        hit = offices.nearest(x["lat"], x["lon"], SITE_GEO_STRICT_M if cur else SITE_GEO_M)
        if hit == "ambiguous":
            st["site_geo_ambiguous"] += 1
        elif hit:
            bid, d = hit
            assign[key] = (bid, "site_geo", "auto", round(1 - d / (2 * SITE_GEO_M), 3))
            st["site_geo"] += 1
    # перекрёстно: непривязанные карточки — через привязанные другого источника в том же здании.
    # Дочерние карточки особых форматов (СберПервый, Private Banking в 2ГИС) в пары не идут:
    # иначе карточка Яндекса могла бы сойтись с ними, а не с основной карточкой ВСП
    by_src: dict[str, list[dict]] = defaultdict(list)
    for x in cards:
        if not is_child(x):
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
    # дочерние карточки без своей привязки — к основной карточке того же источника в том же
    # здании (тот же ключ адреса, иначе ближе GEO_STRONG_M)
    mains = [x for x in cards if not is_child(x)]
    for x in cards:
        key = (x["source"], x["source_branch_id"])
        if not is_child(x) or x["match_status"] == "confirmed" or (assign.get(key) or (None,))[0]:
            continue
        k = address_key(x["address"], x["city"])
        best = None
        for m in mains:
            if m["source"] != x["source"]:
                continue
            mb = (assign.get((m["source"], m["source_branch_id"])) or (None,))[0]
            if not mb:
                continue
            same = k and address_key(m["address"], m["city"]) == k
            near = (None not in (x["lat"], x["lon"], m["lat"], m["lon"])
                    and haversine_m(x["lat"], x["lon"], m["lat"], m["lon"]) <= GEO_STRONG_M)
            if same or near:
                best = mb
                break
        if best:
            assign[key] = (best, "parent_card", "auto", 0.9)
            st["child_linked"] += 1
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


# ── паспорт с сайта банка и форматы ВСП ─────────────────────────────────────

def link_profiles(store: Store) -> dict:
    """Офисы сайта банка → ВСП реестра по коду ВСП («ГОСБ/номер» без ведущих нулей).
    Код пересчитывается из названия при каждой привязке — правка разбора кода применяется
    без повторного обхода сайта. Остановки передвижных офисов к ВСП не привязываются
    (в реестре передвижной пункт записан по госномеру машины) и считаются отдельно."""
    from ..storage import branch_profiles as bp
    st = {"linked": 0, "unlinked": 0, "ambiguous": 0, "mobile_stops": 0, "mobile_routes": 0}
    routes: set[str] = set()
    with store.engine.connect() as c:
        idx: dict[str, list[int]] = defaultdict(list)
        for bid, name in c.execute(select(branches.c.branch_id, branches.c.name)):
            k = vsp_key(name)
            if k:
                idx[k].append(bid)
        profs = list(c.execute(select(bp.c.site_id, bp.c.name, bp.c.vsp_key, bp.c.branch_id, bp.c.formats)))
    with store.engine.begin() as c:
        for sid, name, old_k, cur, fm in profs:
            k = vsp_key(name)
            if "mobile" in json.loads(fm or "[]"):
                st["mobile_stops"] += 1
                routes.add(k or sid)
                bid = None
            else:
                ids = idx.get(k or "", [])
                bid = ids[0] if len(ids) == 1 else None
                st["linked" if bid else ("ambiguous" if len(ids) > 1 else "unlinked")] += 1
            if bid != cur or k != old_k:
                c.execute(update(bp).where(bp.c.site_id == sid).values(branch_id=bid, vsp_key=k))
    st["mobile_routes"] = len(routes)
    return st


YANDEX_FORMATS = {"office_vip": "sber_first", "office_sme": "business", "office_corporate": "corporate",
                  "office_retail": "mass"}


def apply_formats(store: Store) -> dict:
    """Форматы ВСП из всех источников: {формат: [источники]} в vsp_branch.formats.
    Сайт банка — офисный поиск; Яндекс — признаки карточки; 2ГИС — дочерние карточки;
    реестр ЦБ — передвижные пункты и кассово-инкассаторские центры."""
    from ..storage import branch_profiles as bp
    acc: dict[int, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    with store.engine.connect() as c:
        for bid, fm in c.execute(select(bp.c.branch_id, bp.c.formats).where(
                bp.c.branch_id.isnot(None), bp.c.status == "active")):
            for f in json.loads(fm or "[]"):
                acc[bid][f].add("сайт банка")
        for x in c.execute(select(source_branches).where(source_branches.c.branch_id.isnot(None))).mappings():
            raw = _raw(x)
            if x["source"] == "yandex":
                for t in raw.get("office_type") or []:
                    if t in YANDEX_FORMATS:
                        acc[x["branch_id"]][YANDEX_FORMATS[t]].add("Яндекс")
                if "branch_bank" in (raw.get("division_type") or []):
                    acc[x["branch_id"]]["head"].add("Яндекс")
            if raw.get("format") in CHILD_FORMATS:
                acc[x["branch_id"]][raw["format"]].add(x["source"] == "2gis" and "2ГИС" or x["source"])
        for bid, status in c.execute(select(branches.c.branch_id, branches.c.status).where(
                branches.c.status.in_(("mobile", "cash_center")))):
            acc[bid][status].add("реестр ЦБ")
        cur = dict(c.execute(select(branches.c.branch_id, branches.c.formats)).all())
    st: dict = defaultdict(int)
    with store.engine.begin() as c:
        for bid, fm in acc.items():
            val = json.dumps({f: sorted(src) for f, src in sorted(fm.items())}, ensure_ascii=False)
            for f in fm:
                st[f] += 1
            if cur.get(bid) != val:
                c.execute(update(branches).where(branches.c.branch_id == bid).values(formats=val))
                st["rows_changed"] += 1
    return dict(st)
