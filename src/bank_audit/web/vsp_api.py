"""API вкладки «Аудит сети ВСП»: только чтение из базы сборщика (vsp_reviews).

Доступ — env VSP_USERS (логины через запятую); пусто — только владелец (ADMIN_USERS).
Остальным раздел не показывается, сервер отвечает 403.

База: VSP_DB_URL, а если не задан — адрес основной базы приложения с другой базой
(VSP_DB_NAME, по умолчанию vsp_reviews) и схемой VSP_DB_SCHEMA (vsp). Соединение
открывается только на чтение (default_transaction_read_only): вкладка ничего не пишет
в базу сборщика.

Имена сотрудников в текстах показываются как есть (решение владельца 05.11: ФИО без
даты рождения и других данных — не персональные данные в понимании вкладки). vsp/names.py
нужен только для фильтра «упомянут сотрудник». Имена между разными ВСП не сопоставляются.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import and_, case, create_engine, event, func, or_, select

from ..vsp.regions import BY_CODE, subject_name
from ..vsp.signals.risks import BY_KEY as RISK, TYPES as RISK_TYPES
from ..vsp.storage import (author_groups, authors, branch_stats, branches, cases, episodes, network, persons, reviews,
                           signal_runs, source_branches, sync_state)
from .auth import CurrentUser, get_current_user
from . import telemetry

log = logging.getLogger(__name__)
router = APIRouter()
UTC = timezone.utc


# ── доступ и подключение ───────────────────────────────────────────────────────

def can_vsp(username: str | None) -> bool:
    if not username:
        return False
    allow = {u.strip() for u in os.getenv("VSP_USERS", "").split(",") if u.strip()}
    return username in allow if allow else telemetry.is_admin(username)


def _need(user: CurrentUser) -> None:
    if not can_vsp(user.username):
        raise HTTPException(403, "раздел закрыт")


def vsp_db_url() -> str | None:
    url = os.getenv("VSP_DB_URL")
    if url:
        return url
    base = os.getenv("DATABASE_URL")
    if not base:
        return None
    p = urlsplit(base)
    opts = quote(f"-csearch_path={os.getenv('VSP_DB_SCHEMA', 'vsp')}")
    return urlunsplit((p.scheme, p.netloc, "/" + os.getenv("VSP_DB_NAME", "vsp_reviews"), f"options={opts}", ""))


_ENGINE = None
_LOCK = threading.Lock()


def engine():
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            url = vsp_db_url()
            if not url:
                raise HTTPException(503, "база сборщика ВСП не настроена")
            eng = create_engine(url, pool_pre_ping=True, future=True,
                                **({"pool_size": 3, "max_overflow": 2} if url.startswith("postgresql") else {}))
            if eng.dialect.name == "postgresql":
                @event.listens_for(eng, "connect")
                def _ro(dbapi_conn, _rec):          # вкладка в базу сборщика не пишет
                    # вне транзакции: иначе SET откатится вместе со служебной транзакцией пула
                    ac = getattr(dbapi_conn, "autocommit", None)
                    if ac is not None:
                        dbapi_conn.autocommit = True
                    cur = dbapi_conn.cursor()
                    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                    cur.close()
                    if ac is not None:
                        dbapi_conn.autocommit = ac
            _ENGINE = eng
        return _ENGINE


# ── кэш последнего прогона ─────────────────────────────────────────────────────

_CACHE: dict = {"run": None, "at": 0.0}
CACHE_S = 300


def _run() -> dict:
    """Последний завершённый прогон: витрина точек, эпизоды, сеть — в памяти на 5 минут."""
    now = time.monotonic()
    if _CACHE["run"] and now - _CACHE["at"] < CACHE_S:
        return _CACHE["run"]
    with engine().connect() as c:
        r = c.execute(select(signal_runs).where(signal_runs.c.status == "done")
                      .order_by(signal_runs.c.run_id.desc()).limit(1)).mappings().first()
        if not r:
            raise HTTPException(404, "сигналы ещё не рассчитаны")
        rid = r["run_id"]
        if _CACHE["run"] and _CACHE["run"]["id"] == rid:
            _CACHE["at"] = now
            return _CACHE["run"]
        pts = {}
        for x in c.execute(select(branch_stats).where(branch_stats.c.run_id == rid)).mappings():
            d = json.loads(x["detail_json"] or "{}")
            pts[x["point"]] = {"point": x["point"], "branch_id": x["branch_id"], "score": x["score"],
                               "state": x["data_state"], **d}
        eps = []
        for x in c.execute(select(episodes).where(episodes.c.run_id == rid)).mappings():
            e = dict(x)
            e["features"] = json.loads(e.pop("features_json") or "{}")
            e["review_ids"] = json.loads(e.pop("review_ids_json") or "[]")
            eps.append(e)
        net = {k: json.loads(v) for k, v in c.execute(
            select(network.c.key, network.c.value_json).where(network.c.run_id == rid))}
        groups = []
        for x in c.execute(select(author_groups).where(author_groups.c.run_id == rid)).mappings():
            g = dict(x)
            g["detail"] = json.loads(g.pop("detail_json") or "{}")
            groups.append(g)
        auth = []
        for x in c.execute(select(authors).where(authors.c.run_id == rid,
                                                  authors.c.kind == "хвалебщик сети")).mappings():
            a = dict(x)
            a["detail"] = json.loads(a.pop("detail_json") or "{}")
            auth.append(a)
        try:
            case_rows = [dict(x) for x in c.execute(select(cases).where(cases.c.run_id == rid)).mappings()]
            pers = []
            for x in c.execute(select(persons).where(persons.c.run_id == rid)).mappings():
                d_ = dict(x)
                d_["detail"] = json.loads(d_.pop("detail_json") or "{}")
                pers.append(d_)
        except Exception:  # noqa: BLE001 — прогон до появления таблиц случаев
            case_rows, pers = [], []
    ep_rids = set()
    for e in eps:
        if e["level"] == "branch":
            ep_rids.update(e["review_ids"])
    case_by_rid: dict[str, list[str]] = defaultdict(list)
    for c_ in case_rows:
        case_by_rid[c_["review_id"]].append(RISK[c_["type"]].label if c_["type"] in RISK else c_["type"])
    review_point = {}
    for p_ in pts.values():
        for s_ in p_.get("stories") or []:
            review_point[s_["rid"]] = p_["point"]
    run = {"id": rid, "version": r["version"], "case_by_rid": case_by_rid, "review_point": review_point, "finished_at": r["finished_at"],
           "stats": json.loads(r["stats_json"] or "{}"), "points": pts, "episodes": eps, "network": net,
           "groups": groups, "praisers": auth, "ep_rids": ep_rids, "cases": case_rows, "persons": pers}
    _CACHE.update(run=run, at=now)
    return run


def _iso(d):
    return d.isoformat() if isinstance(d, datetime) else d


def _pt_brief(p: dict | None) -> dict:
    if not p:
        return {}
    return {k: p.get(k) for k in ("point", "name", "city", "address", "gosb", "subject", "subject_name", "score",
                                  "state", "n_12m", "rating_all", "rating_wo_episodes", "declared_rating",
                                  "reasons", "formats", "lat", "lon", "status")}


def _ep_brief(e: dict, pts: dict) -> dict:
    f = e["features"]
    return {"id": e["id"], "level": e["level"], "point": e["point"], "gosb": e["gosb"], "subject": e["subject"],
            "subject_name": subject_name(e["subject"]), "start": _iso(e["start"]), "end": _iso(e["end"]),
            "n": e["n"], "expected": e["expected"], "ratio": e["ratio"], "score": e["score"],
            "strength": e["strength"], "oneoff_share": f.get("oneoff_share"),
            "series_share": f.get("series_share"), "confirmed_share": f.get("confirmed_share"),
            "month_end_share": f.get("month_end_share"), "hidden": f.get("hidden"),
            "points": f.get("points"), "points_total": f.get("points_total"),
            "why": [w["label"] for w in f.get("why", []) if not w.get("against")][:4],
            "place": _pt_brief(pts.get(e["point"])) if e["level"] == "branch" else None}


def _fmt_ok(p: dict, fmt: str | None) -> bool:
    if not fmt:
        return True
    f = p.get("formats") or {}
    if fmt == "premium":
        return "sber_first" in f or "private" in f
    if fmt == "business":
        return any(k in f for k in ("business", "corporate", "legal"))
    return fmt in f


# ── методика: что проверить, чем объясняется, ограничения ─────────────────────

CAMPAIGN_GUIDE = {
    "check": [
        {"where": "на месте", "text": "Просят ли клиентов оставить отзыв в картах и как: планшет или QR у стойки, "
                                      "просьба после операции, «поставьте пять»"},
        {"where": "на месте", "text": "Кто из сотрудников работал в дни эпизода: смены, графики, замены"},
        {"where": "запрос в ГОСБ", "text": "Показатели ВСП и сотрудников, завязанные на рейтинг или число отзывов; "
                                          "конкурсы и поручения по отзывам в период эпизода"},
        {"where": "запрос в ГОСБ", "text": "Жалобы клиентов ВСП за тот же период по внутренним каналам: не "
                                          "совпал ли эпизод с волной обращений"},
        {"where": "запрос в центр", "text": "Число операций ВСП по дням за период эпизода — сопоставить с числом "
                                           "отзывов (отзывов больше, чем клиентов, — признак неподлинности)"},
    ],
    "benign": [
        "Официальная акция приглашения к отзывам — тогда высока доля подтверждённых операцией",
        "Открытие, ремонт или переезд ВСП — новый поток клиентов",
        "Ошибка привязки: отзывы о соседнем офисе в том же здании",
        "Сотрудник с очень лояльными клиентами (обычно без пачек и разовых авторов)",
    ],
    "limits": [
        "Автор виден только внутри площадки: один человек с несколькими аккаунтами не отличим от разных людей",
        "2ГИС — полная история отзывов; Яндекс — только последние ~600 на карточку",
        "Отзывы, удалённые площадкой до начала сбора, не видны",
        "Нормы — медиана по сети за тот же год, без найденных эпизодов",
    ],
}

GOSB_GUIDE = {
    "check": [
        {"where": "запрос в ГОСБ", "text": "Поручения, конкурсы, рассылки ВСП о работе с отзывами в картах в период "
                                          "всплеска; кто инициатор"},
        {"where": "запрос в ГОСБ", "text": "Входит ли рейтинг ВСП в картах в показатели руководителей ВСП и ГОСБ"},
        {"where": "запрос в центр", "text": "Сопоставить период с общесетевыми акциями (календарь сети)"},
    ],
    "benign": ["Общесетевая акция, совпавшая по времени", "Массовое открытие или переоформление карточек в ГОСБ"],
    "limits": CAMPAIGN_GUIDE["limits"],
}

FIVES_EXPLAINER = {
    "title": "Почему пятёрки — тоже сигнал",
    "points": [
        "Пятёрки прячут жалобы: площадка показывает свежие отзывы первыми, и жалоба уходит вниз выдачи",
        "Рейтинг перестаёт измерять качество — а он может входить в показатели ВСП",
        "Жалобу «закрывают» правкой оценки или просьбой написать хороший отзыв",
        "Похвала совпадает с проблемой: волна единиц, через несколько дней — волна пятёрок",
    ],
}


# ── эндпоинты ─────────────────────────────────────────────────────────────────

@router.get("/access")
def access(user: CurrentUser = Depends(get_current_user)):
    return {"allowed": can_vsp(user.username)}


@router.get("/overview")
def overview(user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    pts, net, eps = run["points"], run["network"], run["episodes"]
    now = datetime.now(UTC)
    year_ago = now - timedelta(days=365)
    br12 = [e for e in eps if e["level"] == "branch" and e["end"].replace(tzinfo=e["end"].tzinfo or UTC) >= year_ago]
    g12 = [e for e in eps if e["level"] == "gosb" and e["end"].replace(tzinfo=e["end"].tzinfo or UTC) >= year_ago]
    ranked = sorted((p for p in pts.values() if p["score"] > 0), key=lambda p: -p["score"])
    cal = net.get("calendar") or []
    y = str(now.year)
    cy = [m for m in cal if m["month"].startswith(y)]
    py = [m for m in cal if m["month"].startswith(str(now.year - 1))]

    def tot(rows, *keys):
        return sum(m[k] for m in rows for k in keys)
    fives_cy = tot(cy, "conf5", "u5_oneoff", "u5_other")
    all_cy = fives_cy + tot(cy, "rest")
    fives_py = tot(py, "conf5", "u5_oneoff", "u5_other")
    all_py = fives_py + tot(py, "rest")
    lede = {
        "year": now.year,
        "five_share": round(fives_cy / all_cy, 3) if all_cy else None,
        "five_share_prev": round(fives_py / all_py, 3) if all_py else None,
        "oneoff5": tot(cy, "u5_oneoff"), "oneoff5_prev": tot(py, "u5_oneoff"),
        "conf5": tot(cy, "conf5"), "conf5_prev": tot(py, "conf5"),
        "reviews": all_cy, "reviews_prev": all_py,
    }
    subj = net.get("subjects") or {}
    tiles = []
    for code, (col, row) in (net.get("tile_grid") or {}).items():
        s = subj.get(code, {})
        tiles.append({"code": code, "col": col, "row": row, "label": BY_CODE[code][2] if code in BY_CODE else code,
                      "name": subject_name(code), **{k: s.get(k) for k in ("points", "focus", "share", "above",
                                                                          "few", "n_12m")}})
    feed = sorted((e for e in eps if e["level"] == "branch" and e["strength"] != "слабый"),
                  key=lambda e: e["end"], reverse=True)[:8]
    c12 = [c for c in run["cases"] if _aware(c["at"]) >= year_ago]
    c24 = [c for c in run["cases"] if _aware(c["at"]) >= now - timedelta(days=730)]
    sev_rev = {}
    for c in c12:
        sev_rev[c["review_id"]] = max(sev_rev.get(c["review_id"], 0), c["severity"] or 0)
    top_cases = _case_items(run, [c for c in c24 if (c["severity"] or 0) >= 3])
    top_cases.sort(key=lambda x: x["at"] or "", reverse=True)
    with engine().connect() as cn:
        ids = [x["id"] for x in top_cases[:12]]
        texts = dict(cn.execute(select(reviews.c.review_id, reviews.c.review_text).where(
            reviews.c.review_id.in_(ids or ["-"]))).all())
    for x in top_cases[:12]:
        x["text"] = texts.get(x["id"])
    pers = sorted(run["persons"], key=lambda x: -(x["score"] or 0))
    by_type = Counter(c["type"] for c in {(c["review_id"], c["type"]): c for c in c24}.values())
    return {
        "kpi2": {"severe_12m": sum(1 for v in sev_rev.values() if v >= 3),
                 "serious_12m": sum(1 for v in sev_rev.values() if v == 2),
                 "focus": sum(1 for p in pts.values() if p["score"] >= 40),
                 "people_complaints": sum(1 for x in pers if x["kind"] == "complaints"),
                 "praise_waves": sum(1 for x in pers if x["kind"] == "praise_wave"),
                 "favoritism": len({c["point"] for c in c24 if c["type"] == "favoritism"}),
                 "collusion": len(run["praisers"]) + sum(1 for g in run["groups"] if g["kind"] == "ring")},
        "top_cases": top_cases[:12],
        "case_types": [{"key": t.key, "label": t.label, "hint": t.hint, "severity": t.severity,
                        "n": by_type.get(t.key, 0)} for t in RISK_TYPES],
        "people_top": [{"id": x["id"], "kind": x["kind"], "name": x["name"], "n": x["n"], "authors": x["authors"],
                        "first": _iso(x["first_at"]), "last": _iso(x["last_at"]),
                        "place": _pt_brief(pts.get(x["point"]))} for x in pers[:10]],
        "run": {"id": run["id"], "version": run["version"], "finished_at": _iso(run["finished_at"]),
                "stats": run["stats"]},
        "kpi": {
            "points": len(pts), "in_focus": sum(1 for p in pts.values() if p["score"] >= 40),
            "campaigns_12m": len({e["point"] for e in br12 if e["strength"] != "слабый"}),
            "campaigns_strong_12m": len({e["point"] for e in br12 if e["strength"] == "сильный"}),
            "gosb_12m": len({e["gosb"] for e in g12 if e["strength"] != "слабый"}),
            "praisers": len(run["praisers"]),
            "rings": sum(1 for g in run["groups"] if g["kind"] == "ring"),
            "same_text": sum(1 for g in run["groups"] if g["kind"] == "same_text" and g["detail"].get("praise")),
            "few": sum(1 for p in pts.values() if p["state"] == "few"),
        },
        "lede": lede,
        "calendar": cal[-48:], "events": net.get("events") or [], "replies": net.get("replies") or [],
        "tiles": tiles, "network_focus_share": net.get("network_focus_share"),
        "candidates": [_pt_brief(p) for p in ranked[:25]],
        "feed": [_ep_brief(e, pts) for e in feed],
        "fives": FIVES_EXPLAINER,
        "fives_example": _fives_example(ranked),
    }


def _fives_example(ranked: list[dict]) -> dict | None:
    """Пример для объяснения: ВСП с самым большим разрывом «как видит клиент» и «без эпизодов»."""
    best = None
    for p in ranked[:200]:
        d = p.get("declared_rating") or {}
        shown = d.get("2gis") or p.get("rating_all")
        wo = p.get("rating_wo_episodes")
        if shown and wo and (best is None or shown - wo > best["gap"]):
            best = {"gap": round(shown - wo, 2), "shown": shown, "without": wo, "point": p["point"],
                    "city": p.get("city")}
    return best


@router.get("/signals")
def signals(level: str = "branch", strength: Optional[str] = None, subject: Optional[str] = None,
            gosb: Optional[str] = None, fmt: Optional[str] = None, period: str = "12m", q: Optional[str] = None,
            limit: int = 50, offset: int = 0, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    pts = run["points"]
    since = datetime.now(UTC) - timedelta(days=365 if period == "12m" else 730 if period == "24m" else 99999)
    out = []
    for e in run["episodes"]:
        if e["level"] != level:
            continue
        end = e["end"] if e["end"].tzinfo else e["end"].replace(tzinfo=UTC)
        if end < since or (strength and e["strength"] != strength) or (subject and e["subject"] != subject) \
                or (gosb and e["gosb"] != gosb):
            continue
        p = pts.get(e["point"]) if level == "branch" else None
        if fmt and not (p and _fmt_ok(p, fmt)):
            continue
        if q:
            hay = " ".join(str(x) for x in ((p or {}).get("name"), (p or {}).get("city"), (p or {}).get("address"),
                                            e["gosb"], subject_name(e["subject"])) if x).lower()
            if q.lower() not in hay:
                continue
        out.append(e)
    out.sort(key=lambda e: (-e["score"], e["end"]))
    return {"total": len(out), "items": [_ep_brief(e, pts) for e in out[offset:offset + min(limit, 200)]]}


def _point_filter(point: str):
    if point.startswith("b") and point[1:].isdigit():
        return reviews.c.branch_id == int(point[1:])
    src, _, sbid = point.partition(":")
    return and_(reviews.c.source == src, reviews.c.source_branch_id == sbid)


def _review_row(r, run) -> dict:
    raw = {}
    try:
        raw = json.loads(r["raw_json"] or "{}") or {}
    except ValueError:
        pass
    text = r["review_text"]
    return {"id": r["review_id"], "source": r["source"], "rating": r["rating"], "date": _iso(r["review_date"]),
            "text": text, "reply": bool(r["organization_reply"]),
            "author": (r["author_ref"] or "")[:8] or None, "confirmed": bool(raw.get("confirmed_by")),
            "author_total": raw.get("author_reviews_count"), "hidden": raw.get("hiding_reason") or None,
            "gone": r["gone_at"] is not None, "in_episode": r["review_id"] in run["ep_rids"],
            "case_types": run.get("case_by_rid", {}).get(r["review_id"], []),
            "point": (f"b{r['branch_id']}" if r["branch_id"] else f"{r['source']}:{r['source_branch_id']}"),
            "url": r["source_url"]}


@router.get("/signals/{eid}")
def signal_card(eid: int, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    e = next((x for x in run["episodes"] if x["id"] == eid), None)
    if not e:
        raise HTTPException(404, "сигнал не найден (пересчитан?)")
    pts = run["points"]
    start = e["start"] if e["start"].tzinfo else e["start"].replace(tzinfo=UTC)
    end = e["end"] if e["end"].tzinfo else e["end"].replace(tzinfo=UTC)
    a, z = start - timedelta(days=35), end + timedelta(days=35)
    with engine().connect() as c:
        ids = e["review_ids"][:300]
        rows = list(c.execute(select(reviews).where(reviews.c.review_id.in_(ids))).mappings()) if ids else []
        if e["level"] == "branch":
            daily_rows = c.execute(select(reviews.c.review_date, reviews.c.rating, reviews.c.raw_json).where(
                _point_filter(e["point"]), reviews.c.review_date >= a, reviews.c.review_date <= z,
                reviews.c.source == e["source"]))
        else:
            bids = [r[0] for r in c.execute(select(branches.c.branch_id).where(branches.c.gosb == e["gosb"]))]
            daily_rows = c.execute(select(reviews.c.review_date, reviews.c.rating, reviews.c.raw_json).where(
                reviews.c.branch_id.in_(bids), reviews.c.review_date >= a, reviews.c.review_date <= z,
                reviews.c.source == e["source"])) if bids else []
        days: dict[str, Counter] = defaultdict(Counter)
        for d, rating, raw in daily_rows:
            k = (d if d.tzinfo else d.replace(tzinfo=UTC)).date().isoformat()
            conf = bool((json.loads(raw or "{}") or {}).get("confirmed_by"))
            if rating == 5 and not conf:
                days[k]["u5"] += 1
            elif rating == 5:
                days[k]["conf5"] += 1
            elif rating is not None and rating <= 2:
                days[k]["neg"] += 1
            else:
                days[k]["other"] += 1
    span = max(1, (end.date() - start.date()).days + 1)
    series = []
    d = a.date()
    while d <= z.date():
        k = d.isoformat()
        series.append({"day": k, **{c_: days[k][c_] for c_ in ("u5", "conf5", "neg", "other")}})
        d += timedelta(days=1)
    rows.sort(key=lambda r: r["review_date"])
    guide = CAMPAIGN_GUIDE if e["level"] == "branch" else GOSB_GUIDE
    return {
        **_ep_brief(e, pts), "p_log10": e["p_log10"], "features": e["features"],
        "norm_per_day": round(e["expected"] / span, 3), "series": series,
        "reviews": [_review_row(r, run) for r in rows],
        "guide": guide,
        "place": _pt_brief(pts.get(e["point"])) if e["level"] == "branch" else None,
        "gosb_points": e["features"].get("points"),
    }


@router.get("/branches")
def branch_list(sort: str = "score", subject: Optional[str] = None, gosb: Optional[str] = None,
                fmt: Optional[str] = None, state: Optional[str] = None, q: Optional[str] = None,
                focus: int = 0, limit: int = 50, offset: int = 0, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    pts = list(_run()["points"].values())
    ql = (q or "").lower()
    rows = [p for p in pts if (not subject or p.get("subject") == subject) and (not gosb or p.get("gosb") == gosb)
            and _fmt_ok(p, fmt) and (not state or p["state"] == state) and (not focus or p["score"] >= 40)
            and (not ql or ql in " ".join(str(p.get(k) or "") for k in ("name", "city", "address", "gosb")).lower())]
    key = {"score": lambda p: -p["score"], "n": lambda p: -(p.get("n_12m") or 0),
           "gap": lambda p: -((p.get("rating_all") or 0) - (p.get("rating_wo_episodes") or p.get("rating_all") or 0)),
           "neg": lambda p: -(p.get("neg_12m") or 0)}.get(sort, lambda p: -p["score"])
    rows.sort(key=key)
    return {"total": len(rows), "items": [{**_pt_brief(p), "neg_12m": p.get("neg_12m"),
                                           "five_share_12m": p.get("five_share_12m"),
                                           "episodes": len(p.get("episodes") or []), "praisers": p.get("praisers"),
                                           "rings": p.get("rings"), "same_text": p.get("same_text")}
                                          for p in rows[offset:offset + min(limit, 200)]]}


@router.get("/branches/{point}")
def branch_profile(point: str, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    p = run["points"].get(point)
    if not p:
        raise HTTPException(404, "ВСП не найдено")
    with engine().connect() as c:
        months: dict[str, Counter] = defaultdict(Counter)
        for d, rating, raw, src in c.execute(select(reviews.c.review_date, reviews.c.rating, reviews.c.raw_json,
                                                    reviews.c.source).where(_point_filter(point))):
            k = d.strftime("%Y-%m")
            conf = bool((json.loads(raw or "{}") or {}).get("confirmed_by"))
            key = "u5" if rating == 5 and not conf else "conf5" if rating == 5 else "neg" \
                if rating is not None and rating <= 2 else "other"
            months[k][f"{src}:{key}"] += 1
        recent = list(c.execute(select(reviews).where(_point_filter(point))
                                .order_by(reviews.c.review_date.desc()).limit(30)).mappings())
        cards = []
        if p.get("branch_id"):
            card_where = source_branches.c.branch_id == p["branch_id"]
        else:                                   # точка без привязки к реестру — сама карточка площадки
            src, _, sbid = point.partition(":")
            card_where = and_(source_branches.c.source == src, source_branches.c.source_branch_id == sbid)
        if True:
            for x in c.execute(select(source_branches.c.source, source_branches.c.source_branch_id,
                                      source_branches.c.url, source_branches.c.declared_reviews,
                                      source_branches.c.declared_rating, source_branches.c.match_method,
                                      source_branches.c.raw_json).where(card_where)).mappings():
                raw = json.loads(x["raw_json"] or "{}") or {}
                st = c.execute(select(sync_state.c.completeness, sync_state.c.oldest_review_date,
                                      sync_state.c.last_sync_at).where(
                    sync_state.c.source == x["source"], sync_state.c.source_branch_id == x["source_branch_id"])).first()
                cards.append({"source": x["source"], "id": x["source_branch_id"], "url": x["url"],
                              "declared_reviews": x["declared_reviews"], "declared_rating": x["declared_rating"],
                              "match": x["match_method"], "format": raw.get("format"),
                              "completeness": st[0] if st else None, "oldest": _iso(st[1]) if st else None,
                              "synced": _iso(st[2]) if st else None})
    eps = [_ep_brief(e, run["points"]) for e in sorted(
        (e for e in run["episodes"] if e["level"] == "branch" and e["point"] == point), key=lambda e: e["start"],
        reverse=True)]
    gosb_eps = [_ep_brief(e, run["points"]) for e in run["episodes"] if e["level"] == "gosb"
                and e["gosb"] == p.get("gosb")][:5]
    groups = [{"id": g["id"], "kind": g["kind"], "n_authors": g["n_authors"], "n_points": g["n_points"],
               "n_reviews": g["n_reviews"], "score": g["score"]}
              for g in run["groups"] if point in (g["detail"].get("points") or [])]
    praisers = [{"author": a["author_ref"][:8], "points": a["points"], "n": a["n"], "score": a["score"]}
                for a in run["praisers"] if point in (a["detail"].get("points") or [])]
    return {**p, "timeline": [{"month": k, **v} for k, v in sorted(months.items())],
            "episodes_full": eps, "gosb_episodes": gosb_eps, "groups": groups, "praisers_list": praisers,
            "cards_full": cards, "recent": [_review_row(r, run) for r in recent]}


NAMES_SCAN = 20000          # фильтр «упомянут сотрудник» — среди стольких свежих отзывов выборки

REVIEW_FLAGS = {
    "case": "возможное нарушение", "names": "назван сотрудник", "neg": "оценка 1–2", "gone": "удалён с площадки",
    "hidden": "площадка пометила как подозрительный", "oneoff": "новый аккаунт (других отзывов нет)",
    "episode": "из массовой волны пятёрок", "confirmed": "оставлен после операции", "five": "оценка 5",
    "reply": "банк ответил",
}


def _reviews_query(c, run, point, flag, q, source, since, until, limit, offset):
    cond = [reviews.c.review_date.isnot(None)]
    if point:
        cond.append(_point_filter(point))
    if source:
        cond.append(reviews.c.source == source)
    if since:
        cond.append(reviews.c.review_date >= datetime.fromisoformat(since).replace(tzinfo=UTC))
    if until:
        cond.append(reviews.c.review_date < datetime.fromisoformat(until).replace(tzinfo=UTC) + timedelta(days=1))
    if q:
        cond.append(reviews.c.review_text.ilike(f"%{q}%"))
    if flag == "episode":
        ids = list(run["ep_rids"])[:20000]
        cond.append(reviews.c.review_id.in_(ids or ["-"]))
    elif flag == "case":
        ids = list({c["review_id"] for c in run["cases"]})[:20000]
        cond.append(reviews.c.review_id.in_(ids or ["-"]))
    elif flag == "neg":
        cond.append(reviews.c.rating <= 2)
    elif flag == "five":
        cond.append(reviews.c.rating == 5)
    elif flag == "gone":
        cond.append(reviews.c.gone_at.isnot(None))
    elif flag == "reply":
        cond.append(reviews.c.organization_reply.isnot(None))
    elif flag == "hidden":
        cond.append(reviews.c.raw_json.ilike('%"hiding_reason": "_%'))
    elif flag == "confirmed":
        cond.append(reviews.c.raw_json.ilike('%"confirmed_by": [%'))
    elif flag == "oneoff":
        cond.append(reviews.c.raw_json.ilike('%"author_reviews_count": 1,%'))
    where = and_(*cond)
    if flag == "names":
        # имя в тексте SQL не найдёт: просматриваем свежие отзывы пачками (до NAMES_SCAN)
        from ..vsp.names import has_name
        found, scanned = [], 0
        q_ = select(reviews).where(where, reviews.c.review_text.isnot(None)).order_by(reviews.c.review_date.desc())
        while scanned < NAMES_SCAN:
            chunk = list(c.execute(q_.limit(2000).offset(scanned)).mappings())
            if not chunk:
                break
            scanned += len(chunk)
            found += [r for r in chunk if has_name(r["review_text"])]
        return len(found), found[offset:offset + limit]
    total = c.execute(select(func.count()).select_from(reviews).where(where)).scalar()
    rows = list(c.execute(select(reviews).where(where).order_by(reviews.c.review_date.desc())
                          .limit(limit).offset(offset)).mappings())
    return total, rows


@router.get("/reviews")
def review_list(point: Optional[str] = None, flag: Optional[str] = None, q: Optional[str] = None,
                source: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                limit: int = 50, offset: int = 0, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    with engine().connect() as c:
        total, rows = _reviews_query(c, run, point, flag, q, source, since, until, min(limit, 200), offset)
    pts = run["points"]
    items = []
    for r in rows:
        x = _review_row(r, run)
        pb = pts.get(x["point"]) or {}
        x["place"] = {"name": pb.get("name"), "city": pb.get("city"), "address": pb.get("address")}
        items.append(x)
    return {"total": total, "items": items, "flags": REVIEW_FLAGS}


@router.get("/links")
def links(user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    pts = run["points"]

    def places(lst):
        return [{"point": x, "name": (pts.get(x) or {}).get("name"), "city": (pts.get(x) or {}).get("city"),
                 "address": (pts.get(x) or {}).get("address")} for x in (lst or [])[:12]]
    rings = [{"id": g["id"], "n_authors": g["n_authors"], "n_points": g["n_points"], "n_reviews": g["n_reviews"],
              "score": g["score"], "places": places(g["detail"].get("points"))}
             for g in run["groups"] if g["kind"] == "ring"]
    texts = [{"id": g["id"], "n_authors": g["n_authors"], "n_points": g["n_points"], "n_reviews": g["n_reviews"],
              "score": g["score"], "praise": g["detail"].get("praise"), "ratings": g["detail"].get("ratings"),
              "first": g["detail"].get("first"), "last": g["detail"].get("last"),
              "places": places(g["detail"].get("points"))}
             for g in run["groups"] if g["kind"] == "same_text"]
    pr = sorted(run["praisers"], key=lambda a: -(a["score"] or 0))[:150]
    return {"rings": rings, "same_text": texts[:150],
            "praisers": [{"author": a["author_ref"][:8], "points": a["points"], "gosbs": a["gosbs"], "n": a["n"],
                          "score": a["score"], "first": _iso(a["first_at"]), "last": _iso(a["last_at"]),
                          "total": a["total_reviews"], "places": places(a["detail"].get("points"))} for a in pr],
            "kinds": run["stats"].get("authors")}


@router.get("/groups/{gid}")
def group_card(gid: int, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    g = next((x for x in run["groups"] if x["id"] == gid), None)
    if not g:
        raise HTTPException(404, "группа не найдена")
    with engine().connect() as c:
        ids = (g["detail"].get("reviews") or [])[:200]
        rows = list(c.execute(select(reviews).where(reviews.c.review_id.in_(ids))
                              .order_by(reviews.c.review_date)).mappings()) if ids else []
    pts = run["points"]
    out = []
    for r in rows:
        x = _review_row(r, run)
        x["place"] = {"name": (pts.get(x["point"]) or {}).get("name"), "city": (pts.get(x["point"]) or {}).get("city")}
        out.append(x)
    return {"id": gid, "kind": g["kind"], "n_authors": g["n_authors"], "n_points": g["n_points"],
            "score": g["score"], "reviews": out}


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


def _case_items(run, rows: list[dict]) -> list[dict]:
    """Случаи → по одному на отзыв (у отзыва бывает несколько типов), самые тяжёлые первыми."""
    by: dict[str, dict] = {}
    for c in rows:
        x = by.setdefault(c["review_id"], {"id": c["review_id"], "point": c["point"], "at": _iso(c["at"]),
                                           "rating": c["rating"], "source": c["source"], "types": [], "severity": 0,
                                           "quote": c["quote"]})
        x["types"].append({"key": c["type"], "label": RISK[c["type"]].label if c["type"] in RISK else c["type"]})
        if (c["severity"] or 0) > x["severity"]:
            x["severity"], x["quote"] = c["severity"], c["quote"]
    pts = run["points"]
    out = list(by.values())
    for x in out:
        x["place"] = _pt_brief(pts.get(x["point"]))
    return out


RISK_GUIDE = {
    "check": [
        {"where": "запрос в ГОСБ", "text": "Обращения клиента по внутренним каналам за ту же дату: было ли разбирательство и чем закончилось"},
        {"where": "запрос в центр", "text": "Операции клиента и действия сотрудников в системе за дату визита: кто проводил, что оформлено"},
        {"where": "на месте", "text": "Документы по операции: заявления, согласия, подписи клиента; запись камер, если срок хранения позволяет"},
    ],
    "limits": ["Это слова клиента, а не установленный факт", "Поиск по фразам находит и ложные совпадения — "
               "смотрите цитату и полный текст", "Отзывы, удалённые площадкой до начала сбора, не видны"],
}


@router.get("/cases")
def case_list(type: Optional[str] = None, severity: int = 0, period: str = "24m", subject: Optional[str] = None,
              gosb: Optional[str] = None, point: Optional[str] = None, q: Optional[str] = None, limit: int = 50,
              offset: int = 0, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    since = datetime.now(UTC) - timedelta(days={"12m": 365, "24m": 730, "90d": 90}.get(period, 99999))
    rows = [c for c in run["cases"] if _aware(c["at"]) >= since and (not type or c["type"] == type)
            and (c["severity"] or 0) >= severity and (not subject or c["subject"] == subject)
            and (not gosb or c["gosb"] == gosb) and (not point or c["point"] == point)
            and (not q or q.lower() in (c["quote"] or "").lower())]
    items = _case_items(run, rows)
    items.sort(key=lambda x: (-x["severity"], x["at"] or ""), reverse=False)
    items.sort(key=lambda x: (x["severity"], x["at"] or ""), reverse=True)
    page = items[offset:offset + min(limit, 200)]
    with engine().connect() as c:
        ids = [x["id"] for x in page]
        texts = dict(c.execute(select(reviews.c.review_id, reviews.c.review_text).where(
            reviews.c.review_id.in_(ids or ["-"]))).all())
        urls = dict(c.execute(select(reviews.c.review_id, reviews.c.source_url).where(
            reviews.c.review_id.in_(ids or ["-"]))).all())
    for x in page:
        x["text"] = texts.get(x["id"])
        x["url"] = urls.get(x["id"])
    counts = Counter(c["type"] for c in rows)
    return {"total": len(items), "items": page,
            "types": [{"key": t.key, "label": t.label, "hint": t.hint, "severity": t.severity, "n": counts.get(t.key, 0)}
                      for t in RISK_TYPES], "guide": RISK_GUIDE}


@router.get("/people")
def people_list(kind: Optional[str] = None, subject: Optional[str] = None, limit: int = 100,
                user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    pts = run["points"]
    rows = [x for x in run["persons"] if (not kind or x["kind"] == kind)
            and (not subject or (pts.get(x["point"]) or {}).get("subject") == subject)]
    rows.sort(key=lambda x: -(x["score"] or 0))
    return {"total": len(rows), "items": [{
        "id": x["id"], "kind": x["kind"], "name": x["name"], "n": x["n"], "authors": x["authors"],
        "first": _iso(x["first_at"]), "last": _iso(x["last_at"]), "score": x["score"],
        "types": [{"key": k, "label": RISK[k].label if k in RISK else k, "n": v}
                  for k, v in (x["detail"].get("types") or {}).items()],
        "oneoff": x["detail"].get("oneoff"), "full_name": x["detail"].get("full_name"),
        "place": _pt_brief(pts.get(x["point"]))} for x in rows[:min(limit, 300)]]}


@router.get("/people/{pid}")
def person_card(pid: int, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    x = next((p for p in run["persons"] if p["id"] == pid), None)
    if not x:
        raise HTTPException(404, "не найдено")
    with engine().connect() as c:
        ids = (x["detail"].get("review_ids") or [])[:80]
        rows = list(c.execute(select(reviews).where(reviews.c.review_id.in_(ids or ["-"]))
                              .order_by(reviews.c.review_date)).mappings())
    return {"id": pid, "kind": x["kind"], "name": x["name"], "n": x["n"], "authors": x["authors"],
            "place": _pt_brief(run["points"].get(x["point"])), "reviews": [_review_row(r, run) for r in rows]}


# ── «Где стоит проверить»: главный экран, досье, схемы, поиск, отметки ─────────

def _place(p: dict | None) -> dict:
    p = p or {}
    return {k: p.get(k) for k in ("point", "name", "city", "address", "gosb", "subject", "subject_name", "formats",
                                  "n_12m", "status")}


# ── истории клиентов: общий источник для проводника, таблицы, людей и динамики ──

PERIODS = {"90": 90, "180": 180, "365": 365, "730": 730}
SEVERITY = {3: "очень тяжёлая", 2: "тяжёлая", 1: "лёгкая"}


def _aware(d):
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    if isinstance(d, datetime) and d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d


def _stories(run) -> list[dict]:
    """Все истории прогона, свежие первыми. Ключ сети `stories`; у прогона до него — из досье точек."""
    from ..vsp.signals.risk import ACTIONS
    if "_stories" in run:
        return run["_stories"]
    raw = run["network"].get("stories")
    if raw is None:
        rev = {v["label"]: k for k, v in ACTIONS.items()}
        out = [{**x, "point": p_["point"], "gosb": p_.get("gosb"), "subject": p_.get("subject"),
                "actions": [rev.get(a, "другое") for a in x.get("actions") or []]}
               for p_ in run["points"].values() for x in p_.get("stories") or []]
    else:
        out = [dict(x) for x in raw]
    for x in out:
        x["_at"] = _aware(x.get("at")) or datetime(2000, 1, 1, tzinfo=UTC)
        x["actions"] = [a for a in x.get("actions") or [] if a in ACTIONS] or ["другое"]
        x["labels"] = [ACTIONS[a]["label"] for a in x["actions"]]
        x.setdefault("quote_ok", True)
        x["_named"] = _named(x)                    # один раз на прогон: регулярка дорогая
    out.sort(key=lambda x: x["_at"], reverse=True)
    run["_stories"] = out
    return out


def _named(x: dict) -> bool:
    """Назван ли сотрудник. У карточек модели — только её поле (оно проверено по тексту отзыва);
    у историй «по словам» — имя или ФИО рядом с должностью (без «Сотрудник Сбера», «Слава богу»)."""
    if (x.get("employee") or "").strip():
        return True
    if x.get("source") == "модель":
        return False
    from ..vsp.signals.people import people_in
    return bool(people_in(x.get("quote") or ""))


def _sf(run, *, subject=None, gosb=None, point=None, action=None, severity: int = 0, period=None,
        vulnerable=False, named=False, money=False, q=None, fmt=None) -> list[dict]:
    """Истории по фильтрам. period — ключ PERIODS (дни) или пусто (всё время)."""
    cut = datetime.now(UTC) - timedelta(days=PERIODS[period]) if period in PERIODS else None
    ql = (q or "").strip().lower()
    pts = run["points"]
    out = []
    for x in _stories(run):
        if cut and x["_at"] < cut:
            continue
        if (subject and x.get("subject") != subject) or (gosb and x.get("gosb") != gosb) or \
                (point and x["point"] != point) or (action and action not in x["actions"]):
            continue
        if (severity and (x.get("severity") or 0) < severity) or (vulnerable and not x.get("vulnerable")) or \
                (money and not x.get("money")):
            continue
        if fmt and not _fmt_ok(pts.get(x["point"]) or {}, fmt):
            continue
        if named and not x["_named"]:
            continue
        if ql and ql not in " ".join(str(x.get(k) or "") for k in ("quote", "episode", "employee", "scheme")).lower():
            continue
        out.append(x)
    return out


def _safe_url(u):
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else None


def _attach_src(items: list[dict], key: str = "rid") -> list[dict]:
    """Площадка и ссылка на страницу отзывов отделения у каждого элемента (по id отзыва)."""
    rids = list({x[key] for x in items if x.get(key)})
    if not rids:
        return items
    found = {}
    with engine().connect() as c:
        for i in range(0, len(rids), 500):
            for rid, src, url in c.execute(select(reviews.c.review_id, reviews.c.source, reviews.c.source_url).where(
                    reviews.c.review_id.in_(rids[i:i + 500]))):
                found[rid] = (src, _safe_url(url))
    for x in items:
        if x.get(key) in found:
            x["src"], x["url"] = found[x[key]]
    return items


def _story_out(x: dict, pts: dict) -> dict:
    o = {k: v for k, v in x.items() if not k.startswith("_") and k not in ("author",)}
    o["place"] = _place(pts.get(x["point"]))
    o["severity_label"] = SEVERITY.get(x.get("severity") or 0, "")
    return o


def _months(xs: list[dict], n: int = 24, key=None) -> list[dict]:
    now = datetime.now(UTC)
    allc: Counter = Counter()
    heavy: Counter = Counter()
    for x in xs:
        k = x["_at"].strftime("%Y-%m")
        allc[k] += 1
        heavy[k] += (x.get("severity") or 0) >= 2
    out = []
    for i in range(n - 1, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        k = f"{y}-{m:02d}"
        out.append({"month": k, "n": allc.get(k, 0), "heavy": heavy.get(k, 0)})
    return out


def _action_facets(xs: list[dict]) -> list[dict]:
    from ..vsp.signals.risk import ACTIONS
    c: Counter = Counter()
    pts: dict[str, set] = defaultdict(set)
    for x in xs:
        for a in x["actions"]:
            c[a] += 1
            pts[a].add(x["point"])
    return [{"key": k, "label": ACTIONS[k]["label"], "short": ACTIONS[k]["short"], "n": n, "points": len(pts[k])}
            for k, n in c.most_common()]


def _story_kw(subject, gosb, point, action, severity, period, vulnerable, named, money, q, fmt) -> dict:
    return dict(subject=subject or None, gosb=gosb or None, point=point or None, action=action or None,
                severity=int(severity or 0), period=period or None, vulnerable=bool(vulnerable), named=bool(named),
                money=bool(money), q=q or None, fmt=fmt or None)


def _listed(run, subject=None, gosb=None, only: set | None = None) -> list[dict]:
    from ..vsp.signals.risk import order_key
    rows = []
    for p in run["points"].values():
        a = p.get("risk")
        if not a or not a.get("include"):
            continue
        if (subject and p.get("subject") != subject) or (gosb and p.get("gosb") != gosb) or \
                (only is not None and p["point"] not in only):
            continue
        rows.append(((TIER_RANK.get(a.get("tier"), 2),) + tuple(order_key(a)), p))
    rows.sort(key=lambda t: t[0])
    return [{**_place(p), "why": p["risk"]["why"], "quote": p["risk"]["quote"], "confidence": p["risk"]["confidence"],
             "benign": p["risk"]["benign"], "red": p["risk"]["red"], "severity": p["risk"]["severity"],
             "signals": len(p["risk"]["signals"]), "action": p["risk"]["action_label"],
             "heavy_clients": p["risk"].get("heavy_clients"), "vol_2y": p["risk"].get("vol_2y"),
             "people": [{**pp, "name": _nom(pp.get("name"))} for pp in p["risk"].get("people") or []],
             "tier": p["risk"].get("tier") or "watch",
             "lead": p["risk"].get("lead") or {}, "flags": p["risk"].get("flags") or {},
             "acts": p["risk"].get("acts") or [], "ratio": p["risk"].get("ratio")} for _, p in rows]


TIER_RANK = {"urgent": 0, "converged": 1, "watch": 2}


def _gosb_table(run, subject=None) -> list[dict]:
    """ГОСБ региона (или сети): сколько отделений в списке, тяжёлых историй за год и год назад, на 100 отзывов."""
    now = datetime.now(UTC)
    y1, y2 = now - timedelta(days=365), now - timedelta(days=730)
    agg: dict[str, Counter] = defaultdict(Counter)
    acts: dict[str, Counter] = defaultdict(Counter)
    for p in run["points"].values():
        g = p.get("gosb")
        if not g or (subject and p.get("subject") != subject):
            continue
        agg[g]["points"] += 1
        agg[g]["with_data"] += (p.get("n_12m") or 0) > 0
        agg[g]["n_12m"] += p.get("n_12m") or 0
        agg[g]["listed"] += bool((p.get("risk") or {}).get("include"))
        agg[g]["subject:" + (p.get("subject") or "")] += 1
    for x in _stories(run):
        g = x.get("gosb")
        if g not in agg or (x.get("severity") or 0) < 2 or x["_at"] < y2:
            continue
        if x["_at"] >= y1:
            agg[g]["heavy"] += 1
            for a in x["actions"]:
                acts[g][a] += 1
        else:
            agg[g]["heavy_prev"] += 1
    from ..vsp.signals.risk import ACTIONS
    out = []
    for g, c in agg.items():
        subj = max((k for k in c if k.startswith("subject:")), key=lambda k: c[k])[8:]
        top = acts[g].most_common(1)
        out.append({"gosb": g, "subject": subj, "subject_name": subject_name(subj) if subj else None,
                    "points": c["points"], "with_data": c["with_data"], "listed": c["listed"],
                    "heavy": c["heavy"], "heavy_prev": c["heavy_prev"], "n_12m": c["n_12m"],
                    "per100": round(100 * c["heavy"] / c["n_12m"], 2) if c["n_12m"] >= 50 else None,
                    "top_action": ACTIONS[top[0][0]]["short"] if top else None})
    out.sort(key=lambda r: (-r["listed"], -r["heavy"]))
    return out


def _tiles(run) -> list[dict]:
    net = run["network"]
    rmap = net.get("risk_map") or {"subjects": {}, "network_share": 0}
    tiles = []
    for code, (col, row) in (net.get("tile_grid") or {}).items():
        s_ = rmap["subjects"].get(code, {})
        tiles.append({"code": code, "col": col, "row": row, "label": BY_CODE[code][2] if code in BY_CODE else code,
                      "name": subject_name(code), "n": s_.get("n", 0), "hit": s_.get("hit", 0),
                      "listed": s_.get("listed", 0), "share": s_.get("share"), "above": s_.get("above", False),
                      "few": s_.get("few", True)})
    return tiles


_PROG: dict = {"at": 0.0, "v": None}


def _progress() -> dict:
    """Ход сбора и чтения моделью — для полосы «Идёт сбор и чтение» (кэш 2 минуты)."""
    now = time.monotonic()
    if _PROG["v"] is not None and now - _PROG["at"] < 120:
        return _PROG["v"]
    from ..vsp.storage import kv
    v: dict = {}
    with engine().connect() as c:
        v["reviews"] = {s: n for s, n in c.execute(select(reviews.c.source, func.count()).group_by(reviews.c.source))}
        v["cards"] = {s: n for s, n in c.execute(select(source_branches.c.source, func.count())
                                                  .group_by(source_branches.c.source))}
        # сколько отзывов площадка заявляет на найденных карточках и у скольких они уже скачаны
        v["declared"] = {s: int(n or 0) for s, n in c.execute(select(
            source_branches.c.source, func.sum(case((source_branches.c.declared_reviews > 600, 600),
                                                    else_=func.coalesce(source_branches.c.declared_reviews, 0))))
            .group_by(source_branches.c.source))}
        v["synced"] = {s: n for s, n in c.execute(select(sync_state.c.source, func.count()).where(
            sync_state.c.status == "done").group_by(sync_state.c.source))}
        # знаменатель — как отбор модели: текст от 60 знаков, без массовых аккаунтов-рассыльщиков
        from ..vsp.signals.llm_read import MIN_CHARS, MIN_CHARS_NEG, SPAM_AUTHOR_MIN
        spam = select(reviews.c.author_ref).where(reviews.c.author_ref.isnot(None), reviews.c.review_text.isnot(None)) \
            .group_by(reviews.c.author_ref).having(func.count() >= SPAM_AUTHOR_MIN)
        v["with_text"] = c.execute(select(func.count()).select_from(reviews).where(
            or_(func.length(func.trim(reviews.c.review_text)) >= MIN_CHARS,
                and_(reviews.c.rating <= 3, func.length(func.trim(reviews.c.review_text)) >= MIN_CHARS_NEG)),
            or_(reviews.c.author_ref.is_(None), reviews.c.author_ref.notin_(spam)))).scalar() or 0
        try:
            from ..vsp.signals.llm_read import PROMPT_VERSION, SCREEN_VERSION, review_llm
            byv = {s: n for s, n in c.execute(select(review_llm.c.prompt_version, func.count())
                                              .group_by(review_llm.c.prompt_version))}
            v["screened"] = byv.get(SCREEN_VERSION, 0)
            v["carded"] = byv.get(PROMPT_VERSION, 0)
            v["relevant"] = c.execute(select(func.count()).select_from(review_llm).where(
                review_llm.c.prompt_version == SCREEN_VERSION, review_llm.c.relevant == 1)).scalar() or 0
        except Exception:  # noqa: BLE001 — модель ещё не читала
            c.rollback()
            v["screened"] = v["carded"] = v["relevant"] = 0
        try:
            raw = c.execute(select(kv.c.value).where(kv.c.key == "discover:yandex")).scalar()
            y = json.loads(raw or "{}") or {}
            v["yandex_search"] = {"done": len(y.get("done") or []), "points": y.get("points"),
                                  "finished": bool(y.get("finished"))}
        except Exception:  # noqa: BLE001
            c.rollback()
            v["yandex_search"] = None
    _PROG.update(at=now, v=v)
    return v


@router.get("/meta")
def meta_(user: CurrentUser = Depends(get_current_user)):
    """Лёгкое: плитки карты (регионы для фильтра), дата расчёта и ход сбора/чтения."""
    _need(user)
    run = _run()
    try:
        prog = _progress()
    except Exception:  # noqa: BLE001 — полоса прогресса не должна ронять вкладку
        log.exception("vsp: прогресс не посчитан")
        prog = None
    return {"run": {"id": run["id"], "finished_at": _iso(run["finished_at"])}, "tiles": _tiles(run),
            "source": run["network"].get("story_source"), "progress": prog}


@router.get("/home")
def home(subject: Optional[str] = None, gosb: Optional[str] = None, action: Optional[str] = None,
         vulnerable: bool = False, named: bool = False, fresh: bool = False, fmt: Optional[str] = None,
         user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    net = run["network"]
    rmap = net.get("risk_map") or {"subjects": {}, "network_share": 0}
    tiles = _tiles(run)
    filtered = bool(action or vulnerable or named or fresh or fmt)
    only = None
    if filtered:
        # тот же набор, что считают плашки сводки: тяжёлые истории за год (или за 3 месяца)
        only = {x["point"] for x in _sf(run, subject=subject, gosb=gosb, action=action, vulnerable=vulnerable,
                                         named=named, fmt=fmt, severity=2, period="90" if fresh else "365")}
    full = _listed(run)
    lst = _listed(run, subject, gosb, only)
    scope = _listed(run, subject)
    gosbs = Counter(x["gosb"] for x in scope if x["gosb"])
    react = []
    for x in net.get("risk_react") or []:
        pl_ = run["points"].get(x["point"]) or {}
        if (not subject or pl_.get("subject") == subject) and (not gosb or pl_.get("gosb") == gosb):
            react.append({**x, "place": _place(pl_)})
    pts = run["points"].values()
    with_data = sum(1 for p in pts if (p.get("n_12m") or 0) > 0)
    heavy_year = _sf(run, subject=subject, gosb=gosb, severity=2, period="365")
    heavy_prev = [x for x in _sf(run, subject=subject, gosb=gosb, severity=2, period="730")
                  if x["_at"] < datetime.now(UTC) - timedelta(days=365)]
    return {"run": {"id": run["id"], "finished_at": _iso(run["finished_at"])},
            "source": net.get("story_source"), "react": _attach_src(react), "tiles": tiles,
            "network_share": rmap.get("network_share"), "list": lst, "total": len(full), "in_scope": len(scope),
            "filtered": filtered, "tiers": dict(Counter(x["tier"] for x in lst)),
            "gosbs": [{"gosb": g, "n": n} for g, n in gosbs.most_common()],
            "gosb_table": _gosb_table(run, subject),
            "summary": {"heavy_year": len(heavy_year), "heavy_prev": len(heavy_prev),
                        "points_year": len({x["point"] for x in heavy_year}),
                        "vulnerable_year": sum(1 for x in heavy_year if x.get("vulnerable")),
                        "named_year": sum(1 for x in heavy_year if x["_named"]),
                        "actions": _action_facets(heavy_year)[:10]},
            "with_data": with_data, "points": len(run["points"])}


def _month_bars(point: str) -> list[dict]:
    with engine().connect() as c:
        rows = c.execute(select(reviews.c.review_date, reviews.c.rating).where(
            _point_filter(point), reviews.c.review_date.isnot(None)))
        months: Counter = Counter()
        for d, r in rows:
            if r is not None and r <= 2:
                months[d.strftime("%Y-%m")] += 1
    now = datetime.now(UTC)
    out = []
    for i in range(23, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        k = f"{y}-{m:02d}"
        out.append({"month": k, "neg": months.get(k, 0)})
    return out


@router.get("/dossier/{point}")
def dossier(point: str, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    p = run["points"].get(point)
    if not p:
        raise HTTPException(404, "отделение не найдено")
    a = p.get("risk") or {}
    stories = p.get("stories") or []
    nearby = []
    if a.get("action_label") and p.get("gosb"):
        for q in run["points"].values():
            qa = q.get("risk") or {}
            if q["point"] != point and q.get("gosb") == p.get("gosb") and qa.get("action_label") == a.get("action_label") \
                    and qa.get("heavy_clients", 0) >= 1:
                nearby.append(_place(q))
    bars = _month_bars(point)
    full = _sf(run, point=point)
    stories = _attach_src([_story_out(x, {}) for x in full[:300]] or stories)
    vol = a.get("vol_2y") or 0
    n12 = p.get("n_12m") or 0
    neg12 = p.get("neg_12m") if p.get("neg_12m") is not None else sum(b["neg"] for b in bars[-12:])
    heavy2 = sum(1 for x in full if (x.get("severity") or 0) >= 2 and x["_at"] >= datetime.now(UTC) - timedelta(days=730))
    peer = (run["network"].get("peers") or {}).get(p.get("gosb")) or {}
    compare = [
        {"key": "n_12m", "label": "Отзывов за год", "me": n12, "peer": peer.get("n_12m"), "fmt": "n"},
        {"key": "neg_share", "label": "Доля жалоб (1–2★) за год", "me": round(neg12 / n12, 3) if n12 >= 10 else None,
         "peer": peer.get("neg_share"), "fmt": "p"},
        {"key": "heavy_per100", "label": "Тяжёлых историй на 100 отзывов за 2 года",
         "me": round(100 * heavy2 / vol, 2) if vol >= 10 else None, "peer": peer.get("heavy_per100"), "fmt": "x"},
    ]
    return {**_place(p), "vol_2y": a.get("vol_2y"), "open_date": p.get("open_date"),
            "passport": _passport(p), "lat": p.get("lat"), "lon": p.get("lon"),
            "risk": a, "stories": stories, "bars": bars, "nearby": nearby[:5],
            "compare": compare, "peer_points": peer.get("points"), "actions": _action_facets(full),
            "source": (run["network"].get("story_source")),
            "limits": ["Отзывы — слова клиентов, а не установленные факты",
                       "Пожилые клиенты пишут редко — за них часто пишут родственники",
                       "Отзывы, удалённые площадкой до начала сбора, не видны",
                       "Имена сотрудников — как написали клиенты, только внутри этого отделения"],
            "cards": p.get("cards") or []}


# ── «Истории»: все истории клиентов с фильтрами ───────────────────────────────

@router.get("/stories")
def stories_list(subject: Optional[str] = None, gosb: Optional[str] = None, point: Optional[str] = None,
                 action: Optional[str] = None, severity: int = 0, period: Optional[str] = "730",
                 vulnerable: bool = False, named: bool = False, money: bool = False, q: Optional[str] = None,
                 fmt: Optional[str] = None, sort: str = "new", limit: int = 50, offset: int = 0,
                 user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    kw = _story_kw(subject, gosb, point, action, severity, period, vulnerable, named, money, q, fmt)
    xs = _sf(run, **kw)
    if sort == "severe":
        xs = sorted(xs, key=lambda x: (-(x.get("severity") or 0), -x["_at"].timestamp()))
    base = _sf(run, **{**kw, "action": None}) if action else xs
    pts = run["points"]
    return {"total": len(xs), "points": len({x["point"] for x in xs}),
            "clients": len({x.get("author") or x["rid"] for x in xs}),
            "heavy": sum(1 for x in xs if (x.get("severity") or 0) >= 2),
            "items": _attach_src([_story_out(x, pts) for x in xs[offset:offset + min(max(limit, 1), 200)]]),
            "actions": _action_facets(base), "months": _months(xs),
            "source": run["network"].get("story_source")}


@router.get("/export/stories.xlsx")
def export_stories(subject: Optional[str] = None, gosb: Optional[str] = None, point: Optional[str] = None,
                   action: Optional[str] = None, severity: int = 0, period: Optional[str] = "730",
                   vulnerable: bool = False, named: bool = False, money: bool = False, q: Optional[str] = None,
                   fmt: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    xs = _sf(run, **_story_kw(subject, gosb, point, action, severity, period, vulnerable, named, money, q, fmt))
    pts = run["points"]
    head = ["Дата", "Город", "Адрес", "ВСП", "ГОСБ", "Регион", "Что произошло", "Тяжесть", "Сотрудник",
            "Пожилой или уязвимый клиент", "Сумма, ₽", "Слова клиента", "Пересказ ИИ", "Оценка", "Отзыв"]
    data = []
    cap = 20000
    for x in xs[:cap]:
        pl_ = pts.get(x["point"]) or {}
        data.append([x.get("date"), pl_.get("city"), pl_.get("address"), pl_.get("name"), x.get("gosb"),
                     subject_name(x["subject"]) if x.get("subject") else "", ", ".join(x["labels"]),
                     SEVERITY.get(x.get("severity") or 0, ""), x.get("employee") or "",
                     "да" if x.get("vulnerable") else "", x.get("money") or "",
                     x.get("quote") if x.get("quote_ok", True) else "",
                     "; ".join(t for t in ((x.get("episode") or ""), ("" if x.get("quote_ok", True) else
                                                                      f"не дословно: {x.get('quote')}")) if t),
                     x.get("rating"), x["rid"]])
    title = "Аудит сети ВСП · истории клиентов"
    if len(xs) > cap:
        title += f" · первые {cap:,} из {len(xs):,}".replace(",", " ")
    xb = _xlsx(title, [("Истории клиентов", head, data, {"Слова клиента", "Пересказ ИИ", "Адрес", "Что произошло"})])
    return _xlsx_response(xb, "ВСП_истории.xlsx")


# ── таблица отделений: рейтинг по многим параметрам ──────────────────────────

TABLE_SORT = {"heavy", "per100", "clients", "last", "signals", "vulnerable", "named"}


@router.get("/table")
def table(subject: Optional[str] = None, gosb: Optional[str] = None, action: Optional[str] = None,
          period: Optional[str] = "730", vulnerable: bool = False, named: bool = False, fmt: Optional[str] = None,
          sort: str = "heavy", limit: int = 300, offset: int = 0, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    kw = dict(subject=subject, gosb=gosb, action=action, vulnerable=vulnerable, named=named, fmt=fmt)
    xs = _sf(run, period=period, **kw)
    heavy2y = Counter(x["point"] for x in _sf(run, period="730", severity=2, **kw))
    by: dict[str, list[dict]] = defaultdict(list)
    for x in xs:
        by[x["point"]].append(x)
    from ..vsp.signals.risk import ACTIONS
    pts = run["points"]
    rows = []
    for pt, ss in by.items():
        P = pts.get(pt) or {}
        a = P.get("risk") or {}
        heavy = [x for x in ss if (x.get("severity") or 0) >= 2]
        vol = a.get("vol_2y") or 0
        acts = Counter(k for x in ss for k in x["actions"])
        rows.append({**_place(P), "stories": len(ss), "heavy": len(heavy), "very": sum(1 for x in ss if (x.get("severity") or 0) >= 3),
                     "clients": len({x.get("author") or x["rid"] for x in heavy}),
                     "per100": round(100 * heavy2y[pt] / vol, 2) if vol >= 10 else None, "vol_2y": vol or None,
                     "last": ss[0]["date"], "last_at": ss[0]["at"],
                     "vulnerable": sum(1 for x in ss if x.get("vulnerable")), "named": sum(1 for x in ss if x["_named"]),
                     "actions": [ACTIONS[k]["short"] for k, _ in acts.most_common(3)],
                     "listed": bool(a.get("include")), "signals": len(a.get("signals") or []),
                     "confidence": (a.get("confidence") or {}).get("level")})
    key = sort if sort in TABLE_SORT else "heavy"
    if key == "last":
        rows.sort(key=lambda r: r["last_at"] or "", reverse=True)
    else:
        rows.sort(key=lambda r: (-(r.get(key) or 0), -r["heavy"], -r["stories"]))
    off = max(offset, 0)
    return {"total": len(rows), "items": rows[off:off + min(max(limit, 1), 1000)], "offset": off, "sort": key}


_MALE_EI = {"алексей", "сергей", "андрей", "матвей", "тимофей", "корней", "евсей", "моисей", "гордей", "елисей"}


def _nom_word(w: str) -> str:
    """Имя — как обычно пишут в начальной форме. Клиенты пишут «спасибо Анне», «со Светланой»;
    без морфологической библиотеки — только надёжные женские окончания, мужские имена не трогаем."""
    lw = w.lower()
    if len(w) < 4 or not w[:1].isupper() or lw in _MALE_EI:
        return w
    for end, rep in (("ьей", "ья"), ("ией", "ия"), ("ой", "а"), ("ье", "ья"), ("ии", "ия")):
        if lw.endswith(end):
            return w[: len(w) - len(end)] + rep
    if lw.endswith("е") and not lw.endswith(("ее", "ие", "ые", "ое")):
        return w[:-1] + "а"
    return w


def _nom(name: str | None) -> str:
    return " ".join(_nom_word(w) for w in (name or "").split())


def _split_people(name: str | None) -> list[str]:
    """«Иванова Анна, Петрова Ольга» и «Анна и Ольга» — это два человека."""
    import re as _re
    return [x.strip() for x in _re.split(r",|;|\s+и\s+", name or "") if len(x.strip()) >= 3]


# ── «Люди»: сотрудники, которых называют разные клиенты; похвала от новых аккаунтов; посредники ─

@router.get("/staff")
def staff(subject: Optional[str] = None, gosb: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    pts = run["points"]
    inscope = lambda pt: ((not subject or (pts.get(pt) or {}).get("subject") == subject)  # noqa: E731
                          and (not gosb or (pts.get(pt) or {}).get("gosb") == gosb))
    # 1) имя в историях модели: одно отделение, разные клиенты
    named: dict[tuple, list[dict]] = defaultdict(list)
    for x in _stories(run):
        if not inscope(x["point"]):
            continue
        for e in _split_people(x.get("employee")):
            named[(x["point"], _nom(e).lower().replace("ё", "е"))].append({**x, "employee": _nom(e)})
    model_rows = []
    for (pt, _), ss in named.items():
        clients = len({x.get("author") or x["rid"] for x in ss})
        model_rows.append({"kind": "named", "name": Counter(x["employee"].strip() for x in ss).most_common(1)[0][0],
                           "clients": clients, "stories": len(ss), "heavy": sum(1 for x in ss if (x.get("severity") or 0) >= 2),
                           "last": ss[0]["date"], "actions": sorted({l for x in ss for l in x["labels"]})[:3],
                           "quote": ss[0]["quote"], "rid": ss[0]["rid"], "place": _place(pts.get(pt))})
    model_rows.sort(key=lambda r: (-r["clients"], -r["heavy"]))
    # 2) имена по словам (vsp_person): жалобы и волны похвалы
    heavy_by_pt = Counter(x["point"] for x in _stories(run) if (x.get("severity") or 0) >= 2)
    comp, praise = [], []
    for x in run["persons"]:
        if not inscope(x["point"]):
            continue
        row = {"id": x["id"], "name": _nom(x["name"]), "n": x["n"], "authors": x["authors"],
               "first": _iso(x["first_at"]), "last": _iso(x["last_at"]),
               "oneoff": x["detail"].get("oneoff"), "full_name": bool(x["detail"].get("full_name")),
               "types": [RISK[k].label if k in RISK else k for k in (x["detail"].get("types") or {})],
               "heavy_here": heavy_by_pt.get(x["point"], 0), "place": _place(pts.get(x["point"]))}
        (comp if x["kind"] == "complaints" else praise).append(row)
    # одно и то же имя из карточек модели и из слов — одна строка (ключ — основы слов имени, как в
    # vsp_person). Клиентов — больше из двух: модель могла отметить не все отзывы с этим именем.
    from ..vsp.signals.people import stem
    def _stems(name):
        return {stem(w) for w in (name or "").split()[:3] if w}
    keep = []
    for r in comp:
        pk = set(((next((p_ for p_ in run["persons"] if p_["id"] == r["id"]), {}).get("detail") or {}).get("key")
                  or "").split("|")) - {""} or _stems(r["name"])
        m = next((mr for mr in model_rows if mr["place"].get("point") == r["place"].get("point")
                  and _stems(mr["name"]) & pk), None)
        if m is not None:
            m.setdefault("id", r["id"])
            m.setdefault("full_name", bool(r.get("full_name")))
            m["authors"] = max(m.get("authors") or 0, r["authors"])
            m["clients"] = max(m["clients"], r["authors"])
        elif (r["name"] or "").strip():
            keep.append(r)
    model_rows.sort(key=lambda r: (-r["clients"], -r["heavy"]))
    comp = sorted(keep, key=lambda r: (-r["authors"], -r["n"]))
    praise = [r for r in praise if (r["name"] or "").strip() and r["heavy_here"] > 0]
    praise.sort(key=lambda r: (-(r["heavy_here"] > 0), -(r["oneoff"] or 0), -r["n"]))
    # 3) посредники и обслуживание по знакомству
    mid: dict[str, list[dict]] = defaultdict(list)
    for x in _stories(run):
        if inscope(x["point"]) and ({"посредник", "блат_очередь"} & set(x["actions"])):
            mid[x["point"]].append(x)
    middle = sorted(({"place": _place(pts.get(pt)), "stories": len(ss), "last": ss[0]["date"], "quote": ss[0]["quote"],
                      "rid": ss[0]["rid"], "kinds": sorted({l for x in ss for l in x["labels"]
                                                            if l in ("посредники и «свой» менеджер",
                                                                     "обслуживают по знакомству, в обход очереди")})}
                     for pt, ss in mid.items()), key=lambda r: -r["stories"])
    return {"named": _attach_src(model_rows[:200]), "complaints": comp[:200], "praise": praise[:200],
            "middlemen": _attach_src(middle[:100]),
            "source": run["network"].get("story_source")}


# ── «Динамика»: что растёт ────────────────────────────────────────────────────

@router.get("/trends")
def trends(subject: Optional[str] = None, gosb: Optional[str] = None, action: Optional[str] = None,
           user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    from ..vsp.signals.risk import ACTIONS
    xs = _sf(run, subject=subject, gosb=gosb, action=action, severity=2, period="730")
    now = datetime.now(UTC)
    h1, h0 = now - timedelta(days=182), now - timedelta(days=364)
    top = [k for k, _ in Counter(a for x in xs for a in x["actions"] if a != "другое").most_common(6)]
    months = _months(xs)
    by_m: dict[str, Counter] = defaultdict(Counter)
    for x in xs:
        k = x["_at"].strftime("%Y-%m")
        for a in x["actions"]:
            by_m[k][a if a in top else "_other"] += 1
    for m in months:
        m["by"] = dict(by_m.get(m["month"], {}))

    def growth(keyf):
        cur, prev = Counter(), Counter()
        for x in xs:
            for k in keyf(x):
                if not k:
                    continue
                if x["_at"] >= h1:
                    cur[k] += 1
                elif x["_at"] >= h0:
                    prev[k] += 1
        return [{"key": k, "now": cur[k], "before": prev[k], "delta": cur[k] - prev[k]} for k in set(cur) | set(prev)]

    acts = sorted(growth(lambda x: x["actions"]), key=lambda r: -r["delta"])
    for r in acts:
        r["label"] = ACTIONS.get(r["key"], ACTIONS["другое"])["label"]
    n12: Counter = Counter()
    gsub: dict[str, str] = {}
    for p in run["points"].values():
        if p.get("gosb"):
            n12[p["gosb"]] += p.get("n_12m") or 0
            gsub.setdefault(p["gosb"], p.get("subject") or "")
    gs = sorted(growth(lambda x: [x.get("gosb")]), key=lambda r: -r["delta"])
    for r in gs:
        r["subject_name"] = subject_name(gsub.get(r["key"])) if gsub.get(r["key"]) else None
        r["n_12m"] = n12.get(r["key"], 0)
    first: dict[str, datetime] = {}
    for x in _sf(run, subject=subject, gosb=gosb, action=action, severity=2):      # за всё время
        first[x["point"]] = min(first.get(x["point"], x["_at"]), x["_at"])
    cnt = Counter(x["point"] for x in xs)
    fresh_cut = now - timedelta(days=90)
    new_pts = [{"place": _place(run["points"].get(pt)), "first": d.strftime("%d.%m.%Y"), "n": cnt[pt]}
               for pt, d in first.items() if d >= fresh_cut]
    new_pts.sort(key=lambda r: -r["n"])
    return {"months": months, "top": [{"key": k, "label": ACTIONS[k]["label"], "short": ACTIONS[k]["short"]} for k in top],
            "actions": acts[:15], "gosbs": gs[:20], "new_points": new_pts[:20], "total": len(xs),
            "half_now": sum(1 for x in xs if x["_at"] >= h1), "half_before": sum(1 for x in xs if h0 <= x["_at"] < h1),
            "source": run["network"].get("story_source")}


FORMAT_LABEL = {"mass": "массовое обслуживание", "sber_first": "СберПервый", "private": "Private Banking",
                "legal": "юрлица", "business": "бизнес", "corporate": "корпоративные клиенты",
                "islamic": "исламский банкинг", "zone24": "зона 24/7", "mobile": "передвижной офис"}


def _passport(p: dict) -> dict:
    """Что за отделение: карточки на площадках (ссылки), реестр, сайт банка (часы, форматы)."""
    out = {"cards": [], "hours": None, "formats": [], "parent_name": None, "registry_status": None}
    bid = p.get("branch_id")
    with engine().connect() as c:
        if bid:
            cond = source_branches.c.branch_id == bid
        else:
            src, _, sbid = p["point"].partition(":")
            cond = and_(source_branches.c.source == src, source_branches.c.source_branch_id == sbid)
        for r in c.execute(select(source_branches.c.source, source_branches.c.url, source_branches.c.declared_reviews,
                                  source_branches.c.declared_rating, source_branches.c.name).where(cond)):
            out["cards"].append({"source": r.source, "url": _safe_url(r.url), "reviews": r.declared_reviews,
                                 "rating": r.declared_rating, "name": r.name})
        if bid:
            b = c.execute(select(branches.c.parent_name, branches.c.status).where(branches.c.branch_id == bid)).first()
            if b:
                out["parent_name"], out["registry_status"] = b.parent_name, b.status
            try:
                from ..vsp.storage import branch_profiles
                pr = c.execute(select(branch_profiles.c.hours, branch_profiles.c.formats).where(
                    branch_profiles.c.branch_id == bid, branch_profiles.c.status == "active")).first()
                if pr:
                    h = json.loads(pr.hours or "{}") or {}
                    out["hours"] = h.get("text") if isinstance(h, dict) else None
                    out["formats"] = [FORMAT_LABEL.get(f, f) for f in (json.loads(pr.formats or "[]") or [])]
            except Exception:  # noqa: BLE001 — паспорта с сайта банка может не быть
                pass
    if not out["formats"]:
        out["formats"] = [FORMAT_LABEL.get(k, k) for k in (p.get("formats") or {})]
    return out


# ── проверка истории: полный отзыв, что увидела модель, проверки кодом ─────────

@router.get("/evidence/{rid:path}")
def evidence(rid: str, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    from ..vsp.signals.llm_read import PROMPT_VERSION, SCREEN_VERSION, quote_ok, review_llm
    from ..vsp.signals.risk import ACTIONS
    with engine().connect() as c:
        r = c.execute(select(reviews).where(reviews.c.review_id == rid)).mappings().first()
        if not r:
            raise HTTPException(404, "отзыв не найден")
        cards_ = {}
        try:
            for x in c.execute(select(review_llm).where(review_llm.c.review_id == rid)).mappings():
                cards_[x["prompt_version"]] = x
        except Exception:  # noqa: BLE001 — модель ещё не читала
            pass
    row = _review_row(r, run)
    row["url"] = _safe_url(row.get("url"))
    story = next((x for x in _stories(run) if x["rid"] == rid), None)
    text = row.get("text") or ""
    card = cards_.get(PROMPT_VERSION)
    data = {}
    if card is not None:
        try:
            data = json.loads(card["data_json"] or "{}") or {}
        except ValueError:
            data = {}
    quote = (story or {}).get("quote") or data.get("quote") or ""
    emp = (story or {}).get("employee") or (data.get("employee_name") or "").strip()
    low = text.lower().replace("ё", "е")
    checks = []
    if quote:
        checks.append({"ok": quote_ok(quote, text), "label": "Слова клиента дословно есть в отзыве" if quote_ok(quote, text)
                       else "Цитаты дословно нет в отзыве — это пересказ, сверьте с текстом"})
    if emp and emp.lower() not in ("null", "none", "нет", "-"):
        ok = all(w.lower().replace("ё", "е")[:4] in low for w in emp.split()[:2])
        checks.append({"ok": ok, "label": f"Имя «{emp}» есть в тексте" if ok else f"Имени «{emp}» в тексте нет — не учитываем"})
    if story and story.get("source") == "модель":
        checks.append({"ok": True, "label": "Отбор и карточка — модель; цитату и имя проверяет код"})
    elif story:
        checks.append({"ok": None, "label": "Найдено по словам, модель этот отзыв ещё не разбирала"})
    model = None
    if data:
        model = {"what": data.get("episode") or (card or {}).get("summary"), "scheme": data.get("scheme_free"),
                 "actions": [ACTIONS[a]["label"] for a in (data.get("actions") or []) if a in ACTIONS],
                 "products": data.get("products") or [], "severity": SEVERITY.get(int(data["severity"]) if str(data.get("severity") or "").isdigit() else 0, ""),
                 "fault": data.get("fault"), "employee": emp or None, "money": data.get("money_rub"),
                 "vulnerable": bool(data.get("vulnerability")), "relevant": bool(data.get("relevant")),
                 "other_vsp": data.get("other_vsp") or None, "model": (card or {}).get("model")}
    screen = cards_.get(SCREEN_VERSION)
    pt = row.get("point")
    return {"review": row, "quote": quote, "employee": emp or None,
            "story": _story_out(story, {}) if story else None, "model": model,
            "screen": {"relevant": bool(screen["relevant"]), "why": screen["summary"]} if screen is not None else None,
            "checks": checks, "place": _place(run["points"].get(pt)), "point": pt}


# ── «Обзор»: главные случаи и картина по сети ─────────────────────────────────
# Не «раз навязали», а то, что интересно аудиту: очень тяжёлое, крупные суммы, пожилые и уязвимые,
# действия «изнутри» (доступ к телефону, касса, утечка данных, посредники, «по знакомству»,
# платная биометрия), один сотрудник у нескольких клиентов. Балл прозрачный, причины — метками.

INSIDER = {"доступ_к_телефону_или_аккаунту": "доступ к телефону клиента", "касса_недостача_или_чужой_платеж": "касса",
           "оглашение_или_утечка_данных": "утечка данных", "посредник": "посредник", "блат_очередь": "по знакомству",
           "биометрия": "биометрия", "накрутка_продаж": "продажи «для плана»", "наказание_за_отказ": "наказание за отказ"}
MONEY_CAP = 50_000_000          # больше — почти наверняка ошибка чтения суммы


def _money(x) -> float:
    m = x.get("money")
    return float(m) if isinstance(m, (int, float)) and 0 < m < MONEY_CAP else 0.0


def _interest(x: dict, named_many: set, listed_tier: dict, now: datetime) -> tuple[int, list[str]]:
    s, why = 0, []
    sev = x.get("severity") or 0
    if sev >= 3:
        s += 4; why.append("очень тяжёлая")
    elif sev >= 2:
        s += 1
    m = _money(x)
    if m >= 100_000:
        s += 3
    elif m >= 30_000:
        s += 2
    elif m >= 10_000:
        s += 1
    if m >= 10_000:
        why.append(f"{int(m):,} ₽".replace(",", " "))
    if x.get("vulnerable"):
        s += 2; why.append(((x.get("vuln_types") or ["пожилой или уязвимый клиент"])[0]).split("/")[0])
    ins = [INSIDER[a] for a in x["actions"] if a in INSIDER]
    if ins:
        s += 2; why.append(ins[0])
    emp = (x.get("employee") or "").strip()
    if emp and (x["point"], _nom(emp).lower()) in named_many:
        s += 2; why.append("сотрудника называют несколько клиентов")
    if x["_at"] >= now - timedelta(days=120):
        s += 1; why.append("свежая")
    if listed_tier.get(x["point"]) in ("urgent", "converged"):
        s += 1
    if x.get("dup_points"):
        s -= 3
    return s, why


@router.get("/brief")
def brief(subject: Optional[str] = None, gosb: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    from ..vsp.signals.risk import ACTIONS
    pts = run["points"]
    now = datetime.now(UTC)
    y1, y2 = now - timedelta(days=365), now - timedelta(days=730)
    xs = _sf(run, subject=subject, gosb=gosb, severity=2, period="730")
    year = [x for x in xs if x["_at"] >= y1]
    prev = [x for x in xs if x["_at"] < y1]
    listed = {p["point"]: (p.get("risk") or {}).get("tier") for p in pts.values() if (p.get("risk") or {}).get("include")}
    # один сотрудник у нескольких клиентов одного отделения
    by_emp: dict[tuple, set] = defaultdict(set)
    for x in xs:
        for e in _split_people(x.get("employee")):
            by_emp[(x["point"], _nom(e).lower())].add(x.get("author") or x["rid"])
    named_many = {k for k, v in by_emp.items() if len(v) >= 2}
    # главные случаи: балл + разнообразие (тип, отделение, ГОСБ)
    cand = []
    for x in xs:
        if not x.get("quote_ok", True):
            continue
        sc, why = _interest(x, named_many, listed, now)
        if sc >= 5:
            cand.append((sc, x["_at"], x, why))
    cand.sort(key=lambda t: (-t[0], -t[1].timestamp()))
    picked, per_act, per_pt, per_g = [], Counter(), set(), Counter()
    for sc, _, x, why in cand:
        a = next((a for a in x["actions"] if a in INSIDER), x["actions"][0])
        if per_act[a] >= 3 or x["point"] in per_pt or per_g[x.get("gosb")] >= 3:
            continue
        per_act[a] += 1; per_pt.add(x["point"]); per_g[x.get("gosb")] += 1
        picked.append({**_story_out(x, pts), "score": sc, "why": why})
        if len(picked) >= 12:
            break
    _attach_src(picked)
    # деньги
    buckets = [("до 5 тыс.", 0, 5_000), ("5–30 тыс.", 5_000, 30_000), ("30–100 тыс.", 30_000, 100_000),
               ("100–500 тыс.", 100_000, 500_000), ("от 500 тыс.", 500_000, MONEY_CAP)]
    money = [(m, x) for x in xs if (m := _money(x))]
    hist = [{"label": l, "n": sum(1 for m, _ in money if lo <= m < hi), "sum": round(sum(m for m, _ in money if lo <= m < hi))}
            for l, lo, hi in buckets]
    top_money = [{**_story_out(x, pts), "amount": m} for m, x in sorted(money, key=lambda t: -t[0])[:5]]
    _attach_src(top_money)
    # кого обманывают и через какие продукты
    vt = Counter(t.split("/")[0] for x in xs for t in (x.get("vuln_types") or (["пожилой или уязвимый клиент"] if x.get("vulnerable") else [])))
    vul_acts = Counter(a for x in xs if x.get("vulnerable") for a in x["actions"] if a not in ("другое",))
    prod = Counter(p_ for x in xs for p_ in (x.get("products") or []) if p_ and p_ != "другое")
    prod_v = Counter(p_ for x in xs if x.get("vulnerable") for p_ in (x.get("products") or []) if p_ and p_ != "другое")
    # что растёт: тяжёлые за год против года раньше
    ay, ap = Counter(a for x in year for a in x["actions"]), Counter(a for x in prev for a in x["actions"])
    acts = sorted(({"key": a, "label": ACTIONS[a]["short"], "now": ay[a], "before": ap[a]} for a in set(ay) | set(ap)
                   if a in ACTIONS and a != "другое"), key=lambda r: -r["now"])[:10]
    # сговор и «свои»
    staff = []
    for (pt, nm), auth in sorted(by_emp.items(), key=lambda kv: -len(kv[1])):
        if len(auth) < 2:
            break
        ss = [x for x in xs if x["point"] == pt and any(_nom(e).lower() == nm for e in _split_people(x.get("employee")))]
        if not ss:
            continue
        top = max(ss, key=lambda x: (x.get("severity") or 0, x["_at"]))
        staff.append({"name": _nom(top.get("employee")), "clients": len(auth), "place": _place(pts.get(pt)),
                      "labels": sorted({l for x in ss for l in x["labels"]})[:3], "quote": top.get("quote"),
                      "rid": top["rid"], "date": top.get("date")})
        if len(staff) >= 6:
            break
    insiders = Counter(INSIDER[a] for x in year for a in x["actions"] if a in INSIDER)
    # ГОСБ, где тяжёлых историй много для своего размера
    gt = [g for g in _gosb_table(run, subject) if g.get("per100") is not None and g["n_12m"] >= 300]
    net_rate = (sum(g["heavy"] for g in gt) / max(1, sum(g["n_12m"] for g in gt))) * 100 if gt else 0
    for g in gt:
        g["ratio"] = round(g["per100"] / net_rate, 1) if net_rate else None
    gosb_hot = sorted(gt, key=lambda g: -(g["ratio"] or 0))[:8]
    prog = None
    try:
        prog = _progress()
    except Exception:  # noqa: BLE001
        pass
    return {
        "run": {"id": run["id"], "finished_at": _iso(run["finished_at"])}, "source": run["network"].get("story_source"),
        "kpi": {"read": (prog or {}).get("screened"), "stories": (prog or {}).get("relevant"),
                "heavy_2y": len(xs), "heavy_year": len(year), "heavy_prev": len(prev),
                "points_year": len({x["point"] for x in year}), "vulnerable_year": sum(1 for x in year if x.get("vulnerable")),
                "money_sum": round(sum(m for m, _ in money)), "money_n": len(money),
                "very_year": sum(1 for x in year if (x.get("severity") or 0) >= 3)},
        "cases": picked, "money_hist": hist, "money_top": top_money,
        "vuln_types": [{"label": k, "n": n} for k, n in vt.most_common(6)],
        "vuln_actions": [{"label": ACTIONS[a]["short"], "n": n} for a, n in vul_acts.most_common(6) if a in ACTIONS],
        "products": [{"label": k, "n": n, "vulnerable": prod_v.get(k, 0)} for k, n in prod.most_common(8)],
        "actions": acts, "staff": staff, "insiders": [{"label": k, "n": n} for k, n in insiders.most_common(8)],
        "gosb_hot": gosb_hot, "net_per100": round(net_rate, 2),
        "listed": dict(Counter(listed.values())), "react": len(run["network"].get("risk_react") or []),
    }


def _scheme_rows(run) -> tuple[list[dict], str]:
    """Схемы: кластеры модели (vsp_scheme, последняя сборка), иначе — по действиям из карточек."""
    try:
        from ..vsp.signals.schemes import scheme_member, scheme_t
        with engine().connect() as c:
            ver = c.execute(select(func.max(scheme_t.c.version))).scalar()
            if ver:
                rows = [dict(r) for r in c.execute(select(scheme_t).where(scheme_t.c.version == ver,
                                                                           scheme_t.c.relevance >= 2)).mappings()]
                mem = defaultdict(list)
                for rid, sid in c.execute(select(scheme_member.c.review_id, scheme_member.c.scheme_id).where(
                        scheme_member.c.version == ver)):
                    mem[sid].append(rid)
                out = []
                for r in rows:
                    rids = mem.get(r["id"], [])
                    pts = {x for x in (run["review_point"].get(i) for i in rids) if x}
                    out.append({"id": f"k{r['id']}", "name": r["name"], "description": r["description"],
                                "stories": r["size"], "points": len(pts), "recent": r["recent"],
                                "novel": bool(r["novel"]), "check": json.loads(r["check"] or "[]"),
                                "benign": "; ".join(json.loads(r["benign"] or "[]")), "resolve": "",
                                "rids": rids})
                return sorted(out, key=lambda x: (-x["points"], -x["stories"])), "модель"
    except Exception:  # noqa: BLE001 — кластеров ещё нет
        pass
    return run["network"].get("schemes_fallback") or [], "действия"


@router.get("/schemes")
def schemes(user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    rows, kind = _scheme_rows(run)
    brief = [{k: v for k, v in r.items() if k not in ("examples", "rids", "by_subject")} for r in rows]
    return {"kind": kind, "new": [b for b in brief if b.get("novel")][:8], "all": brief}


@router.get("/schemes/{sid}")
def scheme_card(sid: str, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    rows, kind = _scheme_rows(run)
    x = next((r for r in rows if str(r["id"]) == sid), None)
    if not x:
        raise HTTPException(404, "схема не найдена")
    pts = run["points"]
    examples = x.get("examples")
    if examples is None:                      # кластер модели: цитаты из историй отделений
        examples = []
        want = set(x.get("rids") or [])
        for p in pts.values():
            for s_ in p.get("stories") or []:
                if s_["rid"] in want:
                    examples.append({"rid": s_["rid"], "point": p["point"], "quote": s_["quote"], "date": s_["date"]})
    by_subject = Counter((pts.get(e["point"]) or {}).get("subject") for e in examples)
    gosbs = {(pts.get(e["point"]) or {}).get("gosb") for e in examples} - {None}
    return {**{k: v for k, v in x.items() if k not in ("rids",)}, "kind": kind,
            "examples": _attach_src([{**e, "place": _place(pts.get(e["point"]))} for e in examples[:30]]),
            "by_subject": {k: v for k, v in by_subject.items() if k}, "network_wide": len(gosbs) >= 3,
            "gosbs": len(gosbs)}


@router.get("/search")
def search(q: str, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    ql = q.strip().lower()
    if len(ql) < 2:
        return {"places": [], "people": [], "reviews": []}
    places, people = [], []
    for p in run["points"].values():
        hay = " ".join(str(p.get(k) or "") for k in ("name", "city", "address", "gosb")).lower()
        if ql in hay:
            places.append(_place(p))
        for x in (p.get("risk") or {}).get("people") or []:
            if ql in (x.get("name") or "").lower():
                people.append({"name": x["name"], "clients": x["clients"], "place": _place(p)})
    with engine().connect() as c:
        rv = list(c.execute(select(reviews.c.review_id, reviews.c.review_text, reviews.c.branch_id, reviews.c.source,
                                   reviews.c.source_branch_id, reviews.c.review_date).where(
            reviews.c.review_text.ilike(f"%{q.strip()}%")).order_by(reviews.c.review_date.desc()).limit(15)))
    revs = []
    for r in rv:
        pt = f"b{r.branch_id}" if r.branch_id else f"{r.source}:{r.source_branch_id}"
        t = r.review_text or ""
        i = t.lower().find(ql)
        revs.append({"rid": r.review_id, "point": pt, "date": _iso(r.review_date),
                     "snippet": ("…" if i > 60 else "") + t[max(0, i - 60): i + 140] + "…",
                     "place": _place(run["points"].get(pt))})
    return {"places": places[:12], "people": people[:12], "reviews": revs}


class FeedbackIn(BaseModel):
    target: str
    verdict: str          # полезно | неинтересно | ошибка данных | не схема


@router.post("/feedback")
def feedback(body: FeedbackIn, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    if body.verdict not in ("полезно", "неинтересно", "ошибка данных", "не схема"):
        raise HTTPException(400, "неизвестная отметка")
    telemetry.track_batch(user.username, [{"type": "vsp_feedback", "props": {"target": body.target[:200],
                                                                              "verdict": body.verdict}}])
    log.info("vsp: отметка %s → %s (%s)", body.target[:80], body.verdict, user.username)
    return {"ok": True}


@router.get("/export/justification.xlsx")
def export_justification(points: Optional[str] = None, subject: Optional[str] = None, gosb: Optional[str] = None,
                         action: Optional[str] = None, vulnerable: bool = False, named: bool = False,
                         fresh: bool = False, fmt: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    want = set((points or "").split(",")) - {""}
    only = None
    if action or vulnerable or named or fresh or fmt:          # как на главном экране
        only = {x["point"] for x in _sf(run, subject=subject, gosb=gosb, action=action, vulnerable=vulnerable,
                                         named=named, fmt=fmt, severity=2, period="90" if fresh else "365")}
    rows = [x for x in _listed(run, subject, gosb, only) if not want or x["point"] in want]
    if want:
        rows += [{**_place(run["points"][p]), "why": [], "quote": None, "confidence": None, "benign": None,
                  "action": None} for p in want if p in run["points"] and p not in {x["point"] for x in rows}]
    head = ["Отделение", "Город", "Адрес", "ГОСБ", "Регион", "Почему в списке", "Цитата клиента", "Дата",
            "Уверенность", "Может быть безобидно", "Что запросить заранее", "Что проверить на месте"]
    data, st_rows = [], []
    for x in rows:
        a = (run["points"][x["point"]].get("risk") or {})
        b = a.get("benign") or {}
        data.append([x.get("name"), x.get("city"), x.get("address"), x.get("gosb"), x.get("subject_name"),
                     "; ".join(a.get("why") or []), (a.get("quote") or {}).get("text"), (a.get("quote") or {}).get("date"),
                     (f"{a['confidence']['level']} — {a['confidence']['reason']}" if a.get("confidence") else ""),
                     (f"{b.get('text')}. Снимет вопрос: {b.get('resolve')}" if b else ""),
                     "; ".join(f"{r['text']} ({r['where']})" for r in a.get("request") or []),
                     "; ".join(a.get("check") or [])])
        for s_ in run["points"][x["point"]].get("stories") or []:
            st_rows.append([x.get("name"), x.get("city"), s_["date"], ", ".join(s_["actions"]),
                            {3: "очень тяжёлая", 2: "тяжёлая"}.get(s_["severity"], ""), s_.get("episode") or "",
                            s_["quote"], s_["rid"]])
    head2 = ["Отделение", "Город", "Дата", "Что произошло (кратко)", "Тяжесть", "Пересказ ИИ", "Слова клиента", "Отзыв"]
    xb = _xlsx("Аудит сети ВСП · обоснование", [("Где стоит проверить", head, data,
                                                {"Почему в списке", "Цитата клиента", "Может быть безобидно",
                                                 "Что запросить заранее", "Что проверить на месте", "Адрес"}),
                                               ("Истории клиентов", head2, st_rows, {"Пересказ ИИ", "Слова клиента"})])
    return _xlsx_response(xb, "ВСП_обоснование.xlsx")


@router.get("/export/visit.docx")
def export_visit(point: Optional[str] = None, points: Optional[str] = None,
                 user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    want = [x for x in ([point] if point else []) + (points or "").split(",") if x]
    want = [x for x in dict.fromkeys(want) if x in run["points"]]
    if not want:
        raise HTTPException(404, "отделение не найдено")
    from .export_brand import WD, docx_bytes
    first = run["points"][want[0]]
    title = (f"Лист выезда · {first.get('city') or ''}, {first.get('address') or ''}" if len(want) == 1
             else f"Листы выезда · {len(want)} отделений")
    doc = WD.new(title)
    WD.header_footer(doc, "Аудит сети ВСП · лист выезда")
    for i, pt in enumerate(want):
        if i:
            doc.add_page_break()
        _visit_sheet(doc, run, run["points"][pt])
    return Response(docx_bytes(doc), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('ВСП_лист_выезда.docx')}"})


def _visit_sheet(doc, run, p: dict) -> None:
    from .export_brand import stamp_line
    a = p.get("risk") or {}
    doc.add_heading(f"{p.get('city') or ''}", level=0)
    doc.add_paragraph(f"{p.get('address') or ''} · ВСП {p.get('name') or ''} · ГОСБ {p.get('gosb') or '—'}")
    doc.add_paragraph(f"Черновик — поправьте под проверку. {stamp_line()}")
    doc.add_heading("Почему в списке", level=1)
    for w in a.get("why") or ["Отделение открыто вручную — признаков из отзывов нет"]:
        doc.add_paragraph(w, style="List Bullet")
    if a.get("confidence"):
        doc.add_paragraph(f"Уверенность: {a['confidence']['level']} — {a['confidence']['reason']}")
    if a.get("benign"):
        doc.add_paragraph(f"Может быть безобидно: {a['benign']['text']}. Снимет вопрос: {a['benign']['resolve']}.")
    doc.add_heading("Что запросить заранее", level=1)
    for r in a.get("request") or []:
        doc.add_paragraph(f"☐ {r['text']} — {r['where']}")
    doc.add_heading("Что проверить на месте", level=1)
    for t in a.get("check") or []:
        doc.add_paragraph(f"☐ {t}")
    doc.add_heading("Истории клиентов", level=1)
    for s_ in (p.get("stories") or [])[:15]:
        doc.add_paragraph(f"{s_['date']} · {', '.join(s_['actions'])}: «{s_['quote']}»", style="List Bullet")
    doc.add_heading("Чего мы не видим", level=1)
    doc.add_paragraph("Отзывы — слова клиентов, а не установленные факты. Отзывы, удалённые площадкой до начала "
                      "сбора, не видны. Имена сотрудников — как написали клиенты.")


@router.get("/completeness")
def completeness(user: CurrentUser = Depends(get_current_user)):
    _need(user)
    with engine().connect() as c:
        reg = dict(c.execute(select(branches.c.status, func.count()).group_by(branches.c.status)).all())
        cards = {s: {"cards": n, "linked": l} for s, n, l in c.execute(
            select(source_branches.c.source, func.count(), func.count(source_branches.c.branch_id))
            .group_by(source_branches.c.source))}
        comp = defaultdict(dict)
        for s, k, n in c.execute(select(sync_state.c.source, sync_state.c.completeness, func.count())
                                 .group_by(sync_state.c.source, sync_state.c.completeness)):
            comp[s][k or "unknown"] = n
        rv = {s: {"reviews": n, "oldest": _iso(a), "newest": _iso(b)} for s, n, a, b in c.execute(
            select(reviews.c.source, func.count(), func.min(reviews.c.review_date), func.max(reviews.c.review_date))
            .group_by(reviews.c.source))}
        covered = c.execute(select(func.count(func.distinct(source_branches.c.branch_id)))
                            .where(source_branches.c.branch_id.isnot(None))).scalar()
    run = _run()
    return {"registry": reg, "cards": cards, "completeness": comp, "reviews": rv, "branches_covered": covered,
            "run": {"id": run["id"], "finished_at": _iso(run["finished_at"]), "stats": run["stats"]},
            "limits": CAMPAIGN_GUIDE["limits"]}


# ── выгрузки ──────────────────────────────────────────────────────────────────

def _xlsx(title: str, sheets: list[tuple[str, list[str], list[list], set]]) -> bytes:
    from openpyxl import Workbook
    from .export_brand import SERIF, XL, stamp_line
    wb = Workbook()
    wb.remove(wb.active)
    for name, head, data, wrap in sheets:
        ws = wb.create_sheet(name[:30])
        XL.sheet_base(ws, widths=[max(10, min(60, len(h) + 6)) if h not in wrap else 70 for h in head], title=title)
        t = ws.cell(row=1, column=1, value=title)
        t.font = XL.font(16, name=SERIF)
        ws.row_dimensions[1].height = 26
        XL.stamp(ws, 2, text_=f"{stamp_line()} · {len(data)} строк", span=8)
        XL.table(ws, 4, head, data, wrap=wrap)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_response(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/export/candidates.xlsx")
def export_candidates(subject: Optional[str] = None, gosb: Optional[str] = None, fmt: Optional[str] = None,
                      user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    rows = sorted((p for p in run["points"].values() if p["score"] > 0 and (not subject or p.get("subject") == subject)
                   and (not gosb or p.get("gosb") == gosb) and _fmt_ok(p, fmt)), key=lambda p: -p["score"])
    head = ["Балл", "ВСП", "Город", "Адрес", "ГОСБ", "Субъект", "Отзывов за год", "Рейтинг", "Без эпизодов",
            "Рейтинг на карте 2ГИС", "Жалоб за год", "Эпизодов", "Почему в поле зрения"]
    data = [[p["score"], p.get("name"), p.get("city"), p.get("address"), p.get("gosb"), p.get("subject_name"),
             p.get("n_12m"), p.get("rating_all"), p.get("rating_wo_episodes"),
             (p.get("declared_rating") or {}).get("2gis"), p.get("neg_12m"), len(p.get("episodes") or []),
             "; ".join(p.get("reasons") or [])] for p in rows]
    eps = [e for e in run["episodes"] if e["level"] == "branch"]
    eps.sort(key=lambda e: -e["score"])
    head2 = ["Балл", "Сила", "ВСП", "Город", "С", "По", "Пятёрок", "Ожидание", "Кратность", "Разовые авторы",
             "Пачки до 10 мин", "Конец месяца", "Подтверждены", "Помечено площадкой"]
    data2 = []
    for e in eps:
        p = run["points"].get(e["point"]) or {}
        f = e["features"]
        data2.append([e["score"], e["strength"], p.get("name"), p.get("city"), e["start"].date(), e["end"].date(),
                      e["n"], e["expected"], e["ratio"], f.get("oneoff_share"), f.get("series_share"),
                      f.get("month_end_share"), f.get("confirmed_share"), f.get("hidden")])
    head3 = ["Дата", "Что случилось", "Серьёзность", "Отделение", "Город", "Оценка", "Слова клиента"]
    data3 = []
    for x in sorted(_case_items(run, run["cases"]), key=lambda x: (x["severity"], x["at"] or ""), reverse=True)[:5000]:
        pl = x.get("place") or {}
        data3.append([x["at"][:10] if x["at"] else None, "; ".join(t["label"] for t in x["types"]),
                      {3: "очень серьёзно", 2: "серьёзно"}.get(x["severity"], "процесс"),
                      pl.get("name"), pl.get("city"), x["rating"], x["quote"]])
    head4 = ["Сотрудник (как пишут клиенты)", "Что видно", "Отзывов", "Разных клиентов", "С", "По", "Отделение", "Город"]
    data4 = []
    for x in sorted(run["persons"], key=lambda x: -(x["score"] or 0)):
        pl = run["points"].get(x["point"]) or {}
        data4.append([x["name"], "в жалобах разных клиентов" if x["kind"] == "complaints" else "хвалят новые аккаунты",
                      x["n"], x["authors"], _iso(x["first_at"])[:10] if x["first_at"] else None,
                      _iso(x["last_at"])[:10] if x["last_at"] else None, pl.get("name"), pl.get("city")])
    head = [h if h != "Почему в поле зрения" else "Что настораживает" for h in head]
    x = _xlsx("Аудит сети ВСП · отделения в фокусе",
              [("Отделения", head, data, {"Что настораживает", "Адрес"}), ("Случаи", head3, data3, {"Слова клиента"}),
               ("Сотрудники", head4, data4, set()), ("Волны пятёрок", head2, data2, set())])
    return _xlsx_response(x, "ВСП_отделения_в_фокусе.xlsx")


@router.get("/export/reviews.xlsx")
def export_reviews(point: Optional[str] = None, flag: Optional[str] = None, q: Optional[str] = None,
                   source: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                   ids: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    with engine().connect() as c:
        if ids:
            id_list = [x for x in ids.split(",") if x][:2000]
            rows = list(c.execute(select(reviews).where(reviews.c.review_id.in_(id_list))
                                  .order_by(reviews.c.review_date.desc())).mappings())
        else:
            _, rows = _reviews_query(c, run, point, flag, q, source, since, until, 5000, 0)
    pts = run["points"]
    head = ["Дата", "Площадка", "Оценка", "ВСП", "Город", "Текст", "Подтверждён", "Автор: всего отзывов",
            "В эпизоде", "Помечен площадкой", "Пропал", "Ответ банка", "Ссылка"]
    data = []
    for r in rows:
        x = _review_row(r, run)
        p = pts.get(x["point"]) or {}
        data.append([(r["review_date"].replace(tzinfo=None) if r["review_date"] else None),
                     {"2gis": "2ГИС", "yandex": "Яндекс"}.get(x["source"], x["source"]), x["rating"], p.get("name"),
                     p.get("city"), x["text"], "да" if x["confirmed"] else "", x["author_total"],
                     "да" if x["in_episode"] else "", x["hidden"] or "", "да" if x["gone"] else "",
                     "да" if x["reply"] else "", x["url"]])
    xb = _xlsx("Аудит сети ВСП · отзывы", [("Отзывы", head, data, {"Текст"})])
    return _xlsx_response(xb, "ВСП_отзывы.xlsx")

