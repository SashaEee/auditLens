"""Пилот на 20–30 отделениях из разных регионов: все шаги на настоящих данных.

1. реестр ЦБ (≈91 запрос) → справочник отделений;
2. 2ГИС: страницы поиска по пилотным городам → карточки отделений с координатами;
3. отбор пилотных карточек: в каждом городе — самая «отзывная» и средняя/малая;
4. Яндекс: карточки сети рядом с каждой пилотной точкой (1 запрос на точку);
5. сопоставление (registry/apply.match_all);
6. сбор отзывов: 2ГИС и Яндекс по привязанным пилотным отделениям (полный проход);
7. отчёт о полноте и выгрузка.
Объём мал и задан явно: массовый прогон по сети — только по согласованию.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from sqlalchemy import select

from .collectors.base import Area
from .collectors.twogis import TwoGisCollector
from .collectors.yandex import YandexMapsCollector
from .config import Settings
from .http import SourceHttp
from .models import CollectorError, SourceBlocked
from .registry.apply import import_registry, match_all
from .registry.match import Item, match_sources
from .registry.cbr import fetch_registry
from .report import completeness_report, export_reviews
from .storage import Store, source_branches
from .sync import sync_many

log = logging.getLogger(__name__)

# город → сколько карточек 2ГИС взять (большие города — 3, малые — 1–2)
PILOT_CITIES = {
    "Москва": 3, "Санкт-Петербург": 3, "Новосибирск": 2, "Екатеринбург": 2, "Казань": 2,
    "Краснодар": 2, "Владивосток": 2, "Ростов-на-Дону": 2, "Муром": 2, "Кимры": 1,
    "Кашин": 1, "Карпинск": 1, "Торжок": 1, "Весьегонск": 1, "Абакан": 1,
}


def pick(cards: list[dict], n: int) -> list[dict]:
    """В городе: самая отзывная, затем средняя по числу отзывов, затем без отзывов/малая."""
    cards = sorted(cards, key=lambda c: -(c["declared_reviews"] or 0))
    out = cards[:1]
    if n >= 2 and len(cards) > 2:
        out.append(cards[len(cards) // 2])
    if n >= 3 and len(cards) > 3:
        out.append(cards[-1])
    return out[:n]


async def run(store: Store, settings: Settings, out_dir: str, from_step: str = "all") -> dict:
    """from_step="yandex" — продолжить прерванный пилот с шага 4: реестр, поиск и сбор 2ГИС
    берутся из базы и из pilot_selection.json прошлого запуска."""
    t0 = time.time()
    stats: dict = {"timing": {}, "from_step": from_step}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    sel_path = Path(out_dir) / "pilot_selection.json"
    if from_step == "yandex":
        prev = json.loads(sel_path.read_text(encoding="utf-8"))
        stats.update({k: prev[k] for k in ("registry", "cities") if k in prev})
        stats["timing"].update(prev.get("timing", {}))
        pilot_2gis = prev["pilot_2gis"]
    else:
        pilot_2gis = await _registry_and_2gis(store, settings, stats)
        sel_path.write_text(json.dumps({"pilot_2gis": pilot_2gis, "registry": stats["registry"],
                                        "cities": stats["cities"], "timing": stats["timing"]},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
    await _yandex_discover(store, settings, pilot_2gis, stats)
    # 5. сопоставление
    stats["match"] = match_all(store)
    yandex_ids, pairs = _yandex_for_pilot(store, pilot_2gis)
    stats["pilot"] = {"2gis": len(pilot_2gis), "yandex": len(yandex_ids),
                      "registry_linked": pairs["registry_linked"], "2gis_without_yandex": pairs["missing"]}
    # 6. сбор отзывов
    if from_step != "yandex":
        t3 = time.time()
        g = TwoGisCollector(settings)
        try:
            r2 = await sync_many(g, store, pilot_2gis, mode="full", run_id=store.start_run("pilot:2gis", {}))
        finally:
            await g.close()
        stats["timing"]["sync_2gis_s"] = round(time.time() - t3, 1)
        stats.setdefault("sync", {})["2gis"] = r2.summary()
        prev_sel = json.loads(sel_path.read_text(encoding="utf-8"))
        prev_sel["timing"] = stats["timing"]
        prev_sel["sync_2gis"] = stats["sync"]["2gis"]
        sel_path.write_text(json.dumps(prev_sel, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        stats.setdefault("sync", {})["2gis"] = prev.get("sync_2gis")
    t4 = time.time()
    y = YandexMapsCollector(settings)
    try:
        r3 = await sync_many(y, store, yandex_ids, mode="full", run_id=store.start_run("pilot:yandex", {}))
    finally:
        await y.close()
    stats["timing"]["sync_yandex_s"] = round(time.time() - t4, 1)
    stats["sync"]["yandex"] = r3.summary()
    stats["yandex_mode"] = "internal_api" if y.signed else "page_state"
    # 7. отчёт и выгрузка
    stats["report"] = completeness_report(store, out=str(Path(out_dir) / "COMPLETENESS_pilot.md"))
    stats["export"] = export_reviews(store, str(Path(out_dir) / "pilot_reviews.jsonl"))
    stats["timing"]["total_s"] = round(time.time() - t0, 1)
    (Path(out_dir) / "pilot_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1, default=str),
                                                    encoding="utf-8")
    return stats


async def _registry_and_2gis(store: Store, settings: Settings, stats: dict) -> list[str]:
    t0 = time.time()
    # 1. реестр ЦБ
    cbr = SourceHttp("cbr", rps=0.9, timeout_s=60, max_retries=3, user_agent=settings.user_agent)
    try:
        rows = await fetch_registry(cbr)
    finally:
        await cbr.close()
    stats["registry"] = {"rows": len(rows), **import_registry(store, rows)}
    stats["timing"]["registry_s"] = round(time.time() - t0, 1)
    # 2–3. 2ГИС: поиск и отбор
    t1 = time.time()
    g = TwoGisCollector(settings)
    pilot_2gis: list[str] = []
    try:
        for city, n in PILOT_CITIES.items():
            cards = []
            try:
                async for b in g.discover(Area(city=city)):
                    store.upsert_source_branch(b)
                    cards.append({"id": b.source_branch_id, "declared_reviews": b.declared_reviews,
                                  "lat": b.lat, "lon": b.lon})
                    if len(cards) >= 24:          # две страницы поиска — пилоту хватает
                        break
            except CollectorError as e:
                log.warning("2gis %s: %s", city, e)
            chosen = pick(cards, n)
            pilot_2gis += [c["id"] for c in chosen]
            stats.setdefault("cities", {})[city] = {"found": len(cards), "picked": [c["id"] for c in chosen]}
    finally:
        await g.close()
    stats["timing"]["discover_2gis_s"] = round(time.time() - t1, 1)
    return pilot_2gis


async def _yandex_discover(store: Store, settings: Settings, pilot_2gis: list[str], stats: dict) -> None:
    # 4. Яндекс: карточки рядом с пилотными точками (1 запрос на точку)
    t2 = time.time()
    y = YandexMapsCollector(settings, signed=False)
    with store.engine.connect() as c:
        pts = list(c.execute(select(source_branches.c.source_branch_id, source_branches.c.lat,
                                    source_branches.c.lon, source_branches.c.city).where(
            source_branches.c.source == "2gis", source_branches.c.source_branch_id.in_(pilot_2gis))))
    try:
        for sid, lat, lon, city in pts:
            if lat is None:
                continue
            try:
                async for b in y.discover(Area(city=city or "", lat=lat, lon=lon, radius_m=500)):
                    store.upsert_source_branch(b)
            except SourceBlocked as e:
                stats["yandex_blocked"] = e.reason
                break
            except CollectorError as e:
                log.warning("yandex рядом с %s: %s", sid, e)
    finally:
        await y.close()
    stats["timing"]["discover_yandex_s"] = round(time.time() - t2, 1)


def _yandex_for_pilot(store: Store, pilot_2gis: list[str]) -> tuple[list[str], dict]:
    """Карточки Яндекса для пилотных карточек 2ГИС: та же привязка к реестру ЦБ
    или взаимно-лучшая пара в том же здании (match_sources) — если реестр адрес не разобрал.
    Разные отделения по похожему названию не склеиваются: решают адрес и расстояние."""
    with store.engine.connect() as c:
        rows = list(c.execute(select(source_branches).where(
            source_branches.c.source.in_(("2gis", "yandex")))).mappings())
    g = [r for r in rows if r["source"] == "2gis" and r["source_branch_id"] in set(pilot_2gis)]
    ya = [r for r in rows if r["source"] == "yandex"]
    by_bid = {r["branch_id"]: r["source_branch_id"] for r in ya if r["branch_id"]}
    out: dict[str, str] = {}
    linked = 0
    for r in g:
        if r["branch_id"] and r["branch_id"] in by_bid:
            out[r["source_branch_id"]] = by_bid[r["branch_id"]]
            linked += 1
    A = [Item("2gis", r["source_branch_id"], r["lat"], r["lon"], r["address"], r["city"], r["name"])
         for r in g if r["source_branch_id"] not in out]
    B = [Item("yandex", r["source_branch_id"], r["lat"], r["lon"], r["address"], r["city"], r["name"])
         for r in ya if r["source_branch_id"] not in set(out.values())]
    for m in match_sources(A, B):
        if m.status == "auto":
            out[m.a.sid] = m.b.sid
    missing = [sid for sid in pilot_2gis if sid not in out]
    return sorted(set(out.values())), {"registry_linked": linked, "missing": missing, "pairs": out}
