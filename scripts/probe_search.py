#!/usr/bin/env python
"""Живой замер поиска на запросах, где он раньше промахивался.

Запускается ВНУТРИ контейнера прода: нужен доступ и к базе, и к эмбеддеру.
Печатает выдачу, чтобы релевантность оценивал человек, а не метрика — на этих
объёмах любая автоматическая мера врёт больше, чем показывает.

    docker exec auditlens-app python /tmp/probe_search.py
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/app/src")

# Запрос → чем он был плох раньше. Формулировки описывают поведение поиска,
# а не источник замечания: файл живёт в публичном репозитории.
LOOPHOLE = [
    ("эскроу", "записей по теме нет — это должно быть видно, а не скрыто"),
    ("вклад", "подстрока поднимала ломбарды и карты"),
    ("кредит", "ловило «кредитную организацию» в теле статьи"),
    ("кредитная карта", "выдавало переводы и брокерские комиссии"),
    ("ипотека", "по теме была одна запись из пяти"),
]
MARKET = [
    ("дебетовые карты", "подстрока не находила «Дебетовую карту» вообще"),
    ("автокредит", "проверка, что подстрока не потерялась"),
    ("вклады", "проверка словоформы"),
]


def loopholes() -> None:
    from bank_audit.loophole import repository as repo
    print("=" * 78)
    print("ЛАЗЕЙКИ — гибрид (полнотекст + вектор, RRF)")
    print("=" * 78)
    for q, why in LOOPHOLE:
        t = time.time()
        try:
            rows = repo.search_relevant(q, only_loophole=False, limit=6)
        except Exception as e:                                # noqa: BLE001
            print(f"\n[{q}] ОШИБКА {type(e).__name__}: {str(e)[:110]}")
            continue
        print(f"\n[{q}] {len(rows)} шт за {time.time()-t:.2f}с   — {why}")
        for r in rows:
            print(f"   · {(r.get('title') or '')[:74]:<74} [{r.get('via','?')}]")


def market() -> None:
    from bank_audit.db import session
    from sqlalchemy import text
    print()
    print("=" * 78)
    print("ВИТРИНА «РЫНОК» — подстрока и полнотекст, объединением")
    print("=" * 78)
    sql = text("""
        SELECT m.title, m.bank_name,
               (m.title ILIKE :qq) AS by_sub,
               (to_tsvector(CAST('russian' AS regconfig), coalesce(m.title,''))
                @@ websearch_to_tsquery(CAST('russian' AS regconfig), :q)) AS by_fts
          FROM v_market_rub_offer m
         WHERE m.bank_name ILIKE :qq OR m.title ILIKE :qq
            OR to_tsvector(CAST('russian' AS regconfig), coalesce(m.title,''))
               @@ websearch_to_tsquery(CAST('russian' AS regconfig), :q)
         LIMIT 6
    """)
    with session() as s:
        for q, why in MARKET:
            t = time.time()
            rows = s.execute(sql, {"q": q, "qq": f"%{q}%"}).mappings().all()
            print(f"\n[{q}] {len(rows)} шт за {time.time()-t:.2f}с   — {why}")
            for r in rows:
                how = ("подстрока и полнотекст" if r["by_sub"] and r["by_fts"]
                       else "подстрока" if r["by_sub"] else "полнотекст")
                print(f"   · {(r['bank_name'] or '')[:22]:<22} {(r['title'] or '')[:44]:<44} [{how}]")


if __name__ == "__main__":
    loopholes()
    market()
