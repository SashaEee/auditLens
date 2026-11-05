"""Серийные авторы, кольца авторов и одинаковые тексты в разных ВСП.

Автор — хэш псевдонима внутри одного источника (имена не хранятся, между 2ГИС и Яндексом
авторы не связываются, профили авторов не обходятся). Отзывы с Flamp в 2ГИС в признаки
авторов не идут: там другой учёт пользователей.

Шкала автора отделяет естественные типы от подозрительных:
- знаток города — много отзывов на площадке, наши ВСП — малая доля;
- рассыльщик жалобы — единицы по многим ВСП за короткое время (естественный тип: человек
  с одной проблемой пишет везде);
- постоянный клиент — несколько отзывов об одном-двух ВСП;
- хвалебщик сети — только пятёрки, 3+ ВСП за 14 дней (подозрительный);
- остальные — смешанные.
Кольцо — 3+ автора, у каждой пары 3+ общих ВСП с пятёрками в пределах 30 дней.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from hashlib import sha1

from .data import Rec

PRAISER_POINTS = 3
PRAISER_DAYS = 14
RING_COMMON = 3
RING_DAYS = 30
SAME_TEXT_WORDS = 8


def _window_points(rs: list[Rec], days: int) -> int:
    """Наибольшее число разных ВСП в отзывах автора за любое окно в N дней."""
    rs = sorted(rs, key=lambda r: r.at)
    best = 0
    j = 0
    for i in range(len(rs)):
        while rs[i].at - rs[j].at > timedelta(days=days):
            j += 1
        best = max(best, len({r.point for r in rs[j:i + 1]}))
    return best


def author_kind(rs: list[Rec], total: int | None) -> tuple[str, float]:
    pts = {r.point for r in rs}
    ratings = [r.rating for r in rs if r.rating is not None]
    n5 = sum(1 for x in ratings if x == 5)
    n12 = sum(1 for x in ratings if x <= 2)
    share = len(rs) / total if total else None
    if total and total >= 30 and share is not None and share <= 0.2:
        return "знаток города", 0.0
    if len(pts) >= PRAISER_POINTS and ratings and n5 == len(ratings):
        burst = _window_points([r for r in rs if r.rating == 5], PRAISER_DAYS)
        if burst >= PRAISER_POINTS:
            gosbs = {r.gosb for r in rs if r.gosb}
            texts = {r.text for r in rs if r.words >= 4}
            same_text = len(texts) < sum(1 for r in rs if r.words >= 4)
            sc = min(100.0, 40 + 10 * (burst - PRAISER_POINTS) + (15 if len(gosbs) == 1 else 0)
                     + (15 if same_text else 0) + (10 if share is not None and share >= 0.8 else 0))
            return "хвалебщик сети", sc
    if len(pts) >= 3 and ratings and n12 == len(ratings) and _window_points(rs, PRAISER_DAYS) >= 3:
        return "рассыльщик жалобы", 0.0
    if len(pts) <= 2 and len(rs) >= 3:
        return "постоянный клиент", 0.0
    return "смешанный", 0.0


def analyze(recs: list[Rec]) -> dict:
    by: dict[tuple[str, str], list[Rec]] = defaultdict(list)
    for r in recs:
        if r.author and r.provider != "flamp":
            by[(r.source, r.author)].append(r)
    rows = []
    kinds = Counter()
    praisers_by_point: dict[str, list[str]] = defaultdict(list)
    for (src, a), rs in by.items():
        pts = {r.point for r in rs}
        if len(pts) < 2:
            continue
        total = max((r.author_total or 0) for r in rs) or None
        kind, sc = author_kind(rs, total)
        kinds[kind] += 1
        if kind == "хвалебщик сети":
            for p in pts:
                praisers_by_point[p].append(a)
        if len(pts) < 3 and kind != "хвалебщик сети":
            continue
        rows.append({
            "source": src, "author_ref": a, "n": len(rs), "points": len(pts),
            "gosbs": len({r.gosb for r in rs if r.gosb}),
            "n5": sum(1 for r in rs if r.rating == 5), "n12": sum(1 for r in rs if (r.rating or 9) <= 2),
            "first_at": min(r.at for r in rs), "last_at": max(r.at for r in rs), "total_reviews": total,
            "kind": kind, "score": sc,
            "detail": {"reviews": [r.rid for r in sorted(rs, key=lambda r: r.at)][:60],
                       "points": sorted(pts)[:60],
                       "burst_points_14d": _window_points(rs, PRAISER_DAYS)},
        })
    return {"authors": rows, "kinds": dict(kinds), "praisers_by_point": praisers_by_point,
            "rings": rings(by), "same_text": same_text(recs)}


def rings(by: dict[tuple[str, str], list[Rec]]) -> list[dict]:
    """Кольца: пары авторов с 3+ общими ВСП (пятёрки, даты в пределах 30 дней) → компоненты."""
    cand: dict[tuple[str, str], dict[str, list]] = {}
    for key, rs in by.items():
        five = defaultdict(list)
        for r in rs:
            if r.rating == 5:
                five[r.point].append(r)
        if len(five) >= RING_COMMON:
            cand[key] = five
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key, five in cand.items():
        for p in five:
            index[p].append(key)
    pair_pts: dict[tuple, set] = defaultdict(set)
    for p, keys in index.items():
        if len(keys) > 300:            # очень популярная точка: пары через неё не информативны
            continue
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                if a[0] != b[0]:
                    continue
                da = min(r.at for r in cand[a][p])
                db = min(r.at for r in cand[b][p])
                if abs((da - db).days) <= RING_DAYS:
                    pair_pts[(a, b) if a < b else (b, a)].add(p)
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), pts in pair_pts.items():
        if len(pts) >= RING_COMMON:
            parent[find(a)] = find(b)
    comps: dict = defaultdict(set)
    for x in list(parent):
        comps[find(x)].add(x)
    out = []
    for members in comps.values():
        if len(members) < 3:
            continue
        pts = set()
        rids = []
        for m in members:
            for p, rs in cand[m].items():
                pts.add(p)
                rids += [r.rid for r in rs]
        out.append({"kind": "ring", "n_authors": len(members), "n_points": len(pts), "n_reviews": len(rids),
                    "score": min(100.0, 30 + 10 * len(members) + 2 * len(pts)),
                    "detail": {"authors": sorted(m[1] for m in members)[:50], "points": sorted(pts)[:80],
                               "reviews": rids[:200], "source": next(iter(members))[0]}})
    return sorted(out, key=lambda g: -g["score"])


def same_text(recs: list[Rec]) -> list[dict]:
    """Один и тот же текст (8+ слов) в отзывах о разных ВСП — сценарий кампании или рассылка жалобы."""
    groups: dict[str, list[Rec]] = defaultdict(list)
    for r in recs:
        if r.words >= SAME_TEXT_WORDS:
            groups[sha1(r.text.encode()).hexdigest()].append(r)
    out = []
    for rs in groups.values():
        pts = {r.point for r in rs}
        if len(pts) < 2:
            continue
        ratings = Counter(r.rating for r in rs)
        authors = {r.author for r in rs if r.author}
        praise = ratings.get(5, 0) == len(rs)
        out.append({"kind": "same_text", "n_authors": len(authors), "n_points": len(pts), "n_reviews": len(rs),
                    "score": min(100.0, (40 if praise else 10) + 10 * len(pts) + (10 if len(authors) > 1 else 0)),
                    "detail": {"reviews": [r.rid for r in sorted(rs, key=lambda r: r.at)][:100],
                               "points": sorted(pts)[:80], "ratings": dict(ratings),
                               "praise": praise, "first": min(r.at for r in rs).isoformat(),
                               "last": max(r.at for r in rs).isoformat()}})
    return sorted(out, key=lambda g: -g["score"])
