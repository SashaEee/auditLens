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
from ..storage import (Store, author_groups, authors, branch_stats, cases, episodes, network, persons, signal_runs)
from . import VERSION
from .authors import analyze as analyze_authors
from .campaign import (episode_features, find_bursts, score, series_flags, strength, week_start)
from .data import Rec, load
from .network import calendar, events, norms, replies
from .people import analyze as analyze_people
from . import risk as rk
from .risks import BY_KEY as RISK, match as risk_match

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


def _llm_cards(store: Store) -> dict[str, dict]:
    """Карточки модели (llm_read, этап 2): review_id → поля. Пусто, если прогона ещё не было."""
    try:
        from .llm_read import PROMPT_VERSION, review_llm
        with store.engine.connect() as c:
            rows = c.execute(select(review_llm.c.review_id, review_llm.c.data_json).where(
                review_llm.c.prompt_version == PROMPT_VERSION, review_llm.c.relevant == 1))
            out = {}
            for rid, dj in rows:
                try:
                    out[rid] = json.loads(dj or "{}")
                except ValueError:
                    pass
            return out
    except Exception:  # noqa: BLE001 — таблицы ещё нет
        return {}


def _pl(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


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
    # случаи: отзывы, где клиент описывает возможное нарушение (signals/risks.py)
    case_rows: list[dict] = []
    risk_by_rid: dict[str, list[str]] = defaultdict(list)
    for r in recs:
        t = r.extra.get("raw_text")
        if not t:
            continue
        for typ, q in risk_match(t):
            risk_by_rid[r.rid].append(typ)
            case_rows.append({"review_id": r.rid, "type": typ, "severity": RISK[typ].severity, "point": r.point,
                              "branch_id": r.branch_id, "gosb": r.gosb, "subject": r.subject, "source": r.source,
                              "rating": r.rating, "at": r.at, "quote": q[:700]})
    ppl = analyze_people(recs, risk_by_rid)
    # истории клиентов: карточки модели (llm_read), пока их нет — слова-правила
    llm = _llm_cards(store)
    spam = {a for a, n in Counter(r.author for r in recs if r.author).items() if n >= 40}
    stories, story_source = rk.stories_from([r for r in recs if r.author not in spam], llm, case_rows)
    st["stories"] = len(stories)
    st["story_source"] = story_source
    st["cases"] = len(case_rows)
    st["cases_by_type"] = dict(Counter(c["type"] for c in case_rows))
    st["persons"] = dict(Counter(x["kind"] for x in ppl))
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
    two_y = now - timedelta(days=730)
    cases_by_point: dict[str, list[dict]] = defaultdict(list)
    for c in case_rows:
        if c["at"] >= two_y:
            cases_by_point[c["point"]].append(c)
    people_by_point: dict[str, list[dict]] = defaultdict(list)
    for x in ppl:
        people_by_point[x["point"]].append(x)
    # соседи по ГОСБ: взвешенные случаи на отзыв за 2 года — «во сколько раз чаще соседей»
    vol24: Counter = Counter(r.point for r in recs if r.at >= two_y)
    w_pt: Counter = Counter()
    for p_, cs in cases_by_point.items():
        w_pt[p_] = sum(c["severity"] for c in {c["review_id"]: c for c in cs}.values())
    grp_of = {r.point: (r.gosb or r.subject or "net") for r in recs}
    g_w: Counter = Counter()
    g_n: Counter = Counter()
    for p_, n in vol24.items():
        g_w[grp_of[p_]] += w_pt[p_]
        g_n[grp_of[p_]] += n
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
        sc = max(ep12, 0.5 * ep24)
        campaign_score = round(min(100.0, sc), 1)
        # балл отбора: возможные нарушения важнее оценок
        cs = cases_by_point.get(p, [])
        per_review: dict[str, int] = {}
        for c in cs:
            per_review[c["review_id"]] = max(per_review.get(c["review_id"], 0), c["severity"])
        sev3 = sum(1 for v in per_review.values() if v == 3)
        sev2 = sum(1 for v in per_review.values() if v == 2)
        sev1 = sum(1 for v in per_review.values() if v == 1)
        g = grp_of.get(p, "net")
        rate = (g_w[g] - w_pt[p]) / max(1, g_n[g] - vol24[p])
        ratio = (w_pt[p] / (rate * vol24[p])) if rate > 0 and vol24[p] else None
        case_pts = min(45, 15 * min(sev3, 3)) + min(20, 5 * min(sev2, 4)) + min(5, 2 * sev1)
        if ratio is not None and ratio < 1 and (sev3 + sev2):
            case_pts *= 0.6
        reasons = []
        types = Counter(c["type"] for c in cs)
        if sev3 or sev2:
            top = ", ".join(RISK[t].label.lower() for t, _ in types.most_common(2))
            n_ = sev3 + sev2
            reasons.append(f"{n_} {_pl(n_, 'жалоба', 'жалобы', 'жалоб')} на возможные нарушения за 2 года: {top}")
            if ratio and ratio >= 2 and n_ >= 2:
                reasons.append(f"таких жалоб в {ratio:.0f} раз{'а' if ratio < 5 else ''} больше, чем у соседей по ГОСБ")
        pers = people_by_point.get(p, [])
        bad_p = [x for x in pers if x["kind"] == "complaints"]
        wave_p = [x for x in pers if x["kind"] == "praise_wave"]
        person_pts = min(30, 20 + 5 * (max(x["authors"] for x in bad_p) - 2)) if bad_p else 0
        for x in bad_p[:2]:
            reasons.append(f"сотрудник «{x['name']}» — в жалобах {x['authors']} разных клиентов")
        praise_pts = 0
        if wave_p:
            x = wave_p[0]
            praise_pts = 10 + (10 if types.get("favoritism") else 0)
            reasons.append(f"новые аккаунты хвалят «{x['name']}»: {x['n']} отзывов за "
                           f"{max(1, (x['last'] - x['first']).days)} дн.")
        if types.get("favoritism"):
            reasons.append("жалобы «обслуживают по знакомству»")
        camp_pts = 8 if ep12 >= FOCUS else 0
        if ep12 >= FOCUS:
            reasons.append("массовая волна пятёрок от новых аккаунтов")
        link_pts = 8 if (praisers >= 2 or ring_pts[p] or text_pts[p]) else 0
        if link_pts:
            reasons.append("одни и те же люди хвалят несколько отделений")
        sc = round(min(100.0, case_pts + person_pts + praise_pts + camp_pts + link_pts), 1)
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
            "reasons": reasons, "campaign_score": campaign_score,
            "cases_24m": dict(types), "cases_ratio": round(ratio, 2) if ratio is not None else None,
            "severe_24m": sev3, "serious_24m": sev2,
            "people": [{"name": x["name"], "kind": x["kind"], "n": x["n"], "authors": x["authors"]} for x in pers[:6]],
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
            var = e * (1 - net_focus)
            z = (c["focus"] - e) / var ** 0.5 if var > 0 else 0.0
            out[k] = {**dict(c), "share": round(c["focus"] / c["points"], 3) if c["points"] else 0.0,
                      "above": z >= 2.58, "few": c["few"] >= 0.5 * c["points"]}
        return out
    # «Где стоит проверить»: признаки отделения, карта, полоса «Требует реакции», схемы
    pts_ref = {r["point"]: {"gosb": r["detail"].get("gosb"), "subject": r["detail"].get("subject")} for r in rows
               if r["detail"].get("status") in (None, "active") and not r["detail"].get("data_error")}
    assessed = rk.assess(pts_ref, stories, recs, ppl, now=now, source=story_source)
    st["risk_listed"] = sum(1 for a in assessed.values() if a["include"])
    sto_by_point: dict[str, list[dict]] = defaultdict(list)
    for x in stories:
        sto_by_point[x["point"]].append(x)
    for r in rows:
        a = assessed.get(r["point"])
        if a:
            r["detail"]["risk"] = a
        ss = sorted(sto_by_point.get(r["point"], []), key=lambda x: (x["at"]), reverse=True)[:40]
        if ss:
            r["detail"]["stories"] = [{"rid": x["rid"], "date": x["at"].strftime("%d.%m.%Y"), "at": x["at"].isoformat(),
                                       "severity": x["severity"], "actions": [rk.ACTIONS.get(a_, rk.ACTIONS["другое"])["label"]
                                                                              for a_ in x["actions"]],
                                       "quote": x["quote"], "episode": x.get("episode"), "employee": x.get("employee"),
                                       "vulnerable": x.get("vulnerable"), "source": x.get("source"),
                                       "fault": x.get("fault"), "money": x.get("money")} for x in ss]
    cal = calendar(recs)
    net = {
        "calendar": cal, "events": events(cal), "replies": replies(recs), "norms": nrm,
        "subjects": summarize(agg), "gosbs": summarize(gagg),
        "tile_grid": {k: list(v) for k, v in tile_grid(centroids(store)).items()},
        "network_focus_share": round(net_focus, 4),
        "risk_map": rk.map_tiles(pts_ref, assessed, recs, now=now),
        "risk_react": rk.react_list(stories, now=now),
        "schemes_fallback": rk.schemes_fallback(stories, pts_ref, now=now),
        "story_source": story_source,
        "summary": st,
    }
    return {"stats": st, "episodes": eps_b + eps_g, "authors": au, "branch_rows": rows, "network": net,
            "cases": case_rows, "people": ppl,
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
            _write(store, cases, [{"run_id": rid, **c} for c in out["cases"]])
            _write(store, persons, [{"run_id": rid, "point": x["point"], "kind": x["kind"], "name": x["name"][:200],
                                     "n": x["n"], "authors": x["authors"], "first_at": x["first"], "last_at": x["last"],
                                     "score": x["score"], "detail_json": _j({k: v for k, v in x.items() if k not in (
                                         "point", "kind", "name", "n", "authors", "first", "last", "score")})}
                                    for x in out["people"]])
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
        for t in (episodes, authors, author_groups, branch_stats, network, cases, persons):
            c.execute(delete(t).where(t.c.run_id.in_(old)))
        c.execute(delete(signal_runs).where(signal_runs.c.run_id.in_(old)))


def latest_run(store: Store) -> int | None:
    with store.engine.connect() as c:
        return c.execute(select(func.max(signal_runs.c.run_id)).where(signal_runs.c.status == "done")).scalar()
