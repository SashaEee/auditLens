"""Волны: как меняется во времени каждый способ действия — по типу истории и по узкому почерку.

Считаем клиентов (один клиент — один раз) и сравниваем периоды с учётом состава отзывов:
сколько из историй пришлось бы на свежий период, если бы частота не изменилась, — отдельно для
каждой площадки и для жалоб и не-жалоб. Так рост не объясняется тем, что отзывов стало больше
или что стало больше положительных. График — клиенты на 10 тыс. прочитанных жалоб по месяцам.

Состояние:
  «новое»       — почти всё, что есть, появилось за последний год;
  «растёт»      — за три месяца значимо чаще, чем за год до них, либо за год значимо чаще, чем за предыдущий;
  «ушло»        — за последний год ни одной истории;
  «затихает»    — за последний год значимо и вдвое реже, чем в лучший год;
  «идёт давно»  — остальное.

Тип истории ставит модель начиная с TYPED_SINCE — раньше этой даты волны не считаются.
Ключ ряда: «k:<номер типа>» или «m:<id почерка>».
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .kinds import BACKGROUND, KINDS, TYPED_SINCE, kind_name
from .stats import Exposure, bh, binom_low, binom_tail, cells_of, expected_std, share_std, ym

RECENT_M = 3              # «последние три месяца» — текущий неполный месяц и два до него
RISE_RATIO = 1.5
RISE_MIN = 4
RISE_Q = 0.05
NEW_SHARE = 0.7
FADE_RATIO = 0.5
MIN_CLIENTS = 5           # ряды короче не показываем: по ним ничего не сказать
MIN_YEAR_COMPLAINTS = 3000
SERIES_MONTHS = 48


def months_back(now: datetime, n: int) -> list[str]:
    """n месяцев до текущего включительно, по возрастанию."""
    out, y, m = [], now.year, now.month
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def series_keys(s: dict) -> list[str]:
    """Ряды, в которые идёт история: её тип и, если есть, почерк."""
    out = []
    if s.get("kind") and s["kind"] not in BACKGROUND and s["kind"] != 25:      # «другое» — не способ действия
        out.append(f"k:{s['kind']}")
    if s.get("motif") and not str(s["motif"]).endswith("00"):
        out.append(f"m:{s['motif']}")
    return out


def typed(stories: list[dict]) -> list[dict]:
    """Тяжёлые истории без копий, с даты, когда тип ставит модель, — основа волн и очагов."""
    return sorted((s for s in stories if s["severity"] >= 2 and not s.get("copy_of") and not s.get("dup_of")
                   and s["at"].strftime("%Y-%m-%d") >= TYPED_SINCE), key=lambda s: s["at"])


def _compare(ev: list[dict], exposure: Exposure, a: tuple[str, str | None], b: tuple[str, str]) -> dict:
    """Период a против периода b: сколько клиентов, сколько ожидалось при прежней частоте, насколько это случайно."""
    ea, eb = cells_of(ev, *a), cells_of(ev, *b)
    ra, rb = exposure.net_cells(*a), exposure.net_cells(*b)
    ka, kb = sum(ea.values()), sum(eb.values())
    exp = expected_std(ra, eb, rb)
    share = share_std(ea + eb, ra, rb)
    return {"n": ka, "base": kb, "expected": round(exp, 1), "ratio": round(ka / exp, 1) if exp > 0 else None,
            "p": binom_tail(ka, ka + kb, share), "p_low": binom_low(ka, ka + kb, share)}


def build(stories: list[dict], exposure: Exposure, *, now: datetime, motif_meta: dict | None = None) -> list[dict]:
    motif_meta = motif_meta or {}
    months = months_back(now, SERIES_MONTHS)
    m24 = months_back(now, 24)
    y1 = (m24[12], None)                       # последние 12 месяцев
    y2 = (m24[0], m24[11])                     # 12 месяцев до них
    m15 = months_back(now, 12 + RECENT_M)
    rec = (m15[12], None)
    base = (m15[0], m15[11])
    complaints = exposure.complaints()
    year_compl: Counter = Counter()
    for m, n in complaints.items():
        year_compl[int(m[:4])] += n
    compl_y1 = sum(n for m, n in complaints.items() if m >= y1[0])
    by: dict[str, dict[str, dict]] = defaultdict(dict)        # ключ → клиент → первая история
    for s in typed(stories):
        for k in series_keys(s):
            by[k].setdefault(s["client"], s)
    rows, pv = [], {}
    for key, cl in by.items():
        ev = list(cl.values())
        if len(ev) < MIN_CLIENTS:
            continue
        cnt = Counter(ym(s["at"]) for s in ev)
        r3 = _compare(ev, exposure, rec, base)
        yy = _compare(ev, exposure, y1, y2)
        # лучший полный год — по частоте на прочитанные жалобы
        by_year = Counter(s["at"].year for s in ev)
        peak_year, peak_rate = None, 0.0
        for y, n in by_year.items():
            if y < now.year and year_compl[y] >= MIN_YEAR_COMPLAINTS and n / year_compl[y] > peak_rate:
                peak_year, peak_rate = y, n / year_compl[y]
        rate1 = yy["n"] / compl_y1 if compl_y1 else 0.0
        p_fade = 1.0
        if peak_year:
            share = compl_y1 / (compl_y1 + year_compl[peak_year]) if compl_y1 else 0.0
            p_fade = binom_low(yy["n"], yy["n"] + by_year[peak_year], share)
        kind = int(key[2:]) if key.startswith("k:") else (motif_meta.get(key[2:]) or {}).get("kind")
        meta = motif_meta.get(key[2:]) if key.startswith("m:") else None
        rows.append({
            "key": key, "level": "kind" if key.startswith("k:") else "motif", "kind": kind,
            "motif": key[2:] if key.startswith("m:") else None,
            "name": (meta or {}).get("name") or kind_name(kind),
            "description": (meta or {}).get("description") or "",
            "clause": (KINDS.get(kind) or {}).get("clause") or "",
            "clients": len(ev), "points": len({s["point"] for s in ev if not s.get("elsewhere")}),
            "gosbs": len({s["gosb"] for s in ev if s.get("gosb")}),
            "subjects": len({s["subject"] for s in ev if s.get("subject")}),
            "vulnerable": sum(1 for s in ev if s.get("vulnerable")),
            "inside": sum(1 for s in ev if s.get("inside")),
            "first": ev[0]["at"].isoformat(), "last": ev[-1]["at"].isoformat(),
            "recent": r3["n"], "recent_expected": r3["expected"], "recent_ratio": r3["ratio"], "base": r3["base"],
            "year": yy["n"], "prev_year": yy["base"], "year_expected": yy["expected"], "year_ratio": yy["ratio"],
            "year_per10k": round(1e4 * rate1, 1), "peak_year": peak_year, "peak_per10k": round(1e4 * peak_rate, 1),
            # ряд для графика: клиенты и прочитанные жалобы по месяцам (частоту по кварталам считает экран)
            "series": [{"m": m, "n": cnt.get(m, 0), "c": complaints.get(m, 0)} for m in months],
            "_p3": r3["p"], "_py": yy["p"], "_pf": p_fade,
        })
        for tag, c in (("3", r3), ("y", yy)):
            if c["n"] >= RISE_MIN and (c["ratio"] is None or c["ratio"] >= RISE_RATIO):
                pv[(key, tag)] = c["p"]
    rising = bh(pv, RISE_Q, m=2 * len(rows))
    for r in rows:
        up3, upy = (r["key"], "3") in rising, (r["key"], "y") in rising
        if r["year"] >= MIN_CLIENTS and r["year"] / r["clients"] >= NEW_SHARE and datetime.fromisoformat(r["first"]) \
                >= now - timedelta(days=730):
            st = "new"
        elif up3 or upy:
            st = "rising"
        elif r["year"] == 0:
            st = "gone"
        elif r["peak_year"] and r["year_per10k"] < FADE_RATIO * r["peak_per10k"] and r["_pf"] < 0.05:
            st = "fading"
        else:
            st = "steady"
        r["status"] = st
        r["horizon"] = "quarter" if up3 else "year" if upy else None       # на каком отрезке виден рост
        r["p"] = round(min(r.pop("_p3"), r.pop("_py")), 6)
        r.pop("_pf")
    order = {"new": 0, "rising": 1, "steady": 2, "fading": 3, "gone": 4}
    rows.sort(key=lambda r: (order[r["status"]], -(r["year_ratio"] or 0), -r["year"], -r["clients"]))
    return rows
