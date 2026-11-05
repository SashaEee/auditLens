"""Находки уровня сети: календарь по месяцам, события из данных, ответы банка, нормы признаков."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict

from .campaign import month_end, series_flags, template_share
from .data import Rec


def calendar(recs: list[Rec], source: str = "2gis") -> list[dict]:
    """По месяцам: подтверждённые пятёрки (официальный канал), неподтверждённые пятёрки от
    авторов без других отзывов, прочие неподтверждённые пятёрки, остальные оценки."""
    m: dict[str, Counter] = defaultdict(Counter)
    for r in recs:
        if r.source != source or r.rating is None:
            continue
        k = r.at.strftime("%Y-%m")
        if r.rating == 5 and r.confirmed:
            m[k]["conf5"] += 1
        elif r.rating == 5 and r.provider != "flamp" and r.author_total is not None and r.author_total <= 1:
            m[k]["u5_oneoff"] += 1
        elif r.rating == 5:
            m[k]["u5_other"] += 1
        else:
            m[k]["rest"] += 1
        if r.rating <= 2:
            m[k]["neg"] += 1
    return [{"month": k, **{c: v[c] for c in ("conf5", "u5_oneoff", "u5_other", "rest", "neg")}}
            for k, v in sorted(m.items())]


def events(cal: list[dict]) -> list[dict]:
    """Точки смены по сети, выведенные из календаря (календарь событий банка не дан)."""
    out = []
    seen_conf = False
    for i, row in enumerate(cal):
        prev = cal[max(0, i - 3):i]
        if not seen_conf and row["conf5"] >= 100:
            out.append({"month": row["month"], "kind": "official_channel",
                        "label": "появились подтверждённые операцией отзывы — официальный канал приглашений"})
            seen_conf = True
        if len(prev) == 3:
            base_o = sum(p["u5_oneoff"] for p in prev) / 3
            base_c = sum(p["conf5"] for p in prev) / 3
            if row["u5_oneoff"] >= 500 and row["u5_oneoff"] >= 3 * max(base_o, 1):
                out.append({"month": row["month"], "kind": "oneoff_surge",
                            "label": f"неподтверждённых пятёрок от новых авторов ×{row['u5_oneoff'] / max(base_o, 1):.0f} к трём прошлым месяцам"})
            if seen_conf and row["conf5"] >= 500 and row["conf5"] >= 2 * max(base_c, 1):
                out.append({"month": row["month"], "kind": "official_growth",
                            "label": f"подтверждённых отзывов ×{row['conf5'] / max(base_c, 1):.1f} к трём прошлым месяцам"})
    return out


def replies(recs: list[Rec], source: str = "2gis") -> list[dict]:
    y: dict[int, Counter] = defaultdict(Counter)
    for r in recs:
        if r.source == source:
            y[r.at.year]["n"] += 1
            y[r.at.year]["replied"] += r.replied
            if r.rating is not None and r.rating <= 2:
                y[r.at.year]["neg"] += 1
                y[r.at.year]["neg_replied"] += r.replied
    return [{"year": k, **dict(v)} for k, v in sorted(y.items())]


def norms(recs: list[Rec], exclude: set[str]) -> dict[int, dict]:
    """Нормы признаков по годам: медиана по точкам с 10+ неподтверждёнными пятёрками за год,
    без отзывов из найденных эпизодов. С ними сравниваются признаки эпизода."""
    by_point: dict[str, list[Rec]] = defaultdict(list)
    for r in recs:
        by_point[r.point].append(r)
    acc: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for rs in by_point.values():
        rs.sort(key=lambda r: r.at)
        ser = series_flags(rs)
        years: dict[int, list[Rec]] = defaultdict(list)
        for r in rs:
            if r.rid not in exclude:
                years[r.at.year].append(r)
        for y, yr in years.items():
            fives = [r for r in yr if r.rating == 5]
            u5 = [r for r in fives if not r.confirmed]
            if len(u5) < 10:
                continue
            known = [r for r in u5 if r.author_total is not None and r.provider != "flamp"]
            a = acc[y]
            a["series_share"].append(sum(ser.get(r.rid, False) for r in u5) / len(u5))
            a["month_end_share"].append(sum(month_end(r.local) for r in u5) / len(u5))
            a["confirmed_share"].append(sum(r.confirmed for r in fives) / len(fives))
            if known:
                a["oneoff_share"].append(sum((r.author_total or 0) <= 1 for r in known) / len(known))
            a["template_share"].append(template_share([r.text for r in u5[:80]]))
    return {y: {k: round(statistics.median(v), 3) for k, v in a.items() if v} | {"points": len(a["series_share"])}
            for y, a in acc.items()}
