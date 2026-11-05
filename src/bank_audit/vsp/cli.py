"""Командная строка сборщика ВСП: python -m bank_audit.vsp <команда> …

  discover  --source 2gis --city Москва [--city Казань …]   найти отделения в городах
  sync      --source 2gis --id <id>                          одно отделение
  sync      --source 2gis --city Казань | --region … | --all все отделения из реестра
            [--mode full|new]                                full — обновить базу, new — только новые
  match                                                      сопоставить отделения между источниками
  pilot     [--out …] [--from-step all|yandex]               пилот на ~30 отделениях (pilot.py)
  report    [--source …] [--out report.md]                    полнота сбора
  export    --out file.jsonl [--source …] [--city …]           выгрузка отзывов
  registry                                                   обновить реестр ЦБ (закрытые/новые ВСП)
  discover-all --source 2gis|yandex [--force]                обход всей сети (с продолжением)
  banksite  [--force]                                        паспорт ВСП с сайта банка: форматы, часы, услуги
  daemon    --i-know-this-is-mass                            сбор и дописывание по расписанию (mass.py)
  status                                                     сводка: карточки, полнота, расписание, паузы

Массовый прогон по всей сети — только после согласования (см. docs/vsp/RUNBOOK.md):
sync --all и daemon требуют флаг --i-know-this-is-mass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .storage import Store

log = logging.getLogger("bank_audit.vsp")


def collector_for(source: str, settings: Settings):
    if source == "2gis":
        from .collectors.twogis import TwoGisCollector
        return TwoGisCollector(settings)
    if source == "yandex":
        from .collectors.yandex import YandexMapsCollector
        return YandexMapsCollector(settings)
    if source == "domclick":
        from .collectors.domclick import DomclickCollector
        return DomclickCollector(settings)
    raise SystemExit(f"неизвестный источник: {source}")


def setup_logging(settings: Settings, verbose: bool) -> None:
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    # force: пакет bank_audit при импорте уже настраивает корневой журнал — без force
    # basicConfig молча ничего не делает и vsp.log не появляется
    # журнал с ротацией (5 × 10 Мбайт): демон работает неделями, диск сервера не резиновый
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(Path(settings.log_dir) / "vsp.log", maxBytes=10 * 1024 * 1024, backupCount=5,
                             encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, force=True,
                        handlers=[logging.StreamHandler(sys.stderr), fh])
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def cmd_discover(a, settings: Settings, store: Store) -> dict:
    from .collectors.base import Area
    col = collector_for(a.source, settings)
    found = 0
    try:
        for city in a.city:
            n = 0
            async for b in col.discover(Area(city=city, region=a.region)):
                store.upsert_source_branch(b)
                n += 1
            log.info("%s: %s — отделений %d", a.source, city, n)
            found += n
    finally:
        await col.close()
    return {"found": found}


async def cmd_sync(a, settings: Settings, store: Store) -> dict:
    from .sync import sync_many
    if a.all and not a.i_know_this_is_mass:
        raise SystemExit("--all — массовый прогон: нужен флаг --i-know-this-is-mass (и согласование)")
    if a.id:
        ids = a.id
    else:
        ids = store.source_branch_ids(a.source, city=a.city[0] if a.city else None, region=a.region)
        if not a.all and not (a.city or a.region):
            raise SystemExit("укажите --id, --city, --region или --all")
    if a.limit:
        ids = ids[:a.limit]
    col = collector_for(a.source, settings)
    run_id = store.start_run(f"{a.source}:{a.mode}", {"ids": len(ids), "city": a.city, "region": a.region})
    try:
        st = await sync_many(col, store, ids, mode=a.mode, run_id=run_id)
    finally:
        await col.close()
    summary = st.summary()
    store.finish_run(run_id, "blocked" if st.blocked else "done", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bank_audit.vsp", description="Сборщик отзывов о ВСП")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--source", required=True)
    d.add_argument("--city", action="append", required=True)
    d.add_argument("--region")
    s = sub.add_parser("sync")
    s.add_argument("--source", required=True)
    s.add_argument("--id", action="append")
    s.add_argument("--city", action="append")
    s.add_argument("--region")
    s.add_argument("--all", action="store_true")
    s.add_argument("--i-know-this-is-mass", action="store_true")
    s.add_argument("--mode", choices=("full", "new"), default="full")
    s.add_argument("--limit", type=int)
    sub.add_parser("match")
    pl = sub.add_parser("pilot")
    pl.add_argument("--out", default="workspace/vsp/pilot")
    pl.add_argument("--from-step", choices=("all", "yandex"), default="all",
                    help="yandex — продолжить с шага Яндекса (реестр и 2ГИС из прошлого запуска)")
    r = sub.add_parser("report")
    r.add_argument("--source")
    r.add_argument("--out")
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    e.add_argument("--source")
    e.add_argument("--city")
    sub.add_parser("registry")
    da = sub.add_parser("discover-all")
    da.add_argument("--source", required=True, choices=("2gis", "2gis-private", "yandex"))
    da.add_argument("--force", action="store_true")
    bs = sub.add_parser("banksite")
    bs.add_argument("--force", action="store_true")
    dm = sub.add_parser("daemon")
    dm.add_argument("--i-know-this-is-mass", action="store_true")
    sub.add_parser("status")
    a = p.parse_args(argv)

    settings = Settings.from_env()
    setup_logging(settings, a.verbose)
    store = Store(settings.db_url)
    if a.cmd == "discover":
        out = asyncio.run(cmd_discover(a, settings, store))
    elif a.cmd == "sync":
        out = asyncio.run(cmd_sync(a, settings, store))
    elif a.cmd == "pilot":
        from .pilot import run as pilot_run
        out = asyncio.run(pilot_run(store, settings, a.out, from_step=a.from_step))
    elif a.cmd == "match":
        from .registry.apply import apply_formats, link_profiles, match_all
        out = match_all(store)
        out["profiles"] = link_profiles(store)
        out["formats"] = apply_formats(store)
    elif a.cmd == "report":
        from .report import completeness_report
        out = completeness_report(store, source=a.source, out=a.out)
    elif a.cmd == "registry":
        from .mass import refresh_registry
        out = asyncio.run(refresh_registry(store, settings))
    elif a.cmd == "discover-all":
        from . import mass
        fn = {"2gis": mass.discover_2gis_all, "2gis-private": mass.discover_2gis_private,
              "yandex": mass.discover_yandex_points}[a.source]
        out = asyncio.run(fn(store, settings, force=a.force))
    elif a.cmd == "banksite":
        from .mass import refresh_banksite
        out = asyncio.run(refresh_banksite(store, settings, force=a.force))
    elif a.cmd == "daemon":
        if not a.i_know_this_is_mass:
            raise SystemExit("daemon — сбор по всей сети: нужен флаг --i-know-this-is-mass (и согласование)")
        from .mass import daemon
        asyncio.run(daemon(store, settings))
        return 0
    elif a.cmd == "status":
        from .mass import status
        out = status(store)
    else:
        from .report import export_reviews
        out = export_reviews(store, a.out, source=a.source, city=a.city)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
