"""Отчёт о полноте сбора и выгрузка отзывов.

Полнота — по факту из vsp_sync_state: заявлено источником / получено за последний
полный проход / хранится (не пропавших), статус полноты и ошибки. Дополнительно —
профиль отзывов по годам и доля «одноразовых» авторов среди свежих пятёрок: этим
проверяется наблюдение о всплеске положительных отзывов в 2026 году.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from .storage import Store, reviews, source_branches, sync_state


def completeness_report(store: Store, *, source: str | None = None, out: str | None = None) -> dict:
    with store.engine.connect() as c:
        q = select(sync_state, source_branches.c.name, source_branches.c.address, source_branches.c.city,
                   source_branches.c.region, source_branches.c.branch_id, source_branches.c.availability
                   ).select_from(sync_state.join(
                       source_branches, (source_branches.c.source == sync_state.c.source)
                       & (source_branches.c.source_branch_id == sync_state.c.source_branch_id), isouter=True))
        if source:
            q = q.where(sync_state.c.source == source)
        rows = [dict(r) for r in c.execute(q).mappings()]
        rq = select(reviews.c.source, reviews.c.source_branch_id, reviews.c.rating, reviews.c.review_date,
                    reviews.c.organization_reply, reviews.c.raw_json).where(reviews.c.gone_at.is_(None))
        if source:
            rq = rq.where(reviews.c.source == source)
        revs = list(c.execute(rq))
    by_src: dict[str, dict] = defaultdict(lambda: {"branches": 0, "declared": 0, "fetched": 0, "stored": 0,
                                                    "status": Counter()})
    for r in rows:
        s = by_src[r["source"]]
        s["branches"] += 1
        s["declared"] += r["declared_total"] or 0
        s["fetched"] += r["fetched_total"] or 0
        s["stored"] += r["stored_total"] or 0
        s["status"][r["completeness"] or r["status"] or "never"] += 1
    prof: dict[str, dict] = defaultdict(lambda: {"years": Counter(), "ratings": Counter(), "replies": 0,
                                                  "five_2026": 0, "five_2026_single_author": 0,
                                                  "has_author_count": False})
    for src, _sid, rating, d, reply, raw in revs:
        p = prof[src]
        y = str(d)[:4] if d else "?"
        p["years"][y] += 1
        p["ratings"][int(rating) if rating is not None else "?"] += 1
        p["replies"] += 1 if reply else 0
        if y == "2026" and rating == 5:
            p["five_2026"] += 1
            try:
                arc = json.loads(raw or "{}").get("author_reviews_count")
            except ValueError:
                arc = None
            if arc is not None:
                p["has_author_count"] = True
                p["five_2026_single_author"] += 1 if arc == 1 else 0
    summary = {k: {**v, "status": dict(v["status"]),
                   "share_fetched": round(v["fetched"] / v["declared"], 3) if v["declared"] else None}
               for k, v in by_src.items()}
    profile = {k: {"years": dict(sorted(v["years"].items())), "ratings": dict(v["ratings"]),
                   "replies": v["replies"], "five_2026": v["five_2026"],
                   # у источника нет числа отзывов автора (Яндекс) — None, а не 0
                   "five_2026_single_author": v["five_2026_single_author"] if v["has_author_count"] else None}
               for k, v in prof.items()}
    res = {"by_source": summary, "profile": profile,
           "branches": [{k: (str(v) if k.endswith("_at") or k.endswith("_date") else v) for k, v in r.items()
                         if k in ("source", "source_branch_id", "name", "address", "city", "region",
                                  "branch_id", "declared_total", "fetched_total", "stored_total", "pages",
                                  "completeness", "status", "last_sync_at", "oldest_review_date",
                                  "newest_review_date", "errors", "last_error", "availability")}
                        for r in rows]}
    if out:
        Path(out).write_text(_markdown(res), encoding="utf-8")
    return {"by_source": summary, "profile": profile, "branches": len(rows), "out": out}


def _markdown(res: dict) -> str:
    L = ["# Полнота сбора отзывов ВСП", "", "## По источникам", "",
         "| Источник | Отделений | Заявлено | Получено | Доля | Хранится | Статусы |",
         "|---|---|---|---|---|---|---|"]
    for s, v in res["by_source"].items():
        L.append(f"| {s} | {v['branches']} | {v['declared']} | {v['fetched']} | "
                 f"{v['share_fetched'] if v['share_fetched'] is not None else '—'} | {v['stored']} | "
                 f"{', '.join(f'{k}: {n}' for k, n in v['status'].items())} |")
    L += ["", "## Профиль отзывов", ""]
    for s, p in res["profile"].items():
        L.append(f"**{s}**: по годам {p['years']}; оценки {p['ratings']}; с ответом банка {p['replies']}; "
                 f"пятёрок 2026 года {p['five_2026']}, из них от авторов с единственным отзывом "
                 f"{p['five_2026_single_author'] if p['five_2026_single_author'] is not None else 'нет данных (источник не отдаёт число отзывов автора)'}.")
    L += ["", "## По отделениям", "",
          "| Источник | id | Город | Адрес | Заявлено | Получено | Полнота | Старейший | Свежий | Ошибка |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for b in sorted(res["branches"], key=lambda x: (x["source"], x.get("city") or "", x["source_branch_id"])):
        L.append(f"| {b['source']} | {b['source_branch_id']} | {b.get('city') or ''} | {b.get('address') or ''} | "
                 f"{b.get('declared_total') if b.get('declared_total') is not None else '—'} | "
                 f"{b.get('fetched_total') if b.get('fetched_total') is not None else '—'} | "
                 f"{b.get('completeness') or b.get('status')} | {str(b.get('oldest_review_date') or '')[:10]} | "
                 f"{str(b.get('newest_review_date') or '')[:10]} | {(b.get('last_error') or '')[:60]} |")
    return "\n".join(L) + "\n"


def export_reviews(store: Store, out: str, *, source: str | None = None, city: str | None = None) -> dict:
    """Выгрузка JSONL: отзыв + отделение (без имён авторов — только author_ref)."""
    q = select(reviews.c.review_id, reviews.c.source, reviews.c.source_branch_id, reviews.c.branch_id,
               reviews.c.rating, reviews.c.review_text, reviews.c.review_date, reviews.c.author_ref,
               reviews.c.organization_reply, reviews.c.reply_date, reviews.c.first_seen_at,
               reviews.c.last_seen_at, reviews.c.gone_at, reviews.c.collection_method, reviews.c.source_url,
               source_branches.c.city, source_branches.c.address).select_from(reviews.join(
                   source_branches, (source_branches.c.source == reviews.c.source)
                   & (source_branches.c.source_branch_id == reviews.c.source_branch_id), isouter=True))
    if source:
        q = q.where(reviews.c.source == source)
    if city:
        q = q.where(source_branches.c.city == city)
    n = 0
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with store.engine.connect() as c, open(out, "w", encoding="utf-8") as f:
        for r in c.execute(q).mappings():
            f.write(json.dumps({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()},
                               ensure_ascii=False) + "\n")
            n += 1
    return {"exported": n, "out": out}
