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
  llm-read  [--stage screen|card|detail|both|all] [--limit N] [--cap-rub N]   чтение отзывов моделью
  wait      [--limit N] [--cap-rub N]         жалобы на долгое ожидание — отдельный этап чтения
  motifs    [--cap-rub N] [--kinds 5,15]                     почерки: разложить описания историй по каталогу
  signals                                                    расчёт сигналов вкладки (истории, волны, очаги, полки)
  branch-read [--limit N] [--cap-rub N]                      выводы по отделениям (после signals)
  nightly   [--cap-rub N]                                    вся ночная цепочка: чтение → почерки → расчёт → выводы → расчёт

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


async def cmd_nightly(store: Store, cap_rub: float | None) -> dict:
    """Ночная цепочка одним запуском — то же, что делает демон, но сразу и с общим потолком на шаг."""
    from .mass import _signals
    from .signals import branch_llm, llm_read, motifs
    out: dict = {"read": await llm_read.run(store, limit=20000, stage="all", cap_rub=cap_rub)}
    out["motifs"] = await motifs.build(store, cap_rub=cap_rub)
    out["signals"] = await _signals(store)
    out["branches"] = await branch_llm.run(store, cap_rub=cap_rub)
    if out["branches"].get("saved"):
        out["signals"] = await _signals(store)
    return out


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
    sub.add_parser("signals")
    lr = sub.add_parser("llm-read")
    lr.add_argument("--limit", type=int, default=500)
    lr.add_argument("--stage", choices=["screen", "card", "detail", "both", "all"], default="both")
    lr.add_argument("--cap-rub", type=float, default=None, help="потолок трат на этот запуск, ₽")
    lr.add_argument("--ids", default=None, help="файл со списком review_id (эталон)")
    wt = sub.add_parser("wait")
    wt.add_argument("--limit", type=int, default=2000)
    wt.add_argument("--cap-rub", type=float, default=None, help="потолок трат на этот запуск, ₽")
    mo = sub.add_parser("motifs")
    mo.add_argument("--cap-rub", type=float, default=None, help="потолок трат на этот запуск, ₽")
    mo.add_argument("--kinds", default=None, help="только эти типы историй, через запятую (проба)")
    br = sub.add_parser("branch-read")
    br.add_argument("--limit", type=int, default=2000)
    br.add_argument("--cap-rub", type=float, default=None, help="потолок трат на этот запуск, ₽")
    ni = sub.add_parser("nightly")
    ni.add_argument("--cap-rub", type=float, default=None, help="потолок трат на каждый шаг, ₽")
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
        from .registry.enrich import enrich_branches
        profiles = link_profiles(store)
        out = match_all(store)
        out["profiles"] = profiles
        out["regions"] = enrich_branches(store)
        out["formats"] = apply_formats(store)
    elif a.cmd == "report":
        from .report import completeness_report
        out = completeness_report(store, source=a.source, out=a.out)
    elif a.cmd == "llm-read":
        from .signals.llm_read import run as llm_run
        ids = [x.strip() for x in open(a.ids, encoding="utf-8") if x.strip()] if a.ids else None
        out = asyncio.run(llm_run(store, limit=a.limit, ids=ids, stage=a.stage, cap_rub=a.cap_rub))
    elif a.cmd == "wait":
        from .signals.wait import run as wait_run
        out = asyncio.run(wait_run(store, limit=a.limit, cap_rub=a.cap_rub))
    elif a.cmd == "motifs":
        from .signals.motifs import build as motifs_build
        kinds = [int(x) for x in a.kinds.split(",") if x.strip().isdigit()] if a.kinds else None
        out = asyncio.run(motifs_build(store, cap_rub=a.cap_rub, kinds=kinds))
    elif a.cmd == "branch-read":
        from .signals.branch_llm import run as branch_run
        out = asyncio.run(branch_run(store, limit=a.limit, cap_rub=a.cap_rub))
    elif a.cmd == "nightly":
        out = asyncio.run(cmd_nightly(store, a.cap_rub))
    elif a.cmd == "signals":
        from .mass import _signals
        out = asyncio.run(_signals(store))          # с отметкой signals:at — демон не повторит
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
