"""Ночной расчёт сигналов: эпизоды кампаний, синхронные всплески ГОСБ, авторы и кольца,
находки сети, витрина ВСП с баллом отбора. Результаты — новым прогоном (vsp_signal_run);
вкладка читает последний завершённый. Хранятся три последних прогона."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import delete, func, insert, select, update

from ..models import utcnow
from ..regions import subject_name, tile_grid
from ..registry.enrich import centroids
from ..storage import (Store, author_groups, authors, branch_stats, episodes, network, signal_runs)
from . import VERSION
from .authors import analyze as analyze_authors
from .campaign import (episode_features, find_bursts, score, series_flags, strength, week_start)
from .data import Rec, load
from .network import calendar, events, norms, replies

log = logging.getLogger(__name__)

KEEP_RUNS = 3
CHUNK = 1000
FOCUS = 40.0           # балл, с которого ВСП «в фокусе»
FEW_12M = 10           # меньше отзывов за год — «мало данных»: 10 пятёрок за две недели тут не набрать
APPLICABLE_12M = 30    # где сигнал кампании применим — по этим точкам проверка «балл ≠ объём»


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


def _write(store: Store, table, rows: list[dict]) -> None:
    for k in range(0, len(rows), CHUNK):
        with store.engine.begin() as c:
            c.execute(insert(table), rows[k:k + CHUNK])


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 10:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return round(cov / (vx * vy), 3) if vx and vy else None


def _episodes(recs: list[Rec], by_point: dict[str, list[Rec]], level: str, key, nrm: dict) -> list[dict]:
    out = []
    series_cache: dict[str, dict] = {}
    for b in find_bursts(recs, key):
        a, z = week_start(b["w0"]), week_start(b["w1"] + 1)
        members = by_point[b["point"]]
        ep = [r for r in members if a <= r.at < z]
        u5 = sorted((r for r in ep if r.u5), key=lambda r: r.at)
        if not u5:
            continue
        start, end = u5[0].at, u5[-1].at
        before = [r for r in members if start - timedelta(days=30) <= r.at < start]
        if level == "branch":
            if b["point"] not in series_cache:
                series_cache[b["point"]] = series_flags(sorted(members, key=lambda r: r.at))
            ser = series_cache[b["point"]]
        else:                         # для ГОСБ — пачки внутри каждого ВСП
            ser = {}
            per = defaultdict(list)
            for r in ep:
                per[r.branch_id].append(r)
            for rs in per.values():
                ser.update(series_flags(sorted(rs, key=lambda r: r.at)))
        f = episode_features(ep, before, ser)
        if level == "gosb":
            f["points"] = len({r.branch_id for r in u5})
            f["points_total"] = len({r.branch_id for r in members})
        sc, why = score(f, b["ratio"], nrm.get(start.year, {}))
        if level == "gosb":
            breadth = f["points"] / max(1, f["points_total"])
            sc = round(min(100.0, sc * (0.5 + breadth)), 1)
        any_r = u5[0]
        out.append({
            "level": level, "point": b["point"],
            "branch_id": any_r.branch_id if level == "branch" else None,
            "gosb": b["point"] if level == "gosb" else any_r.gosb,
            "subject": any_r.subject, "source": any_r.source,
            "start": start, "end": end, "n": b["n"], "expected": b["expected"], "ratio": b["ratio"],
            "p_log10": b["p_log10"], "score": sc, "strength": strength(sc),
            "features_json": _j({**f, "why": why, "norms": nrm.get(start.year, {})}),
            "review_ids_json": _j([r.rid for r in u5][:500]),
        })
    return out


def compute(store: Store, *, now=None) -> dict:
    t0 = time.monotonic()
    now = now or utcnow()
    recs, ref = load(store)
    st: dict = {"reviews": len(recs)}
    if not recs:
        return {"stats": st}
    two = [r for r in recs if r.source == "2gis"]
    by_point: dict[str, list[Rec]] = defaultdict(list)
    for r in two:
        by_point[r.point].append(r)
    # предварительные нормы (по всем отзывам) → эпизоды → нормы без эпизодов → балл пересчитан
    nrm0 = norms(two, set())
    eps_b = _episodes(two, by_point, "branch", lambda r: r.gosb or r.subject or "net", nrm0)
    in_eps = {rid for e in eps_b for rid in json.loads(e["review_ids_json"])}
    nrm = norms(two, in_eps)
    eps_b = _episodes(two, by_point, "branch", lambda r: r.gosb or r.subject or "net", nrm)
    with_gosb = [r for r in two if r.gosb]
    # для ГОСБ «точка» — сам ГОСБ: подменяем point на время поиска
    gosb_recs = [replace(r, point=r.gosb) for r in with_gosb]
    by_gosb: dict[str, list[Rec]] = defaultdict(list)
    for r in gosb_recs:
        by_gosb[r.point].append(r)
    eps_g = _episodes(gosb_recs, by_gosb, "gosb", lambda r: "net", nrm)
    st["episodes_branch"] = len(eps_b)
    st["episodes_gosb"] = len(eps_g)
    st["t_episodes_s"] = round(time.monotonic() - t0, 1)
    au = analyze_authors(recs)
    st["authors"] = au["kinds"]
    st["rings"] = len(au["rings"])
    st["same_text"] = len(au["same_text"])
    # витрина ВСП
    year_ago = now - timedelta(days=365)
    two_years = now - timedelta(days=730)
    eps_by_point: dict[str, list[dict]] = defaultdict(list)
    for e in eps_b:
        eps_by_point[e["point"]].append(e)
    ring_pts: Counter = Counter()
    for g in au["rings"]:
        for p in g["detail"]["points"]:
            ring_pts[p] += 1
    text_pts: Counter = Counter()
    for g in au["same_text"]:
        if g["detail"]["praise"]:
            for p in g["detail"]["points"]:
                text_pts[p] += 1
    all_by_point: dict[str, list[Rec]] = defaultdict(list)
    for r in recs:
        all_by_point[r.point].append(r)
    cards_by_point: dict[str, list] = defaultdict(list)
    for (src, sbid), c in ref["cards"].items():
        cards_by_point[f"b{c.branch_id}" if c.branch_id else f"{src}:{sbid}"].append(c)
    rows = []
    for p, rs in all_by_point.items():
        y = [r for r in rs if r.at >= year_ago]
        ep_rids = {rid for e in eps_by_point.get(p, []) for rid in json.loads(e["review_ids_json"])}
        rated = [r.rating for r in rs if r.rating is not None]
        rated_wo = [r.rating for r in rs if r.rating is not None and r.rid not in ep_rids]
        y_rated = [r.rating for r in y if r.rating is not None]
        eps = sorted(eps_by_point.get(p, []), key=lambda e: e["start"], reverse=True)
        ep12 = max((e["score"] for e in eps if e["end"] >= year_ago), default=0.0)
        ep24 = max((e["score"] for e in eps if two_years <= e["end"] < year_ago), default=0.0)
        praisers = len(au["praisers_by_point"].get(p, []))
        reasons = []
        sc = max(ep12, 0.5 * ep24)
        if ep12 >= FOCUS:
            reasons.append("кампания отзывов за год")
        elif ep24 >= FOCUS:
            reasons.append("кампания отзывов в прошлом году")
        if praisers >= 2:
            sc += min(15, 5 * praisers)
            reasons.append(f"хвалебщики сети: {praisers}")
        if ring_pts[p]:
            sc += 15
            reasons.append("кольцо авторов")
        if text_pts[p]:
            sc += min(10, 5 * text_pts[p])
            reasons.append("один текст в разных ВСП")
        sc = round(min(100.0, sc), 1)
        b = ref["branches"].get(rs[0].branch_id) if rs[0].branch_id else None
        cards = cards_by_point.get(p, [])
        by_src = Counter(r.source for r in rs)
        decl = {c.source: c.declared_rating for c in cards if c.declared_rating}
        detail = {
            "name": (b.name if b is not None else (cards[0].name if cards else None)),
            "city": (b.city if b is not None else (cards[0].city if cards else None)),
            "address": ((b.address if b is not None else None) or (cards[0].address if cards else None)
                        or "")[:300] or None,
            "gosb": b.gosb if b is not None else None, "subject": b.subject if b is not None else None,
            "subject_name": subject_name(b.subject) if b is not None else None,
            "status": b.status if b is not None else None,
            "formats": json.loads(b.formats or "{}") if b is not None and b.formats else {},
            "lat": (b.lat if b is not None else None) or (cards[0].lat if cards else None),
            "lon": (b.lon if b is not None else None) or (cards[0].lon if cards else None),
            "open_date": b.open_date if b is not None else None,
            "sources": dict(by_src), "cards": [{"source": c.source, "id": c.source_branch_id,
                                                "declared_reviews": c.declared_reviews,
                                                "declared_rating": c.declared_rating} for c in cards][:6],
            "declared_rating": decl,
            "n": len(rs), "n_12m": len(y),
            "rating_all": round(sum(rated) / len(rated), 2) if rated else None,
            "rating_wo_episodes": round(sum(rated_wo) / len(rated_wo), 2) if rated_wo else None,
            "rating_12m": round(sum(y_rated) / len(y_rated), 2) if y_rated else None,
            "five_share_12m": round(sum(1 for x in y_rated if x == 5) / len(y_rated), 3) if y_rated else None,
            "confirmed_12m": sum(r.confirmed for r in y),
            "neg_12m": sum(1 for x in y_rated if x <= 2),
            "reply_12m": round(sum(r.replied for r in y) / len(y), 3) if y else None,
            "gone": sum(r.gone for r in rs), "hidden": sum(1 for r in rs if r.hidden),
            "episodes": [{"start": e["start"], "end": e["end"], "n": e["n"], "ratio": e["ratio"],
                          "score": e["score"], "strength": e["strength"]} for e in eps[:10]],
            "praisers": praisers, "rings": ring_pts[p], "same_text": text_pts[p],
            "reasons": reasons,
        }
        rows.append({"point": p, "branch_id": rs[0].branch_id, "score": sc,
                     "data_state": "few" if len(y) < FEW_12M else "ok", "detail": detail})
    # покрытие: балл не должен повторять число отзывов (ρ > 0,5 — сигнал не выпускаем)
    # по всем точкам ρ завышен «немыми» малыми ВСП (меньше 10 отзывов в год — сигналу не на чем
    # сработать); порог 0,5 проверяем там, где сигнал применим. Замер 05.11: все 0,51 · 30+ 0,11
    app = [r for r in rows if r["detail"]["n_12m"] >= APPLICABLE_12M]
    st["rho_score_vs_volume"] = _spearman([r["score"] for r in app], [r["detail"]["n_12m"] for r in app])
    st["rho_all_points"] = _spearman([r["score"] for r in rows], [r["detail"]["n_12m"] for r in rows])
    st["applicable_points"] = len(app)
    st["few_points"] = sum(1 for r in rows if r["data_state"] == "few")
    st["in_focus"] = sum(1 for r in rows if r["score"] >= FOCUS)
    st["points"] = len(rows)
    # карта: субъекты и ГОСБ
    net_focus = st["in_focus"] / max(1, len(rows))
    agg: dict[str, Counter] = defaultdict(Counter)
    gagg: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        d = r["detail"]
        for key, bucket in ((d["subject"], agg), (d["gosb"], gagg)):
            if not key:
                continue
            bucket[key]["points"] += 1
            bucket[key]["focus"] += r["score"] >= FOCUS
            bucket[key]["n_12m"] += d["n_12m"]
            bucket[key]["few"] += r["data_state"] == "few"
    def summarize(bucket):
        out = {}
        for k, c in bucket.items():
            e = c["points"] * net_focus
            z = (c["focus"] - e) / ((e * (1 - net_focus)) ** 0.5) if e > 0 else 0.0
            out[k] = {**dict(c), "share": round(c["focus"] / c["points"], 3) if c["points"] else 0.0,
                      "above": z >= 2.58, "few": c["few"] >= 0.5 * c["points"]}
        return out
    cal = calendar(recs)
    net = {
        "calendar": cal, "events": events(cal), "replies": replies(recs), "norms": nrm,
        "subjects": summarize(agg), "gosbs": summarize(gagg),
        "tile_grid": {k: list(v) for k, v in tile_grid(centroids(store)).items()},
        "network_focus_share": round(net_focus, 4),
        "summary": st,
    }
    return {"stats": st, "episodes": eps_b + eps_g, "authors": au, "branch_rows": rows, "network": net,
            "elapsed_s": round(time.monotonic() - t0, 1)}


def run(store: Store) -> dict:
    with store.engine.begin() as c:
        rid = c.execute(insert(signal_runs).values(version=VERSION, started_at=utcnow(), status="running",
                                                   params_json=_j({}))).inserted_primary_key[0]
    try:
        out = compute(store)
        if "episodes" in out:
            _write(store, episodes, [{**e, "run_id": rid} for e in out["episodes"]])
            au = out["authors"]
            _write(store, authors, [{"run_id": rid, **{k: v for k, v in a.items() if k != "detail"},
                                     "detail_json": _j(a["detail"])} for a in au["authors"]])
            _write(store, author_groups, [{"run_id": rid, **{k: v for k, v in g.items() if k != "detail"},
                                           "detail_json": _j(g["detail"])}
                                          for g in au["rings"] + au["same_text"]])
            _write(store, branch_stats, [{"run_id": rid, "point": r["point"], "branch_id": r["branch_id"],
                                          "score": r["score"], "data_state": r["data_state"],
                                          "detail_json": _j(r["detail"])} for r in out["branch_rows"]])
            _write(store, network, [{"run_id": rid, "key": k, "value_json": _j(v)} for k, v in out["network"].items()])
        stats = {**out["stats"], "elapsed_s": out.get("elapsed_s")}
        with store.engine.begin() as c:
            c.execute(update(signal_runs).where(signal_runs.c.run_id == rid).values(
                status="done", finished_at=utcnow(), stats_json=_j(stats)))
        _prune(store)
        log.info("сигналы: прогон %s — %s", rid, stats)
        return {"run_id": rid, **stats}
    except Exception:
        with store.engine.begin() as c:
            c.execute(update(signal_runs).where(signal_runs.c.run_id == rid).values(status="failed",
                                                                                    finished_at=utcnow()))
        raise


def _prune(store: Store) -> None:
    with store.engine.connect() as c:
        ids = [r[0] for r in c.execute(select(signal_runs.c.run_id).order_by(signal_runs.c.run_id.desc()))]
    old = ids[KEEP_RUNS:]
    if not old:
        return
    with store.engine.begin() as c:
        for t in (episodes, authors, author_groups, branch_stats, network):
            c.execute(delete(t).where(t.c.run_id.in_(old)))
        c.execute(delete(signal_runs).where(signal_runs.c.run_id.in_(old)))


def latest_run(store: Store) -> int | None:
    with store.engine.connect() as c:
        return c.execute(select(func.max(signal_runs.c.run_id)).where(signal_runs.c.status == "done")).scalar()
