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
from sqlalchemy import and_, create_engine, event, func, select

from ..vsp.regions import BY_CODE, subject_name
from ..vsp.storage import (author_groups, authors, branch_stats, branches, episodes, network, reviews,
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
                    cur = dbapi_conn.cursor()
                    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                    cur.close()
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
    ep_rids = set()
    for e in eps:
        if e["level"] == "branch":
            ep_rids.update(e["review_ids"])
    run = {"id": rid, "version": r["version"], "finished_at": r["finished_at"],
           "stats": json.loads(r["stats_json"] or "{}"), "points": pts, "episodes": eps, "network": net,
           "groups": groups, "praisers": auth, "ep_rids": ep_rids}
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
    return {
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
            for x in c.execute(select(source_branches.c.source, source_branches.c.source_branch_id,
                                      source_branches.c.url, source_branches.c.declared_reviews,
                                      source_branches.c.declared_rating, source_branches.c.match_method,
                                      source_branches.c.raw_json).where(
                    source_branches.c.branch_id == p["branch_id"])).mappings():
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
    "episode": "в эпизоде кампании", "oneoff": "автор без других отзывов", "confirmed": "подтверждён операцией",
    "hidden": "помечен площадкой", "gone": "пропал из выдачи", "neg": "1–2★", "five": "5★",
    "reply": "с ответом банка", "names": "упомянут сотрудник",
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
    x = _xlsx("Аудит сети ВСП · кандидаты в проверку",
              [("Кандидаты", head, data, {"Почему в поле зрения", "Адрес"}), ("Эпизоды кампаний", head2, data2, set())])
    return _xlsx_response(x, "ВСП_кандидаты_в_проверку.xlsx")


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

