"""Очаги: один способ действия в нескольких отделениях одного города или ГОСБ за короткое время.

В каждом отделении очага — один-два клиента, поэтому по отдельности отделения ничем не
выделяются (аудит 10.11: Казань, платная «биометрия» — 6 отделений за 7 месяцев, в списке
из них было одно). Очаг сравнивается не с пустым местом, а с волной: ожидание — доля территории
в прочитанных отзывах × число случаев этого способа по всей сети за то же окно, отдельно
по площадкам и по жалобам и не-жалобам.

Окно — фиксированной длины (9 или 18 месяцев, либо вся история) и выбирается самым плотным,
поэтому вероятность умножается на число возможных окон; затем — поправка на число сравнений
по всем парам «способ × территория». Сравнение точное: сколько из всех случаев сети за окно
пришлось бы на территорию при ровном распределении (stats.excess).
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .kinds import TYPED_SINCE, kind_name
from .stats import Exposure, bh, bh_levels, city_key, city_name, excess, level_of, story_cell, ym
from .waves import series_keys, typed

MIN_CLIENTS = 3
MIN_POINTS = 2            # очаг — это несколько отделений; одно отделение — полка «один почерк»
MIN_KEY_CLIENTS = 8       # способ, который встречается реже, по территориям не раскладываем
MIN_TERR_READ = 300       # территория с меньшим числом прочитанных отзывов — не сравниваем
WINDOWS_D = (270, 540)
STRONG_Q = 0.05
MAYBE_Q = 0.20
MAIN_MOTIF_SHARE = 0.5    # почерк — «главный» в очаге типа, если его описывает не меньше половины клиентов очага
SINCE = TYPED_SINCE[:7]


def _best(ev: list[dict], days: int) -> tuple[int, int]:
    best, j = (0, 0), 0
    for i in range(len(ev)):
        while ev[i]["at"] - ev[j]["at"] > timedelta(days=days):
            j += 1
        if i - j > best[1] - best[0]:
            best = (j, i)
    return best


def _id(key: str, level: str, terr: str) -> str:
    return hashlib.sha1(f"{key}|{level}|{terr}".encode()).hexdigest()[:10]


def _msc(ev: list[dict]) -> Counter:
    """Истории как счётчик (месяц, площадка, знак) — в том же виде, что и прочитанные отзывы."""
    return Counter((ym(s["at"]), *story_cell(s)) for s in ev)


def _terr_of(s: dict, level: str) -> str | None:
    if level == "gosb":
        return s.get("gosb")
    if level == "subject":
        return s.get("subject")
    return city_key(s.get("subject"), s.get("city"))


def build(stories: list[dict], exposure: Exposure, points: dict[str, dict], *, now: datetime,
          motif_meta: dict | None = None) -> list[dict]:
    motif_meta = motif_meta or {}
    start = datetime.fromisoformat(TYPED_SINCE).replace(tzinfo=now.tzinfo)
    by_key: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in typed(stories):
        if s.get("elsewhere") or s.get("hq"):
            continue
        for k in series_keys(s):
            by_key[k].setdefault(s["client"], s)
    span_d = max(1, (now - start).days)
    read_all: dict[tuple, int] = {}
    net_cache: dict[tuple, Counter] = {}
    cells, tested = [], Counter()
    for key, cl in by_key.items():
        ev_all = list(cl.values())
        if len(ev_all) < MIN_KEY_CLIENTS:
            continue
        net_msc = _msc(ev_all)
        for level in ("gosb", "city"):
            terr: dict[str, list[dict]] = defaultdict(list)
            for s in ev_all:
                t = _terr_of(s, level)
                if t:
                    terr[t].append(s)
            for t, ev in terr.items():
                if (level, t) not in read_all:
                    read_all[(level, t)] = exposure.terr_read(level, t, SINCE)
                if read_all[(level, t)] < MIN_TERR_READ:
                    continue
                tested[level_of(key)] += 1
                if len(ev) < MIN_CLIENTS:
                    continue
                t_msc = _msc(ev)
                best = None
                for days in (*WINDOWS_D, span_d):
                    i, j = _best(ev, days)
                    if j - i + 1 < MIN_CLIENTS:
                        continue
                    b = ym(ev[j]["at"])
                    a = max(SINCE, ym(ev[j]["at"] - timedelta(days=days))) if days != span_d else SINCE
                    b = b if days != span_d else ym(now)
                    inw = [s for s in ev if a <= ym(s["at"]) <= b]
                    pts = {s["point"] for s in inw}
                    if len(inw) < MIN_CLIENTS or len(pts) < MIN_POINTS:
                        continue
                    if (a, b) not in net_cache:
                        net_cache[(a, b)] = exposure.net_cells(a, b)
                    e, p = excess(Exposure._cells(t_msc, a, b), exposure.terr_cells(level, t, a, b),
                                  Exposure._cells(net_msc, a, b), net_cache[(a, b)])
                    # сколько таких окон помещается в историю; окно скользит, поэтому берём с запасом вдвое
                    # (на ровном фоне без запаса «сильные» очаги появлялись чаще заявленных 5 %)
                    windows = max(1.0, 2 * span_d / days) if days != span_d else 1.0
                    p_adj = min(1.0, p * windows * (len(WINDOWS_D) + 1))
                    if best is None or p_adj < best["p"]:
                        best = {"p": p_adj, "clients": len(inw), "points": sorted(pts), "expected": round(e, 2),
                                "ratio": round(len(inw) / e, 1) if e > 0 else None, "a": inw[0]["at"], "b": inw[-1]["at"],
                                "rids": [s["rid"] for s in inw], "read": sum(exposure.terr_cells(level, t, a, b).values()),
                                "all_time": days == span_d, "window_months": round(days / 30.4)}
                if best:
                    cells.append({"key": key, "level": level, "terr": t, "subject": ev[0].get("subject"),
                                  "gosb": ev[0].get("gosb") if level == "gosb" else None, **best})
    pvals = {(c["key"], c["level"], c["terr"]): c["p"] for c in cells}
    strong = bh_levels(pvals, STRONG_Q, tested, key_of=lambda k: k[0])
    maybe = bh_levels(pvals, MAYBE_Q, tested, key_of=lambda k: k[0])
    out = []
    for c in cells:
        ck = (c["key"], c["level"], c["terr"])
        if ck not in maybe:
            continue
        key = c["key"]
        meta = motif_meta.get(key[2:]) if key.startswith("m:") else None
        kind = int(key[2:]) if key.startswith("k:") else (meta or {}).get("kind")
        per_point = Counter()
        rid_set = set(c["rids"])
        for s in by_key[key].values():
            if s["rid"] in rid_set:
                per_point[s["point"]] += 1
        out.append({
            "id": _id(key, c["level"], c["terr"]), "key": key, "level_key": "kind" if key.startswith("k:") else "motif",
            "kind": kind, "motif": key[2:] if key.startswith("m:") else None,
            "name": (meta or {}).get("name") or kind_name(kind),
            "level": c["level"], "terr": city_name(c["terr"]) if c["level"] == "city" else c["terr"],
            "terr_key": c["terr"], "subject": c["subject"], "gosb": c["gosb"],
            "from": c["a"].isoformat(), "to": c["b"].isoformat(), "all_time": c["all_time"],
            "window_months": c["window_months"],
            "clients": c["clients"], "points": [{"point": p, "n": per_point[p]} for p in c["points"]],
            "expected": c["expected"], "ratio": c["ratio"], "p": round(c["p"], 8), "read": c["read"],
            "strength": "strong" if ck in strong else "maybe", "rids": c["rids"],
            "listed": sum(1 for p in c["points"] if ((points.get(p) or {}).get("risk") or {}).get("include")),
        })
    # город и его ГОСБ про одних и тех же клиентов — один очаг: оставляем более узкий (город)
    by_k: dict[str, list[dict]] = defaultdict(list)
    for h in out:
        by_k[h["key"]].append(h)
    keep = []
    for hs in by_k.values():
        cities = [h for h in hs if h["level"] == "city"]
        for h in hs:
            if h["level"] == "gosb" and any(len(set(h["rids"]) & set(c["rids"])) >= 0.8 * len(h["rids"]) for c in cities):
                continue
            keep.append(h)
    # почерк и его тип на одной территории — тоже один очаг: оставляем почерк, если он выделяется не слабее типа;
    # иначе остаётся тип (значимо именно «таких историй здесь больше», а не «больше именно этого приёма»)
    drop: set[str] = set()
    for h in keep:
        if h["level_key"] != "kind":
            continue
        for o in keep:
            if o["level_key"] == "motif" and o["kind"] == h["kind"] and o["level"] == h["level"] \
                    and o["terr_key"] == h["terr_key"] and len(set(h["rids"]) & set(o["rids"])) >= 0.6 * len(h["rids"]):
                drop.add(o["id"] if o["strength"] == "maybe" and h["strength"] == "strong" else h["id"])
    keep = [h for h in keep if h["id"] not in drop]
    # приёмы внутри очага типа: какие почерки описывают его клиенты (до трёх, от двух клиентов) и главный из них,
    # если он у половины и больше. Это пояснение, а не отдельная проверка: значимо «таких историй здесь больше
    # ожидаемого», почерки говорят, о чём они. На данных 11.11 очаги оказались смесью приёмов одного типа
    for h in keep:
        if h["level_key"] != "kind":
            continue
        rs = set(h["rids"])
        cnt = Counter(s["motif"] for s in by_key[h["key"]].values()
                      if s["rid"] in rs and s.get("motif") and not str(s["motif"]).endswith("00"))
        top = [{"id": mid, "name": motif_meta[mid]["name"], "n": n}
               for mid, n in cnt.most_common(3) if n >= 2 and mid in motif_meta]
        if top:
            h["motifs"] = top
            if top[0]["n"] >= MIN_CLIENTS and top[0]["n"] >= MAIN_MOTIF_SHARE * h["clients"]:
                h["main_motif"] = top[0]
    keep.sort(key=lambda h: (h["strength"] != "strong", h["p"], -h["clients"]))
    return keep


def territory_table(stories: list[dict], exposure: Exposure, *, now: datetime, days: int = 730,
                    min_clients: int = MIN_KEY_CLIENTS) -> dict[str, dict]:
    """Для карты по выбранному способу: клиенты за период по регионам и ГОСБ против ожидания
    (ожидание — по остальной сети за тот же период, с учётом состава отзывов)."""
    since = now - timedelta(days=days)
    sm = ym(since)
    by_key: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in typed(stories):
        if s.get("elsewhere") or s.get("hq") or s["at"] < since:
            continue
        for k in [*series_keys(s), "all"]:
            by_key[k].setdefault(s["client"], s)
    net_read = exposure.net_cells(sm)
    terr_read = {tk: exposure.terr_cells(tk[0], tk[1], sm) for tk in exposure.terr if tk[0] in ("subject", "gosb")}
    out = {}
    for key, cl in by_key.items():
        ev = list(cl.values())
        if len(ev) < min_clients:
            continue
        net_ev = Counter(story_cell(s) for s in ev)
        table = {}
        for level in ("subject", "gosb"):
            ev_t: dict[str, Counter] = defaultdict(Counter)
            pts: dict[str, set] = defaultdict(set)
            for s in ev:
                t = _terr_of(s, level)
                if t:
                    ev_t[t][story_cell(s)] += 1
                    pts[t].add(s["point"])
            rows, pv = {}, {}
            for (lvl, t), t_read in terr_read.items():
                if lvl != level:
                    continue
                read = sum(t_read.values())
                k = sum(ev_t[t].values()) if t in ev_t else 0
                e, p = excess(ev_t.get(t, Counter()), t_read, net_ev, net_read)
                few = read < MIN_TERR_READ
                rows[t] = {"n": k, "points": len(pts.get(t, ())), "expected": round(e, 2), "read": read,
                           "ratio": round(k / e, 1) if e > 0 and not few else None, "few": few}
                if k >= MIN_CLIENTS and not few:
                    pv[t] = p
            sig = bh(pv, STRONG_Q, m=len(rows))
            for t in rows:
                rows[t]["sig"] = t in sig
            table[level] = rows
        out[key] = {"clients": len(ev), **table}
    return out
