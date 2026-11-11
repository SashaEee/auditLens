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
from ..vsp.signals.kinds import TYPED_SINCE
from ..vsp.signals.risks import BY_KEY as RISK, TYPES as RISK_TYPES
from ..vsp.storage import (author_groups, authors, branch_stats, branches, cases, episodes, network, persons, reviews,
                           signal_runs, source_branches, sync_state)
from .auth import CurrentUser, get_current_user
from . import telemetry

log = logging.getLogger(__name__)
router = APIRouter()
UTC = timezone.utc


# ── доступ и подключение ───────────────────────────────────────────────────────

# Бета-доступ: владелец (ADMIN_USERS) — всегда; остальные — из VSP_USERS или из базы (app_setting,
# ключ vsp_users: {"users": [логины]}). База — чтобы открыть раздел коллеге без пересоздания контейнера.
_BETA: dict = {"at": -1e9, "v": []}


def beta_users() -> list[str]:
    now = time.monotonic()
    if now - _BETA["at"] < 60:
        return _BETA["v"]
    v: list[str] = []
    try:
        from sqlalchemy import text as _t
        from .. import db
        with db.session() as s_:
            raw = s_.execute(_t("SELECT value FROM app_setting WHERE key = 'vsp_users'")).scalar()
        if isinstance(raw, str):
            raw = json.loads(raw)
        v = [str(u).strip() for u in ((raw or {}).get("users") or []) if str(u).strip()]
    except Exception:  # noqa: BLE001 — нет таблицы или базы: только владелец и VSP_USERS
        v = []
    _BETA.update(at=now, v=v)
    return v


def set_beta_users(users: list[str], by: str | None) -> list[str]:
    from sqlalchemy import text as _t
    from .. import db
    clean = sorted({str(u).strip() for u in users if str(u).strip()})
    with db.session() as s_:
        s_.execute(_t("""INSERT INTO app_setting (key, value, updated_at, updated_by)
                         VALUES ('vsp_users', CAST(:v AS jsonb), now(), :by)
                         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(),
                                                         updated_by = EXCLUDED.updated_by"""),
                   {"v": json.dumps({"users": clean}, ensure_ascii=False), "by": by})
        s_.commit()
    _BETA.update(at=-1e9)
    return clean


def can_vsp(username: str | None) -> bool:
    if not username:
        return False
    if telemetry.is_admin(username):
        return True
    allow = {u.strip() for u in os.getenv("VSP_USERS", "").split(",") if u.strip()}
    return username in allow or username in beta_users()


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
        {"where": "ВСП", "text": "Просят ли клиентов оставить отзыв в картах и как: планшет или QR у стойки, "
                                      "просьба после операции, «поставьте пять»"},
        {"where": "ВСП", "text": "Кто из сотрудников работал в дни эпизода: смены, графики, замены"},
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


class BetaIn(BaseModel):
    users: list[str]


@router.get("/beta")
def beta_get(user: CurrentUser = Depends(get_current_user)):
    if not telemetry.is_admin(user.username):
        raise HTTPException(403, "только владельцу")
    return {"users": beta_users()}


@router.post("/beta")
def beta_set(body: BetaIn, user: CurrentUser = Depends(get_current_user)):
    if not telemetry.is_admin(user.username):
        raise HTTPException(403, "только владельцу")
    return {"users": set_beta_users(body.users, user.username)}


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
        {"where": "ВСП", "text": "Документы по операции: заявления, согласия, подписи клиента; запись камер, если срок хранения позволяет"},
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


# ── вкладка: истории, волны и очаги способов действия, полки отделений ────────

def _place(p: dict | None) -> dict:
    p = p or {}
    return {k: p.get(k) for k in ("point", "name", "city", "address", "gosb", "subject", "subject_name", "formats",
                                  "n_12m", "status", "hq")}


# ── истории клиентов: общий источник для разделов, слоёв и выгрузок ────────────

PERIODS = {"90": 90, "180": 180, "365": 365, "730": 730}
SEVERITY = {3: "очень тяжёлая", 2: "тяжёлая", 1: "лёгкая"}
SHELF_LABEL = {"sure": "Выделяются наверняка", "pattern": "Виден один почерк"}
STATUS_LABEL = {"new": "новое", "rising": "растёт", "steady": "идёт давно", "fading": "затихает", "gone": "ушло"}


def _aware(d):
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    if isinstance(d, datetime) and d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d


def _kinds(run) -> dict[int, dict]:
    """Справочник типов историй: из расчёта (каким он был в ту ночь), иначе — из кода."""
    if "_kinds" not in run:
        from ..vsp.signals.kinds import KINDS
        raw = run["network"].get("kinds") or {}
        run["_kinds"] = {int(k): v for k, v in raw.items()} if raw else {k: dict(v) for k, v in KINDS.items()}
    return run["_kinds"]


def _kind_name(run, k) -> str | None:
    try:
        return (_kinds(run).get(int(k)) or {}).get("name") if k else None
    except (TypeError, ValueError):
        return None


def _wave_id(key: str) -> str:
    """Ключ ряда «k:5» / «m:m0503» → адрес «k5» / «m0503»."""
    return "k" + key[2:] if key.startswith("k:") else key[2:]


def _wave_key(wid: str | None) -> str | None:
    wid = (wid or "").strip()
    if wid.startswith("k") and wid[1:].isdigit():
        return f"k:{int(wid[1:])}"
    if wid.startswith("m") and len(wid) > 1:
        return f"m:{wid}"
    return None


def _stories(run) -> list[dict]:
    """Все истории прогона, свежие первыми. Ключ сети `stories`; у прогона до него — из досье точек."""
    from ..vsp.signals.kinds import BACKGROUND
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
    motifs = run["network"].get("motifs") or {}
    for x in out:
        x["_at"] = _aware(x.get("at")) or datetime(2000, 1, 1, tzinfo=UTC)
        x["actions"] = [a for a in x.get("actions") or [] if a in ACTIONS] or ["другое"]
        x["labels"] = [ACTIONS[a]["label"] for a in x["actions"]]
        x.setdefault("quote_ok", True)
        x["_named"] = _named(x)                    # один раз на прогон: регулярка дорогая
        x["kind_name"] = _kind_name(run, x.get("kind"))
        m = x.get("motif")
        real = bool(m) and not str(m).endswith("00")
        x["motif_name"] = (motifs.get(m) or {}).get("name") if real else None
        keys = []
        if x.get("kind") and x["kind"] not in BACKGROUND and x["kind"] != 25:
            keys.append(f"k:{x['kind']}")
        if real:
            keys.append(f"m:{m}")
        x["_keys"] = keys
        x["_client"] = x.get("client") or x.get("author") or x["rid"]
    out.sort(key=lambda x: x["_at"], reverse=True)
    run["_stories"] = out
    run["_story_by_rid"] = {x["rid"]: x for x in out}
    return out


def _story(run, rid: str) -> dict | None:
    _stories(run)
    return run["_story_by_rid"].get(rid)


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
        vulnerable=False, named=False, money=False, q=None, fmt=None, kind=None, key=None, inside=False,
        uniq=False, here=False) -> list[dict]:
    """Истории по фильтрам. period — ключ PERIODS (дни) или пусто (всё время); key — ряд волны
    («k:5», «m:m0503»); uniq — по одной истории на клиента (без копий); here — без историй «не про это отделение»."""
    cut = datetime.now(UTC) - timedelta(days=PERIODS[period]) if period in PERIODS else None
    ql = (q or "").strip().lower()
    pts = run["points"]
    kind = int(kind) if kind not in (None, "", 0, "0") and str(kind).isdigit() else None
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
        if (kind and x.get("kind") != kind) or (key and key not in x["_keys"]) or (inside and not x.get("inside")):
            continue
        if (uniq and (x.get("copy_of") or x.get("dup_of"))) or (here and (x.get("elsewhere") or x.get("hq"))):
            continue
        if fmt and not _fmt_ok(pts.get(x["point"]) or {}, fmt):
            continue
        if named and not x["_named"]:
            continue
        if ql and ql not in " ".join(str(x.get(k) or "") for k in ("quote", "episode", "employee", "scheme", "label",
                                                                   "kind_name", "motif_name")).lower():
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
    if x.get("moved_from"):
        o["moved_place"] = _place(pts.get(x["moved_from"]))
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


def _kind_facets(run, xs: list[dict]) -> list[dict]:
    """Типы историй в выборке: для фильтра «Что произошло» и подписей в досье."""
    c: Counter = Counter()
    pts: dict[int, set] = defaultdict(set)
    for x in xs:
        k = x.get("kind")
        if k:
            c[k] += 1
            pts[k].add(x["point"])
    kinds = _kinds(run)
    return [{"kind": k, "name": (kinds.get(k) or {}).get("name") or str(k), "inside": bool((kinds.get(k) or {}).get("inside")),
             "n": n, "points": len(pts[k])} for k, n in c.most_common()]


def _story_kw(subject, gosb, point, action, severity, period, vulnerable, named, money, q, fmt,
              kind=None, key=None, inside=False) -> dict:
    return dict(subject=subject or None, gosb=gosb or None, point=point or None, action=action or None,
                severity=int(severity or 0), period=period or None, vulnerable=bool(vulnerable), named=bool(named),
                money=bool(money), q=q or None, fmt=fmt or None, kind=kind or None, key=_wave_key(key),
                inside=bool(inside))


def _pl(n: int, one: str, few: str, many: str) -> str:
    m = abs(int(n)) % 100
    return many if 11 <= m <= 14 else one if m % 10 == 1 else few if 2 <= m % 10 <= 4 else many


def _x(v) -> str:
    """Число для текста: запятая вместо точки, без лишнего нуля."""
    if v is None:
        return "—"
    s = f"{v:.1f}" if isinstance(v, float) and abs(v) < 100 and v != int(v) else str(int(round(v)))
    return s.replace(".", ",")


MONTHS_G = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
            "декабря"]
MONTHS_N = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
            "декабрь"]


def _period(a, b) -> str:
    """«с марта по сентябрь 2025» / «с ноября 2024 по май 2025» / «в мае 2025»."""
    a, b = _aware(a), _aware(b)
    if not a or not b:
        return ""
    if (a.year, a.month) == (b.year, b.month):
        return f"в {['январе', 'феврале', 'марте', 'апреле', 'мае', 'июне', 'июле', 'августе', 'сентябре', 'октябре', 'ноябре', 'декабре'][a.month - 1]} {a.year}"
    if a.year == b.year:
        return f"с {MONTHS_G[a.month - 1]} по {MONTHS_N[b.month - 1]} {b.year}"
    return f"с {MONTHS_G[a.month - 1]} {a.year} по {MONTHS_N[b.month - 1]} {b.year}"


# ── волны и очаги ─────────────────────────────────────────────────────────────

def _waves(run) -> list[dict]:
    if "_waves" not in run:
        hot: Counter = Counter()
        for h in run["network"].get("hotspots") or []:
            hot.update(_hot_keys(h))
        out = []
        for w in run["network"].get("waves") or []:
            out.append({**w, "id": _wave_id(w["key"]), "status_label": STATUS_LABEL.get(w.get("status"), ""),
                        "hot": hot.get(w["key"], 0),
                        "inside_kind": bool((_kinds(run).get(w.get("kind")) or {}).get("inside"))})
        run["_waves"] = out
        run["_wave_by_key"] = {w["key"]: w for w in out}
    return run["_waves"]


def _wave(run, key: str | None) -> dict | None:
    _waves(run)
    return run["_wave_by_key"].get(key) if key else None


def _wave_text(w: dict) -> dict:
    """Заголовок волны словами: что именно и чем подтверждается. Текст собирает код, числа — из расчёта."""
    clause = w.get("clause") or ""
    name = w["name"]
    what = f"клиенты пишут, что {clause}" if (clause and w["level"] == "kind") else name[0].lower() + name[1:]
    y, p = w.get("year") or 0, w.get("prev_year") or 0
    cl = lambda n: f"{n} {_pl(n, 'клиент', 'клиента', 'клиентов')}"  # noqa: E731
    if w["status"] == "new":
        head, acc = f"Новое: {what}", f"за год {cl(y)}, раньше почти не встречалось"
    elif w["status"] == "rising" and w.get("horizon") == "quarter":
        head = f"Растёт: {what}"
        acc = f"за три месяца {cl(w.get('recent') or 0)} при ожидании {_x(w.get('recent_expected'))}"
    elif w["status"] == "rising":
        head, acc = f"Растёт: {what}", f"за год {cl(y)}, годом раньше — {p}"
    elif w["status"] == "fading":
        head, acc = f"Затихает: {what}", f"за год {cl(y)}, в {w.get('peak_year')} году было чаще"
    elif w["status"] == "gone":
        head, acc = f"Ушло: {what}", "за последний год — ни одной истории"
    else:
        head, acc = f"Идёт давно: {what}", f"за год {cl(y)}, годом раньше — {p}"
    return {"text": head, "accent": acc}


def _hot_keys(h: dict) -> set[str]:
    """Способы действия, к которым относится очаг: его ряд и главный почерк (если очаг найден по типу)."""
    mm = (h.get("main_motif") or {}).get("id")
    return {h["key"], f"m:{mm}"} if mm else {h["key"]}


def _hot_motif(h: dict) -> str | None:
    return h.get("motif") or (h.get("main_motif") or {}).get("id")


def _hots(run) -> list[dict]:
    if "_hots" not in run:
        pts = run["points"]
        out = []
        for h in run["network"].get("hotspots") or []:
            w = _wave(run, h["key"]) or {}
            n, k = h["clients"], len(h["points"])
            where = (f"ГОСБ {h['terr']}" if h["level"] == "gosb" else h["terr"])
            # очаг за всю историю — не вспышка, а устойчивое превышение: так и говорим
            when = f"с {TYPED_SINCE[:4]} года" if h.get("all_time") else _period(h["from"], h["to"])
            out.append({**{f: v for f, v in h.items() if f != "rids"}, "wave_id": _wave_id(h["key"]),
                        "subject_name": subject_name(h["subject"]) if h.get("subject") else None,
                        "where": where, "period": when,
                        "wave_status": w.get("status"),
                        "text": (f"{where}: {n} {_pl(n, 'клиент', 'клиента', 'клиентов')} в {k} "
                                 f"{_pl(k, 'отделении', 'отделениях', 'отделениях')} {when} — "
                                 f"{h['name'][0].lower() + h['name'][1:]}"),
                        "note": f"по частоте остальной сети за то же время ожидалось {_x(h['expected'])}",
                        "places": [{**_place(pts.get(x["point"])), "point": x["point"], "n": x["n"]} for x in h["points"]],
                        # свежий — последняя история не старше полутора лет: старый очаг — справка, а не повод
                        "recent": _aware(h["to"]) >= datetime.now(UTC) - timedelta(days=548),
                        "_rids": h["rids"]})
        out.sort(key=lambda h: (not h["recent"], h["strength"] != "strong", h["p"], -h["clients"]))
        run["_hots"] = out
        run["_hot_by_id"] = {h["id"]: h for h in out}
        by_pt: dict[str, list[dict]] = defaultdict(list)
        for h in out:
            for x in h["points"]:
                by_pt[x["point"]].append(h)
        run["_hot_by_point"] = by_pt
    return run["_hots"]


def _hot_brief(h: dict) -> dict:
    return {k: v for k, v in h.items() if not k.startswith("_") and k != "places"} | {"points_n": len(h["points"])}


def _req_period(a: dict) -> str:
    """Период запроса по отделению: окно причины (почерка), иначе — от первой до последней тяжёлой истории."""
    x, y = a.get("req_from") or a.get("first"), a.get("req_to") or a.get("last")
    return f"с {_aware(x).strftime('%d.%m.%Y')} по {_aware(y).strftime('%d.%m.%Y')}" if x and y else ""


def _request_text(run, kind, *, where: str, period: str, motif: str | None = None) -> dict | None:
    """Запрос данных банка: что, где, за какой период, какой показатель снимет вопрос.
    У почерка — свои операции и показатель (их предложила модель-редактор), иначе — по типу истории."""
    k = _kinds(run).get(int(kind)) if kind else None
    m = (run["network"].get("motifs") or {}).get(motif) if motif else None
    ops = ((m or {}).get("ops") or (k or {}).get("ops") or "").strip()
    if not ops:
        return None
    metric = (m or {}).get("metric") or (k or {}).get("metric") or ""
    return {"kind": int(kind) if kind else None, "name": (m or {}).get("name") or (k or {}).get("name"),
            "what": ops[0].upper() + ops[1:], "where": where, "period": period, "metric": metric,
            "benign": (m or {}).get("benign") or (k or {}).get("benign") or "",
            "text": (f"Прошу предоставить: {ops} — {where}, {period}, в разрезе сотрудника и окна. "
                     + (f"Показатель для сравнения: {metric}. " if metric else "")
                     + "Основание: повторяющиеся отзывы клиентов; это слова клиентов, а не установленные факты.")}


# ── полки отделений ───────────────────────────────────────────────────────────

def _row_out(run, p: dict) -> dict:
    a = p.get("risk") or {}
    con = a.get("conclusion") or {}
    _hots(run)
    return {**_place(p), "shelf": a.get("shelf"), "shelf_label": SHELF_LABEL.get(a.get("shelf")),
            "why": a.get("why") or [], "sig": a.get("sig") or [], "stat": a.get("stat"),
            "patterns": [{k: v for k, v in x.items() if k != "rids"} for x in a.get("patterns") or []],
            "quote": a.get("quote"), "benign": a.get("benign"), "red": a.get("red"), "severity": a.get("severity"),
            "heavy_clients": a.get("heavy_clients"), "heavy_2y": a.get("heavy_2y"), "heavy_5y": a.get("heavy_5y"),
            "read": a.get("read"), "vol_2y": a.get("vol_2y"), "first": a.get("first"), "last": a.get("last"),
            "people": [{**pp, "name": _nom(pp.get("name"))} for pp in a.get("people") or []],
            "lead": a.get("lead") or {}, "flags": a.get("flags") or {}, "kinds": a.get("kinds") or [],
            "action": a.get("action_label"),
            "conclusion": ({"pattern": bool(con.get("pattern")), "pattern_name": con.get("pattern_name"),
                            "confidence": con.get("confidence"), "wave_only": bool(con.get("wave_only"))} if con else None),
            "hot": [{"id": h["id"], "where": h["where"], "name": h["name"]} for h in run["_hot_by_point"].get(p["point"], [])]}


def _listed(run, subject=None, gosb=None, only: set | None = None, shelf: str | None = None) -> list[dict]:
    from ..vsp.signals.risk import order_key
    rows = []
    for p in run["points"].values():
        a = p.get("risk")
        if not a or not a.get("include"):
            continue
        if (subject and p.get("subject") != subject) or (gosb and p.get("gosb") != gosb) or \
                (only is not None and p["point"] not in only) or (shelf and a.get("shelf") != shelf):
            continue
        rows.append((order_key(a), p))
    rows.sort(key=lambda t: t[0])
    return [_row_out(run, p) for _, p in rows]


def _queue(run, *, subject=None, gosb=None, kind=None, inside=False, vulnerable=False, days: int | None = None) -> list[dict]:
    """Третья полка — отдельные тяжёлые случаи за год: очередь случаев, а не отделений."""
    pts = run["points"]
    kind = int(kind) if kind and str(kind).isdigit() else None
    cut = datetime.now(UTC) - timedelta(days=days) if days else None
    out = []
    for q in run["network"].get("case_queue") or []:
        x = _story(run, q["rid"])
        if not x:
            continue
        if (subject and x.get("subject") != subject) or (gosb and x.get("gosb") != gosb) or \
                (kind and x.get("kind") != kind) or (inside and not x.get("inside")) or \
                (vulnerable and not x.get("vulnerable")) or (cut and x["_at"] < cut):
            continue
        out.append({**_story_out(x, pts), "score": q["score"], "why": q.get("why") or []})
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


_WATCH: dict = {"at": 0.0, "v": None}


def _watch() -> dict | None:
    """Сторож чтения моделью (кэш 2 минуты): всё ли подходящее прочитано, не отстало ли чтение."""
    now = time.monotonic()
    if _WATCH["v"] is not None and now - _WATCH["at"] < 120:
        return _WATCH["v"]
    from ..vsp.storage import kv
    from ..vsp.watch import llm_watch
    try:
        with engine().connect() as c:
            raw = dict(c.execute(select(kv.c.key, kv.c.value).where(
                kv.c.key.in_(["llm:last", "llm:at", "llm:clean_at", "llm:spent", "failed:llm"]))).all())

        def kv_get(k):
            try:
                return json.loads(raw[k]) if raw.get(k) is not None else None
            except (TypeError, ValueError):
                return raw.get(k)
        v = llm_watch(kv_get)
    except Exception:  # noqa: BLE001 — сторож не должен ронять вкладку
        log.exception("vsp: сторож чтения не посчитан")
        v = None
    _WATCH.update(at=now, v=v)
    return v


@router.get("/meta")
def meta_(user: CurrentUser = Depends(get_current_user)):
    """Лёгкое: плитки карты (регионы для фильтра), дата расчёта, ход сбора и чтения, счётчики разделов."""
    _need(user)
    run = _run()
    try:
        prog = _progress()
    except Exception:  # noqa: BLE001 — полоса прогресса не должна ронять вкладку
        log.exception("vsp: прогресс не посчитан")
        prog = None
    owner = telemetry.is_admin(user.username)
    waves = _waves(run)
    shelves = Counter((p.get("risk") or {}).get("shelf") for p in run["points"].values()
                      if (p.get("risk") or {}).get("include"))
    _notify_waves(run)
    return {"run": {"id": run["id"], "finished_at": _iso(run["finished_at"])}, "tiles": _tiles(run),
            "source": run["network"].get("story_source"), "progress": prog, "watch": _watch(),
            "nav": {"up": sum(1 for w in waves if w["status"] in ("new", "rising")),
                    "hot": sum(1 for h in _hots(run) if h["strength"] == "strong" and h["recent"]),
                    "listed": sum(shelves.values()), "sure": shelves.get("sure", 0), "pattern": shelves.get("pattern", 0),
                    "cases": len(run["network"].get("case_queue") or [])},
            "kinds": [{"kind": k, "name": v.get("name"), "inside": bool(v.get("inside"))}
                      for k, v in sorted(_kinds(run).items())],
            "typed_since": TYPED_SINCE[:4],
            "access": {"owner": owner, "beta": len(beta_users()) if owner else None}}


MAP_FEW = 300            # меньше прочитанных отзывов за период — цвет не показываем (штриховка)


@router.get("/map")
def map_(subject: Optional[str] = None, key: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    """Карта «больше ожидаемого» — по всем тяжёлым историям или по выбранному способу действия (key —
    адрес волны). Клиенты за 2 года по регионам и ГОСБ против ожидания: прочитанные отзывы территории ×
    частота по остальной сети, отдельно по площадкам и жалобам; красным — значимо с поправкой на число сравнений."""
    _need(user)
    run = _run()
    wkey = _wave_key(key) or "all"
    cache = run.setdefault("_map", {})
    if (subject, wkey) in cache:
        return cache[(subject, wkey)]
    net = run["network"]
    terr = (net.get("terr") or {}).get(wkey) or {}
    grid = net.get("tile_grid") or {}
    reg: dict[str, Counter] = defaultdict(Counter)
    gos: dict[str, Counter] = defaultdict(Counter)
    for p in run["points"].values():
        for bucket, k in ((reg, p.get("subject")), (gos, p.get("gosb"))):
            if not k or (bucket is gos and subject and p.get("subject") != subject):
                continue
            bucket[k]["points"] += 1
            bucket[k]["with_data"] += (p.get("n_12m") or 0) > 0
            bucket[k]["listed"] += bool((p.get("risk") or {}).get("include"))
            bucket[k]["subject:" + (p.get("subject") or "")] += 1

    def cell(row: dict | None) -> dict:
        row = row or {}
        return {"n": row.get("n", 0), "branches": row.get("points", 0), "expected": row.get("expected", 0.0),
                "read": row.get("read", 0), "ratio": row.get("ratio"), "few": bool(row.get("few", True)),
                "sig": bool(row.get("sig"))}

    regions = []
    for code, c in reg.items():
        col, row = grid.get(code, (None, None))
        regions.append({"code": code, "name": subject_name(code), "label": BY_CODE[code][2] if code in BY_CODE else code,
                        "col": col, "row": row, "points": c["points"], "with_data": c["with_data"],
                        "listed": c["listed"], **cell((terr.get("subject") or {}).get(code))})
    for code, (col, row) in grid.items():          # регионы без отделений — пустые плитки
        if code not in reg:
            regions.append({"code": code, "name": subject_name(code), "label": BY_CODE[code][2] if code in BY_CODE else code,
                            "col": col, "row": row, "points": 0, "with_data": 0, "listed": 0, **cell(None)})
    gosbs = []
    for g, c in gos.items():
        subj = max((k for k in c if k.startswith("subject:")), key=lambda k: c[k])[8:]
        gosbs.append({"gosb": g, "subject": subj, "subject_name": subject_name(subj) if subj else None,
                      "points": c["points"], "listed": c["listed"], **cell((terr.get("gosb") or {}).get(g))})
    gosbs.sort(key=lambda g: (not g["sig"], -(g["ratio"] or 0), -g["n"]))
    w = _wave(run, wkey) if wkey != "all" else None
    keys = [{"id": _wave_id(k), "name": (_wave(run, k) or {}).get("name") or k, "clients": v.get("clients", 0),
             "level": "motif" if k.startswith("m:") else "kind"}
            for k, v in (net.get("terr") or {}).items() if k != "all" and _wave(run, k)]
    keys.sort(key=lambda x: (x["level"] != "motif", -x["clients"]))
    out = {"key": key if w else None, "name": w["name"] if w else "Все тяжёлые истории", "clients": terr.get("clients", 0),
           "period_days": 730, "few_below": MAP_FEW, "regions": regions, "gosbs": gosbs, "keys": keys,
           "points_total": len(run["points"]),
           "listed_total": sum(1 for p in run["points"].values() if (p.get("risk") or {}).get("include"))}
    cache[(subject, wkey)] = out
    return out


@router.get("/home")
def home(subject: Optional[str] = None, gosb: Optional[str] = None, kind: Optional[int] = None,
         vulnerable: bool = False, fresh: bool = False, fmt: Optional[str] = None,
         shelf: Optional[str] = None, limit: Optional[int] = None, offset: int = 0,
         user: CurrentUser = Depends(get_current_user)):
    """Полки отделений «Где проверить». shelf — «sure» (выделяются наверняка) или «pattern» (виден один почерк);
    limit/offset — постранично (без limit — весь список). Третья полка — случаи — в /queue."""
    _need(user)
    run = _run()
    filtered = bool(kind or vulnerable or fresh or fmt)
    only = None
    if filtered:
        only = {x["point"] for x in _sf(run, subject=subject, gosb=gosb, kind=kind, vulnerable=vulnerable, fmt=fmt,
                                         severity=2, period="90" if fresh else None)}
    lst = _listed(run, subject, gosb, only)
    scope = _listed(run, subject)
    gosbs = Counter(x["gosb"] for x in scope if x["gosb"])
    shelves = dict(Counter(x["shelf"] for x in lst))
    shelves["cases"] = len(_queue(run, subject=subject, gosb=gosb, kind=kind, vulnerable=vulnerable,
                                  days=90 if fresh else None))
    page = [x for x in lst if not shelf or x["shelf"] == shelf]
    list_total = len(page)
    if limit is not None:
        page = page[max(0, offset):max(0, offset) + max(1, min(int(limit), 500))]
    pts = run["points"].values()
    return {"run": {"id": run["id"], "finished_at": _iso(run["finished_at"])},
            "source": run["network"].get("story_source"), "list": page, "list_total": list_total,
            "total": sum(1 for p in pts if (p.get("risk") or {}).get("include")), "in_scope": len(scope),
            "filtered": filtered, "shelves": shelves,
            "tested": sum(1 for p in pts if (p.get("risk") or {}).get("read")),
            "gosbs": [{"gosb": g, "n": n} for g, n in gosbs.most_common()],
            "with_data": sum(1 for p in pts if (p.get("n_12m") or 0) > 0), "points": len(run["points"])}


@router.get("/queue")
def queue(subject: Optional[str] = None, gosb: Optional[str] = None, kind: Optional[int] = None,
          inside: bool = False, vulnerable: bool = False, fresh: bool = False, limit: int = 30, offset: int = 0,
          user: CurrentUser = Depends(get_current_user)):
    """Отдельные тяжёлые случаи за год — по важности для аудита: сначала поступок сотрудника (действие за
    клиента, личная выгода), потом обстоятельства (сумма, пожилой клиент, обращение в полицию)."""
    _need(user)
    run = _run()
    xs = _queue(run, subject=subject, gosb=gosb, kind=kind, inside=inside, vulnerable=vulnerable,
                days=90 if fresh else None)
    off = max(0, offset)
    return {"total": len(xs), "inside": sum(1 for x in xs if x.get("inside")),
            "items": _attach_src(xs[off:off + max(1, min(limit, 200))])}


# ── «Схемы»: волны способов действия и очаги ─────────────────────────────────

def _wave_brief(w: dict) -> dict:
    return {k: v for k, v in w.items() if k != "description"} | _wave_text(w)


@router.get("/waves")
def waves(level: Optional[str] = None, status: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    """Что идёт по сети: каждый способ действия — тип истории и узкий почерк внутри типа — с рядом по месяцам
    и состоянием (новое, растёт, идёт давно, затихает, ушло)."""
    _need(user)
    run = _run()
    ws = _waves(run)
    has_motifs = any(w["level"] == "motif" for w in ws)
    rows = [w for w in ws if (not level or w["level"] == level) and (not status or w["status"] == status)]
    return {"waves": [_wave_brief(w) for w in rows], "has_motifs": has_motifs,
            "counts": {lv: dict(Counter(w["status"] for w in ws if w["level"] == lv)) for lv in ("kind", "motif")},
            "since": TYPED_SINCE[:4], "source": run["network"].get("story_source")}


def _examples(run, xs: list[dict], n: int = 12) -> list[dict]:
    """Примеры для карточки: сначала важные для аудита, по одной истории на отделение."""
    from ..vsp.signals.risk import case_score
    now = datetime.now(UTC)
    pts = run["points"]

    def sc(x):
        return case_score({**x, "at": x["_at"]}, now)

    ranked = sorted(xs, key=lambda x: (-sc(x)[0], -x["_at"].timestamp()))
    out, seen = [], set()
    for x in ranked:
        if x["point"] in seen or not x.get("quote_ok", True):
            continue
        seen.add(x["point"])
        out.append({**_story_out(x, pts), "why": sc(x)[1][:4]})
        if len(out) >= n:
            break
    return _attach_src(out)


def _profile(xs: list[dict]) -> dict:
    """Кто пострадал, кому выгода и чем закончилось — по уточнённым историям (одна на клиента)."""
    det = [x for x in xs if x.get("detailed")]
    n = len(xs)
    money = [float(x["money"]) for x in xs if isinstance(x.get("money"), (int, float)) and 0 < x["money"] < MONEY_CAP
             and x.get("sum_kind") in (None, "потеря")]
    return {"n": n, "detailed": len(det), "vulnerable": sum(1 for x in xs if x.get("vulnerable")),
            "very": sum(1 for x in xs if (x.get("severity") or 0) >= 3),
            "named": sum(1 for x in xs if x["_named"]), "inside": sum(1 for x in xs if x.get("inside")),
            "benefit": [{"label": k, "n": v} for k, v in Counter(x.get("benefit") or "неясно" for x in det).most_common()],
            "systemic": sum(1 for x in det if x.get("systemic")),
            "police": sum(1 for x in det if set(x.get("escalation") or []) & {"полиция", "суд", "прокуратура", "ЦБ"}),
            "outcome": [{"label": k, "n": v} for k, v in Counter(x.get("outcome") or "неизвестно" for x in det).most_common(5)],
            "money_n": len(money), "money_sum": round(sum(money)), "money_median": (sorted(money)[len(money) // 2] if money else None)}


@router.get("/waves/{wid}")
def wave_card(wid: str, user: CurrentUser = Depends(get_current_user)):
    """Карточка способа действия — «за пять минут»: что это, как меняется, где сгущается, какие отделения
    смотреть, что пишут клиенты и что запросить из данных банка."""
    _need(user)
    run = _run()
    key = _wave_key(wid)
    w = _wave(run, key)
    if not w:
        raise HTTPException(404, "способ действия не найден")
    cache = run.setdefault("_wave_cards", {})
    if key in cache:
        return cache[key]
    since = datetime.fromisoformat(TYPED_SINCE).replace(tzinfo=UTC)
    xs = [x for x in _sf(run, key=key, severity=2, uniq=True) if x["_at"] >= since]
    here = [x for x in xs if not x.get("elsewhere") and not x.get("hq")]
    pts = run["points"]
    by_pt: dict[str, list[dict]] = defaultdict(list)
    for x in here:
        by_pt[x["point"]].append(x)
    branches = sorted(({**_place(pts.get(p)), "point": p, "n": len(ss), "last": ss[0]["date"],
                        "shelf": ((pts.get(p) or {}).get("risk") or {}).get("shelf")}
                       for p, ss in by_pt.items() if len(ss) >= 2 and p in pts), key=lambda r: (-r["n"], r["last"]))[:15]
    hot = [_hot_brief(h) for h in _hots(run) if key in _hot_keys(h)]
    kinfo = _kinds(run).get(w.get("kind")) or {}
    motif = (run["network"].get("motifs") or {}).get(w.get("motif")) or {}
    siblings = [{"id": o["id"], "name": o["name"], "status": o["status"], "status_label": o["status_label"],
                 "year": o["year"], "clients": o["clients"]}
                for o in _waves(run) if o["kind"] == w["kind"] and o["key"] != key and o["level"] == "motif"]
    parent = _wave(run, f"k:{w['kind']}") if w["level"] == "motif" else None
    year = [x for x in xs if x["_at"] >= datetime.now(UTC) - timedelta(days=365)]
    out = {**_wave_brief(w), "description": w.get("description") or motif.get("description") or "",
           "kind_name": kinfo.get("name"), "inside_kind": bool(kinfo.get("inside")),
           "ops": motif.get("ops") or kinfo.get("ops") or "", "metric": motif.get("metric") or kinfo.get("metric") or "",
           "benign": motif.get("benign") or kinfo.get("benign") or "",
           "hotspots": hot, "branches": branches, "branches_total": sum(1 for ss in by_pt.values() if len(ss) >= 2),
           "siblings": siblings, "parent": ({"id": parent["id"], "name": parent["name"]} if parent else None),
           "profile": _profile(xs), "profile_year": _profile(year),
           "examples": _examples(run, [x for x in here if x["_at"] >= datetime.now(UTC) - timedelta(days=730)] or here),
           "request": _request_text(run, w.get("kind"), where="по отделениям из списка ниже" if branches else "по сети",
                                    period=_period(w["first"], w["last"]), motif=w.get("motif")),
           "since": TYPED_SINCE[:4]}
    cache[key] = out
    return out


@router.get("/hotspots")
def hotspots(subject: Optional[str] = None, key: Optional[str] = None, strength: Optional[str] = None,
             user: CurrentUser = Depends(get_current_user)):
    """Где сгущается: один способ действия в нескольких отделениях одного города или ГОСБ за короткое время."""
    _need(user)
    run = _run()
    wkey = _wave_key(key)
    hs = [h for h in _hots(run) if (not subject or h.get("subject") == subject) and (not wkey or wkey in _hot_keys(h))
          and (not strength or h["strength"] == strength)]
    allh = _hots(run)
    return {"hotspots": [_hot_brief(h) | {"places": h["places"][:8]} for h in hs],
            "counts": dict(Counter(h["strength"] for h in allh)), "total": len(allh)}


@router.get("/hotspots/{hid}")
def hotspot_card(hid: str, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    _hots(run)
    h = run["_hot_by_id"].get(hid)
    if not h:
        raise HTTPException(404, "очаг не найден")
    pts = run["points"]
    xs = [x for x in (_story(run, r) for r in h["_rids"]) if x]
    xs.sort(key=lambda x: x["_at"], reverse=True)
    w = _wave(run, h["key"]) or {}
    mw = _wave(run, f"m:{h['main_motif']['id']}") if h.get("main_motif") else None
    kinfo = _kinds(run).get(h.get("kind")) or {}
    codes = [rk_code(pl_.get("name")) or pl_.get("name") or "" for pl_ in h["places"]]
    where = "отделения " + ", ".join(c for c in codes if c) if any(codes) else h["where"]
    a, b = _aware(h["from"]), _aware(h["to"])
    return {**_hot_brief(h), "places": h["places"],
            "stories": _attach_src([_story_out(x, pts) for x in xs]), "profile": _profile(xs),
            "wave": ({"id": w["id"], "name": w["name"], "status": w["status"], "status_label": w["status_label"],
                      "year": w["year"], "clients": w["clients"], "points": w["points"]} if w else None),
            "main_wave": {"id": mw["id"], "name": mw["name"]} if mw else None,
            "motifs": [{**m, "wave_id": (_wave(run, f"m:{m['id']}") or {}).get("id")} for m in h.get("motifs") or []],
            "benign": ((run["network"].get("motifs") or {}).get(_hot_motif(h)) or {}).get("benign") or kinfo.get("benign") or "",
            "request": _request_text(run, h.get("kind"), where=where, motif=_hot_motif(h),
                                     period=f"с {a.strftime('%d.%m.%Y')} по {b.strftime('%d.%m.%Y')}"),
            "limits": ["Отзывы — слова клиентов, а не установленные факты",
                       "Очаг — повод запросить данные банка по этим отделениям, а не вывод о нарушении",
                       "В каждом отделении очага — один-два клиента: по отдельности они ничем не выделяются"]}


def rk_code(name: str | None) -> str | None:
    from ..vsp.signals.risk import code_key
    return code_key(name)


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
    _hots(run)
    bars = _month_bars(point)
    full = _sf(run, point=point)
    pts = run["points"]
    stories = _attach_src([_story_out(x, pts) for x in full[:300]])
    n12 = p.get("n_12m") or 0
    neg12 = p.get("neg_12m") if p.get("neg_12m") is not None else sum(b["neg"] for b in bars[-12:])
    peer = (run["network"].get("peers") or {}).get(p.get("gosb")) or {}
    compare = [
        {"key": "n_12m", "label": "Отзывов за год", "me": n12, "peer": peer.get("n_12m"), "fmt": "n"},
        {"key": "neg_share", "label": "Доля жалоб (1–2★) за год", "me": round(neg12 / n12, 3) if n12 >= 10 else None,
         "peer": peer.get("neg_share"), "fmt": "p"},
    ]
    pat_rids = {r for x in a.get("patterns") or [] for r in x.get("rids") or []}
    for s_ in stories:
        s_["in_pattern"] = s_["rid"] in pat_rids
    code = rk_code(p.get("name")) or ""
    period = _req_period(a)
    request = []
    for r in a.get("request") or []:
        rt = _request_text(run, r.get("kind"), where=f"отделение {code}" if code else "это отделение", period=period)
        if rt:
            request.append(rt)
    return {**_place(p), "vol_2y": a.get("vol_2y"), "open_date": p.get("open_date"),
            "passport": _passport(p), "lat": p.get("lat"), "lon": p.get("lon"),
            "risk": {k: v for k, v in a.items() if k != "request"}, "request": request,
            "shelf_label": SHELF_LABEL.get(a.get("shelf")),
            "hot": [_hot_brief(h) for h in run["_hot_by_point"].get(point, [])],
            "stories": stories, "bars": bars, "kinds": _kind_facets(run, full),
            "compare": compare, "peer_points": peer.get("points"),
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
                 fmt: Optional[str] = None, kind: Optional[int] = None, key: Optional[str] = None,
                 inside: bool = False, sort: str = "new", limit: int = 50, offset: int = 0,
                 user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    kw = _story_kw(subject, gosb, point, action, severity, period, vulnerable, named, money, q, fmt, kind, key, inside)
    xs = _sf(run, **kw)
    if sort == "severe":
        xs = sorted(xs, key=lambda x: (-(x.get("severity") or 0), -x["_at"].timestamp()))
    elif sort == "important":
        from ..vsp.signals.risk import case_score
        now = datetime.now(UTC)
        xs = sorted(xs, key=lambda x: (-case_score({**x, "at": x["_at"]}, now)[0], -x["_at"].timestamp()))
    base = _sf(run, **{**kw, "kind": None}) if kind else xs
    pts = run["points"]
    w = _wave(run, kw["key"]) if kw["key"] else None
    return {"total": len(xs), "points": len({x["point"] for x in xs}),
            "clients": len({x["_client"] for x in xs}),
            "heavy": sum(1 for x in xs if (x.get("severity") or 0) >= 2),
            "items": _attach_src([_story_out(x, pts) for x in xs[offset:offset + min(max(limit, 1), 200)]]),
            "kinds": _kind_facets(run, base), "months": _months(xs),
            "wave": ({"id": w["id"], "name": w["name"]} if w else None),
            "source": run["network"].get("story_source")}


@router.get("/export/stories.xlsx")
def export_stories(subject: Optional[str] = None, gosb: Optional[str] = None, point: Optional[str] = None,
                   action: Optional[str] = None, severity: int = 0, period: Optional[str] = "730",
                   vulnerable: bool = False, named: bool = False, money: bool = False, q: Optional[str] = None,
                   fmt: Optional[str] = None, kind: Optional[int] = None, key: Optional[str] = None,
                   inside: bool = False, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    xs = _sf(run, **_story_kw(subject, gosb, point, action, severity, period, vulnerable, named, money, q, fmt,
                              kind, key, inside))
    pts = run["points"]
    head = ["Дата", "Город", "Адрес", "ВСП", "ГОСБ", "Регион", "Что произошло", "Способ (почерк)", "Тяжесть",
            "Кому выгода, по словам клиента", "Сотрудник", "Пожилой или уязвимый клиент", "Сумма, ₽",
            "Слова клиента", "Пересказ ИИ", "Примечание", "Оценка", "Отзыв"]
    data = []
    cap = 20000
    for x in xs[:cap]:
        pl_ = pts.get(x["point"]) or {}
        note = "; ".join(t for t in (
            "копия отзыва того же клиента" if x.get("copy_of") else "",
            f"тот же текст ещё у {x['dup_points']} отделений" if x.get("dup_points") else "",
            "клиент пишет о другом отделении" if x.get("elsewhere") else "",
            "перенесено из карточки другого отделения по коду в тексте" if x.get("moved_from") else "") if t)
        data.append([x.get("date"), pl_.get("city"), pl_.get("address"), pl_.get("name"), x.get("gosb"),
                     subject_name(x["subject"]) if x.get("subject") else "", x.get("kind_name") or ", ".join(x["labels"]),
                     x.get("motif_name") or x.get("label") or "",
                     SEVERITY.get(x.get("severity") or 0, ""), x.get("benefit") or "", x.get("employee") or "",
                     "да" if x.get("vulnerable") else "", x.get("money") or "",
                     x.get("quote") if x.get("quote_ok", True) else "",
                     "; ".join(t for t in ((x.get("episode") or ""), ("" if x.get("quote_ok", True) else
                                                                      f"не дословно: {x.get('quote')}")) if t),
                     note, x.get("rating"), x["rid"]])
    title = "Аудит сети ВСП · истории клиентов"
    if len(xs) > cap:
        title += f" · первые {cap:,} из {len(xs):,}".replace(",", " ")
    xb = _xlsx(title, [("Истории клиентов", head, data, {"Слова клиента", "Пересказ ИИ", "Адрес", "Что произошло",
                                                        "Способ (почерк)", "Примечание"})])
    return _xlsx_response(xb, "ВСП_истории.xlsx")


# ── таблица отделений: все отделения с историями ─────────────────────────────

TABLE_SORT = {"heavy", "clients", "per100", "last", "vulnerable", "inside"}


@router.get("/table")
def table(subject: Optional[str] = None, gosb: Optional[str] = None, kind: Optional[int] = None,
          period: Optional[str] = "730", vulnerable: bool = False, fmt: Optional[str] = None,
          sort: str = "clients", limit: int = 300, offset: int = 0, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    xs = _sf(run, period=period, subject=subject, gosb=gosb, kind=kind, vulnerable=vulnerable, fmt=fmt, here=True)
    by: dict[str, list[dict]] = defaultdict(list)
    for x in xs:
        by[x["point"]].append(x)
    pts = run["points"]
    rows = []
    for pt, ss in by.items():
        P = pts.get(pt) or {}
        a = P.get("risk") or {}
        heavy = [x for x in ss if (x.get("severity") or 0) >= 2]
        clients = {x["_client"] for x in heavy if not x.get("dup_of")}
        read = a.get("read") or 0
        kinds = Counter(x["kind_name"] for x in heavy if x.get("kind_name"))
        rows.append({**_place(P), "stories": len(ss), "heavy": len(heavy), "very": sum(1 for x in ss if (x.get("severity") or 0) >= 3),
                     "clients": len(clients),
                     "per100": round(100 * (a.get("heavy_clients") or 0) / read, 2) if read >= 30 else None, "read": read or None,
                     "last": ss[0]["date"], "last_at": ss[0]["at"],
                     "vulnerable": sum(1 for x in heavy if x.get("vulnerable")),
                     "inside": sum(1 for x in heavy if x.get("inside")),
                     "kinds": [k for k, _ in kinds.most_common(2)],
                     "shelf": a.get("shelf"), "shelf_label": SHELF_LABEL.get(a.get("shelf"))})
    key = sort if sort in TABLE_SORT else "clients"
    if key == "last":
        rows.sort(key=lambda r: r["last_at"] or "", reverse=True)
    else:
        rows.sort(key=lambda r: (-(r.get(key) or 0), -r["clients"], -r["stories"]))
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

INTENT_LABEL = {0: "ошибка или недоразумение", 1: "неясно", 2: "ввели в заблуждение или надавили",
                3: "действие за клиента или присвоение"}


@router.get("/evidence/{rid:path}")
def evidence(rid: str, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    from ..vsp.signals.llm_read import DETAIL_VERSION, PROMPT_VERSION, SCREEN_VERSION, quote_ok, review_llm
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
    story = _story(run, rid)
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
    pts = run["points"]
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
    if story and story.get("copy_of"):
        checks.append({"ok": None, "label": "Тот же клиент уже описал этот случай в другом отзыве об этом отделении — "
                                            "в подсчётах он один"})
    if story and story.get("dup_points"):
        n = story["dup_points"]
        checks.append({"ok": None, "label": f"Этот же текст оставлен ещё у {n} {_pl(n, 'отделения', 'отделений', 'отделений')} — "
                                            "в волнах и очагах клиент считается один раз"})
    if story and story.get("moved_from"):
        src_pl = pts.get(story["moved_from"]) or {}
        checks.append({"ok": None, "label": "Отзыв оставлен на карточке другого отделения"
                       + (f" ({src_pl.get('city') or ''}, {src_pl.get('name') or ''})" if src_pl else "")
                       + ", но клиент назвал код этого — история отнесена сюда"})
    if story and story.get("elsewhere"):
        checks.append({"ok": None, "label": "По словам клиента, это произошло в другом отделении — в счёт этого "
                                            "отделения история не идёт, в волнах по сети учитывается"})
    if story and story.get("hq"):
        checks.append({"ok": None, "label": "Карточка центрального офиса: сюда пишут жалобы на банк в целом — "
                                            "в сравнении отделений не участвует"})
    model = None
    if data:
        model = {"what": data.get("episode") or (card or {}).get("summary"), "scheme": data.get("scheme_free"),
                 "actions": [ACTIONS[a]["label"] for a in (data.get("actions") or []) if a in ACTIONS],
                 "products": data.get("products") or [], "severity": SEVERITY.get(int(data["severity"]) if str(data.get("severity") or "").isdigit() else 0, ""),
                 "fault": data.get("fault"), "employee": emp or None, "money": data.get("money_rub"),
                 "vulnerable": bool(data.get("vulnerability")), "relevant": bool(data.get("relevant")),
                 "model": (card or {}).get("model")}
    detail = None
    dcard = cards_.get(DETAIL_VERSION)
    if dcard is not None and story:
        intent = story.get("intent")
        detail = {"kind": story.get("kind_name"), "kind2": _kind_name(run, story.get("kind2")),
                  "motif": story.get("motif_name"), "label": story.get("label"), "benefit": story.get("benefit"),
                  "intent": INTENT_LABEL.get(intent) if isinstance(intent, int) else None,
                  "systemic": story.get("systemic") or [], "outcome": story.get("outcome"),
                  "escalation": story.get("escalation") or [], "window": story.get("window"),
                  "sum_kind": story.get("sum_kind"), "event_date": story.get("event_date"),
                  "inside": bool(story.get("inside")), "model": dcard["model"]}
    screen = cards_.get(SCREEN_VERSION)
    pt = (story or {}).get("point") or row.get("point")
    waves_ = [{"id": _wave_id(k), "name": (_wave(run, k) or {}).get("name"),
               "status_label": (_wave(run, k) or {}).get("status_label")}
              for k in ((story or {}).get("_keys") or []) if _wave(run, k)]
    return {"review": row, "quote": quote, "employee": emp or None,
            "story": _story_out(story, pts) if story else None, "model": model, "detail": detail, "waves": waves_,
            "screen": {"relevant": bool(screen["relevant"]), "why": screen["summary"]} if screen is not None else None,
            "checks": checks, "place": _place(pts.get(pt)), "point": pt}


# ── «Главное»: что идёт → где сгущается → кого смотреть → что запросить ─────────
MONEY_CAP = 50_000_000          # больше — почти наверняка ошибка чтения суммы


def _money(x) -> float:
    m = x.get("money")
    return float(m) if isinstance(m, (int, float)) and 0 < m < MONEY_CAP else 0.0


@router.get("/brief")
def brief(subject: Optional[str] = None, gosb: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    cache = run.setdefault("_brief", {})
    if (subject, gosb) not in cache:              # считаем раз на прогон
        cache[(subject, gosb)] = _brief(run, subject, gosb)
    return {**cache[(subject, gosb)], "watch": _watch()}


def _brief(run, subject, gosb) -> dict:
    pts = run["points"]
    now = datetime.now(UTC)
    y1 = now - timedelta(days=365)
    scoped = bool(subject or gosb)
    ws = _waves(run)
    has_motifs = any(w["level"] == "motif" for w in ws)

    # 1. что идёт: новое и растущее (почерк раньше своего типа), затем самое частое из «идёт давно»
    def rank(w):
        return ({"new": 0, "rising": 1}.get(w["status"], 2), w["level"] != "motif", -(w.get("year_ratio") or 0), -w["year"])
    up = sorted((w for w in ws if w["status"] in ("new", "rising")), key=rank)
    up_kinds = {w["kind"] for w in up if w["level"] == "motif"}
    up = [w for w in up if not (w["level"] == "kind" and w["kind"] in up_kinds)]      # тип повторяет свой почерк
    rest = sorted((w for w in ws if w["status"] == "steady" and w["level"] == ("motif" if has_motifs else "kind")),
                  key=lambda w: -w["year"])
    waves_top = (up + rest)[:6]
    # 2. где сгущается
    hs = [h for h in _hots(run) if (not subject or h.get("subject") == subject)
          and (not gosb or h.get("gosb") == gosb or any(pl_.get("gosb") == gosb for pl_ in h["places"]))]
    # 3. кого смотреть
    lst = _listed(run, subject, gosb)
    shelves = Counter(x["shelf"] for x in lst)
    q = _queue(run, subject=subject, gosb=gosb)
    fresh = [x for x in q if _aware(x["at"]) >= now - timedelta(days=90)]
    inside = [x for x in q if x.get("inside")]
    # цифры по охвату
    xs = _sf(run, subject=subject, gosb=gosb, severity=2, period="730", uniq=True, here=True)
    year = [x for x in xs if x["_at"] >= y1]
    prev = [x for x in xs if x["_at"] < y1]
    money = [m for x in year if (m := _money(x)) and x.get("sum_kind") in (None, "потеря")]
    det = [x for x in year if x.get("detailed")]
    benefit = Counter(x.get("benefit") or "неясно" for x in det)
    # 4. заголовок: волна по сети; в регионе — его очаг; иначе — счёт
    lead_w = None if scoped else (up[0] if up else None)
    lead_h = hs[0] if (hs and hs[0]["strength"] == "strong" and hs[0]["recent"] and (scoped or not lead_w)) else None
    hero, same, same_total, head, request = None, [], 0, None, None
    if lead_w:
        head = {**_wave_text(lead_w), "kind": "wave", "wave_id": lead_w["id"], "name": lead_w["name"]}
        ex = _examples(run, [x for x in _sf(run, key=lead_w["key"], severity=2, uniq=True, here=True, period="365")], 4)
        same_total = lead_w["year"]
        request = _request_text(run, lead_w.get("kind"), where="по сети, в разрезе отделений",
                                period=_period(y1, now), motif=lead_w.get("motif"))
    elif lead_h:
        head = {"text": lead_h["text"], "accent": lead_h["note"], "kind": "hot", "hot_id": lead_h["id"],
                "name": lead_h["name"]}
        ex = _examples(run, [x for x in (_story(run, r) for r in lead_h["_rids"]) if x], 4)
        same_total = lead_h["clients"]
        request = _request_text(run, lead_h.get("kind"), where=lead_h["where"], motif=_hot_motif(lead_h),
                                period=f"с {_aware(lead_h['from']).strftime('%d.%m.%Y')} по {_aware(lead_h['to']).strftime('%d.%m.%Y')}")
    else:
        n, p_ = len(year), len({x["point"] for x in year})
        head = {"text": (f"За год — {n} {_pl(n, 'клиент', 'клиента', 'клиентов')} с тяжёлыми историями в {p_} "
                         f"{_pl(p_, 'отделении', 'отделениях', 'отделениях')}"),
                "accent": "ни один способ действия не растёт и нигде не сгущается" if not scoped else "", "kind": "count"}
        ex = _attach_src([dict(x) for x in q[:4]])
    if ex:
        hero, same = ex[0], ex[1:4]
    return {
        "run": {"id": run["id"], "finished_at": _iso(run["finished_at"])}, "source": run["network"].get("story_source"),
        "scoped": scoped, "headline": head, "hero": hero, "same": same, "same_total": same_total,
        "kpi": {"read": (run["stats"] or {}).get("read"), "clients_2y": len(xs), "clients_year": len(year),
                "clients_prev": len(prev), "points_year": len({x["point"] for x in year}),
                "vulnerable_year": sum(1 for x in year if x.get("vulnerable")),
                "very_year": sum(1 for x in year if (x.get("severity") or 0) >= 3),
                "inside_year": sum(1 for x in year if x.get("inside")), "detailed_year": len(det),
                "money_sum": round(sum(money)), "money_n": len(money)},
        "waves": [_wave_brief(w) for w in waves_top], "waves_up": len(up), "waves_total": len(ws),
        "has_motifs": has_motifs,
        "hotspots": [_hot_brief(h) | {"places": h["places"][:6]} for h in hs[:6]],
        "hot_strong": sum(1 for h in hs if h["strength"] == "strong" and h["recent"]), "hot_total": len(hs),
        "shelves": {"sure": shelves.get("sure", 0), "pattern": shelves.get("pattern", 0), "rows": lst[:5],
                    "tested": sum(1 for p in pts.values() if (p.get("risk") or {}).get("read"))},
        "cases": _attach_src([dict(x) for x in q[:6]]),
        "cases_total": len(q), "cases_fresh": len(fresh),
        "inside": _attach_src([dict(x) for x in inside[:4]]),
        "inside_total": len(inside),
        "benefit": [{"label": k, "n": v} for k, v in benefit.most_common()],
        "request": request, "typed_since": TYPED_SINCE[:4],
    }


# ── колокольчик: раннее предупреждение о новой или растущей волне ──────────────
_NOTIFIED: dict = {"run": None}


def _notify_waves(run) -> None:
    """Раз на расчёт: если способ действия стал «новым» или «растёт», а раньше таким не был, — строка в
    колокольчик тем, кому открыт раздел. Письмом не дублируется. Что уже сообщали — в app_setting."""
    if _NOTIFIED["run"] == run["id"]:
        return
    _NOTIFIED["run"] = run["id"]
    ups = [w for w in _waves(run) if w["status"] in ("new", "rising")]

    def work():
        try:
            from sqlalchemy import text as _t
            from .. import db
            from . import notices
            with db.session() as s_:
                raw = s_.execute(_t("SELECT value FROM app_setting WHERE key = 'vsp_waves_seen'")).scalar()
            if isinstance(raw, str):
                raw = json.loads(raw)
            seen = raw or {}
            if seen.get("run") == run["id"]:
                return
            known = set(seen.get("keys") or [])
            fresh = [w for w in ups if w["key"] not in known]
            # почерк важнее своего типа
            motif_kinds = {w["kind"] for w in fresh if w["level"] == "motif"}
            fresh = [w for w in fresh if not (w["level"] == "kind" and w["kind"] in motif_kinds)]
            admins = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}
            users = set(beta_users()) | {u.strip() for u in os.getenv("VSP_USERS", "").split(",") if u.strip()} | admins
            if not known:
                # первый расчёт с волнами: всё «новое» разом. Не засыпаем коллег — три строки и только владельцу;
                # остальным уведомления пойдут со следующих изменений
                fresh, users = fresh[:3], admins
            for w in fresh:
                t = _wave_text(w)
                notices.notify(users, "vsp_wave", link=f"#vsp?s=schemes&scheme={w['id']}",
                               ref={"name": w["name"], "status": w["status"], "accent": t["accent"], "key": w["key"]})
            with db.session() as s_:
                s_.execute(_t("""INSERT INTO app_setting (key, value, updated_at, updated_by)
                                 VALUES ('vsp_waves_seen', CAST(:v AS jsonb), now(), 'vsp')
                                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""),
                           {"v": json.dumps({"run": run["id"], "keys": sorted(w["key"] for w in ups)}, ensure_ascii=False)})
                s_.commit()
        except Exception:  # noqa: BLE001 — уведомление не должно ронять вкладку
            log.warning("vsp: уведомление о волнах не отправлено", exc_info=True)

    threading.Thread(target=work, name="vsp-wave-notice", daemon=True).start()


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


def _stat_line(a: dict) -> str:
    """Строка о числе клиентов против ожидания — словами, без терминов."""
    st = a.get("stat")
    n = a.get("heavy_clients") or 0
    if st and st.get("sig"):
        return (f"{st['clients']} {_pl(st['clients'], 'клиент', 'клиента', 'клиентов')} с {st['what']} за {st['window']} "
                f"при ожидании {_x(st['expected'])} — так много случайно не бывает")
    if st:
        return (f"{st['clients']} {_pl(st['clients'], 'клиент', 'клиента', 'клиентов')} с {st['what']} за {st['window']} "
                f"при ожидании {_x(st['expected'])} — в пределах случайного для отделения с таким числом отзывов")
    return f"{n} {_pl(n, 'клиент', 'клиента', 'клиентов')} с тяжёлыми историями — в пределах случайного" if n else ""


def _conclusion_line(con: dict | None) -> str:
    if not con:
        return ""
    if not con.get("pattern"):
        return "общего почерка нет: жалобы разные" + (f" — {con['why']}" if con.get("why") else "")
    t = f"{con.get('pattern_name') or 'общий почерк'} ({con.get('clients') or 0} кл., {con.get('period') or ''}); уверенность {con.get('confidence')}"
    if con.get("wave_only"):
        t += "; отделение этим не выделяется: столько историй могло набраться и случайно"
    return t


@router.get("/export/justification.xlsx")
def export_justification(points: Optional[str] = None, subject: Optional[str] = None, gosb: Optional[str] = None,
                         kind: Optional[int] = None, vulnerable: bool = False, fresh: bool = False,
                         fmt: Optional[str] = None, shelf: Optional[str] = None,
                         user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    want = set((points or "").split(",")) - {""}
    only = None
    if kind or vulnerable or fresh or fmt:          # как на экране «Где проверить»
        only = {x["point"] for x in _sf(run, subject=subject, gosb=gosb, kind=kind, vulnerable=vulnerable, fmt=fmt,
                                         severity=2, period="90" if fresh else None)}
    rows = [x for x in _listed(run, subject, gosb, only, shelf) if not want or x["point"] in want]
    have = {x["point"] for x in rows}
    rows += [_row_out(run, run["points"][p]) for p in want if p in run["points"] and p not in have]
    head = ["Отделение", "Город", "Адрес", "ГОСБ", "Регион", "Полка", "Почему в списке", "Клиенты против ожидания",
            "Вывод по всем историям (модель)", "В каких очагах", "Цитата клиента", "Дата", "Может быть безобидно",
            "Что запросить", "Что проверить"]
    data, st_rows = [], []
    for x in rows:
        P = run["points"][x["point"]]
        a = P.get("risk") or {}
        b = a.get("benign") or {}
        code = rk_code(P.get("name")) or ""
        period = _req_period(a)
        req = [r_ for r_ in (_request_text(run, r.get("kind"), where=f"отделение {code}" if code else "это отделение",
                                           period=period) for r in a.get("request") or []) if r_]
        data.append([x.get("name"), x.get("city"), x.get("address"), x.get("gosb"), x.get("subject_name"),
                     x.get("shelf_label") or "не в списке", "; ".join(a.get("why") or []), _stat_line(a),
                     _conclusion_line(a.get("conclusion")), "; ".join(f"{h['where']} — {h['name']}" for h in x.get("hot") or []),
                     (a.get("quote") or {}).get("text"), (a.get("quote") or {}).get("date"),
                     (f"{b.get('text')}. Снимет вопрос: {b.get('resolve')}" if b else ""),
                     "; ".join(f"{r_['what']} — {r_['where']}, {r_['period']}" for r_ in req),
                     "; ".join(a.get("check") or [])])
        for s_ in _sf(run, point=x["point"], severity=2)[:60]:
            st_rows.append([x.get("name"), x.get("city"), s_["date"], s_.get("kind_name") or ", ".join(s_["labels"]),
                            {3: "очень тяжёлая", 2: "тяжёлая"}.get(s_["severity"], ""), s_.get("episode") or "",
                            s_["quote"] if s_.get("quote_ok", True) else "", s_["rid"]])
    head2 = ["Отделение", "Город", "Дата", "Что произошло", "Тяжесть", "Пересказ ИИ", "Слова клиента", "Отзыв"]
    xb = _xlsx("Аудит сети ВСП · обоснование", [("Где стоит проверить", head, data,
                                                {"Почему в списке", "Клиенты против ожидания", "Цитата клиента",
                                                 "Вывод по всем историям (модель)", "Может быть безобидно",
                                                 "Что запросить", "Что проверить", "Адрес", "В каких очагах"}),
                                               ("Истории клиентов", head2, st_rows, {"Пересказ ИИ", "Слова клиента",
                                                                                     "Что произошло"})])
    return _xlsx_response(xb, "ВСП_обоснование.xlsx")


def _docx_response(doc, name: str) -> Response:
    from .export_brand import docx_bytes
    return Response(docx_bytes(doc), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/export/visit.docx")
def export_visit(point: Optional[str] = None, points: Optional[str] = None,
                 user: CurrentUser = Depends(get_current_user)):
    _need(user)
    run = _run()
    want = [x for x in ([point] if point else []) + (points or "").split(",") if x]
    want = [x for x in dict.fromkeys(want) if x in run["points"]]
    if not want:
        raise HTTPException(404, "отделение не найдено")
    from .export_brand import WD
    first = run["points"][want[0]]
    title = (f"Лист проверки · {first.get('city') or ''}, {first.get('address') or ''}" if len(want) == 1
             else f"Листы проверки · {len(want)} отделений")
    doc = WD.new(title)
    WD.header_footer(doc, "Аудит сети ВСП · лист проверки")
    for i, pt in enumerate(want):
        if i:
            doc.add_page_break()
        _visit_sheet(doc, run, run["points"][pt])
    return _docx_response(doc, "ВСП_лист_проверки.docx")


def _visit_sheet(doc, run, p: dict) -> None:
    from .export_brand import stamp_line
    a = p.get("risk") or {}
    _hots(run)
    doc.add_heading(f"{p.get('city') or ''}", level=0)
    doc.add_paragraph(f"{p.get('address') or ''} · ВСП {p.get('name') or ''} · ГОСБ {p.get('gosb') or '—'}")
    doc.add_paragraph(f"Черновик — поправьте под проверку. {stamp_line()}")
    doc.add_heading("Почему в списке" if a.get("include") else "Что видно по отзывам", level=1)
    for w in a.get("why") or ["Отделение открыто вручную — оснований из отзывов нет"]:
        doc.add_paragraph(w, style="List Bullet")
    if _stat_line(a):
        doc.add_paragraph(_stat_line(a)[0].upper() + _stat_line(a)[1:] + ".")
    for h in run["_hot_by_point"].get(p["point"], []):
        doc.add_paragraph(f"Входит в очаг: {h['text']}; {h['note']}.", style="List Bullet")
    con = a.get("conclusion")
    if con:
        doc.add_heading("Вывод по всем историям отделения (модель)", level=1)
        doc.add_paragraph(_conclusion_line(con)[0].upper() + _conclusion_line(con)[1:] + ".")
        for label, key in (("Об исполнителях", "who"), ("Кто пострадал", "victims"), ("Суммы, по словам клиентов", "money"),
                           ("Остальные истории", "other"), ("Может объясняться безобидно", "benign")):
            if con.get(key):
                doc.add_paragraph(f"{label}: {con[key]}")
    if a.get("benign"):
        doc.add_paragraph(f"Может быть безобидно: {a['benign']['text']}. Снимет вопрос: {a['benign']['resolve']}.")
    code = rk_code(p.get("name")) or ""
    period = _req_period(a)
    doc.add_heading("Что запросить", level=1)
    for r in a.get("request") or []:
        rt = _request_text(run, r.get("kind"), where=f"отделение {code}" if code else "это отделение", period=period)
        if rt:
            doc.add_paragraph(f"☐ {rt['what']} — {rt['where']}, {rt['period']}, в разрезе сотрудника и окна."
                              + (f" Показатель: {rt['metric']}." if rt.get("metric") else ""))
    for r in (con or {}).get("request") or []:
        doc.add_paragraph(f"☐ {r}")
    doc.add_heading("Что проверить", level=1)
    for t in a.get("check") or []:
        doc.add_paragraph(f"☐ {t}")
    doc.add_heading("Истории клиентов", level=1)
    for s_ in _sf(run, point=p["point"], severity=2)[:15]:
        body = f"«{s_['quote']}»" if s_.get("quote_ok", True) and s_.get("quote") else (s_.get("episode") or "")
        doc.add_paragraph(f"{s_['date']} · {s_.get('kind_name') or ', '.join(s_['labels'])}: {body}", style="List Bullet")
    doc.add_heading("Чего мы не видим", level=1)
    doc.add_paragraph("Отзывы — слова клиентов, а не установленные факты. Отзывы, удалённые площадкой до начала "
                      "сбора, не видны. Имена сотрудников — как написали клиенты.")


@router.get("/export/request.docx")
def export_request(hotspot: Optional[str] = None, wave: Optional[str] = None, point: Optional[str] = None,
                   user: CurrentUser = Depends(get_current_user)):
    """«Запрос данных»: что запросить у банка по очагу, способу действия или отделению — с основанием,
    списком отделений, периодом, показателем, который снимет вопрос, и примерами слов клиентов."""
    _need(user)
    run = _run()
    from .export_brand import WD, stamp_line
    pts = run["points"]
    basis, places, stories, reqs, benign, name = [], [], [], [], "", ""
    if hotspot:
        _hots(run)
        h = run["_hot_by_id"].get(hotspot)
        if not h:
            raise HTTPException(404, "очаг не найден")
        d = hotspot_card(hotspot, user)
        name = f"{h['where']} · {h['name']}"
        basis = [h["text"] + ".", h["note"][0].upper() + h["note"][1:] + "."]
        if h.get("main_motif"):
            mm = h["main_motif"]
            basis.append(f"Чаще всего клиенты описывают один приём — «{mm['name']}»: {mm['n']} из {h['clients']}.")
        places = [pl_ for pl_ in h["places"]]
        stories = d["stories"][:6]
        reqs = [d["request"]] if d.get("request") else []
        benign = d.get("benign") or ""
    elif wave:
        d = wave_card(wave, user)
        name = d["name"]
        basis = [f"{d['text']} — {d['accent']}.",
                 f"С {d['since']} года — {d['clients']} {_pl(d['clients'], 'клиент', 'клиента', 'клиентов')} в {d['points']} "
                 f"{_pl(d['points'], 'отделении', 'отделениях', 'отделениях')}."]
        places = d["branches"]
        stories = d["examples"][:6]
        reqs = [d["request"]] if d.get("request") else []
        benign = d.get("benign") or ""
    elif point and point in pts:
        d = dossier(point, user)
        name = f"{d.get('city') or ''}, {d.get('address') or ''}"
        basis = [w + "." for w in (d["risk"].get("why") or [])] or ["Отделение выбрано вручную."]
        places = [_place(pts[point]) | {"n": d["risk"].get("heavy_clients")}]
        stories = [s_ for s_ in d["stories"] if (s_.get("severity") or 0) >= 2][:6]
        reqs = d.get("request") or []
        benign = ((d["risk"].get("benign") or {}).get("text")) or ""
    else:
        raise HTTPException(404, "не указано, по чему готовить запрос")
    doc = WD.new(f"Запрос данных · {name}")
    WD.header_footer(doc, "Аудит сети ВСП · запрос данных")
    doc.add_heading("Запрос данных", level=0)
    doc.add_paragraph(name)
    doc.add_paragraph(f"Черновик — поправьте под задачу. {stamp_line()}")
    doc.add_heading("Основание", level=1)
    for b in basis:
        doc.add_paragraph(b)
    doc.add_paragraph("Это слова клиентов из открытых отзывов, а не установленные факты.")
    doc.add_heading("Что запросить", level=1)
    for r in reqs:
        doc.add_paragraph(f"☐ {r['what']} — {r['where']}, {r['period']}, в разрезе сотрудника и окна.")
        if r.get("metric"):
            doc.add_paragraph(f"Что подтвердит или снимет вопрос: {r['metric']}.")
    if not reqs:
        doc.add_paragraph("☐ Операции по продуктам, о которых пишут клиенты, за период историй — в разрезе сотрудника и окна.")
    if places:
        doc.add_heading("Отделения", level=1)
        for pl_ in places:
            n = pl_.get("n")
            doc.add_paragraph(f"{pl_.get('city') or ''}, {pl_.get('address') or ''} · ВСП {pl_.get('name') or ''}"
                              f"{' · ГОСБ ' + str(pl_['gosb']) if pl_.get('gosb') else ''}"
                              + (f" — {n} {_pl(n, 'клиент', 'клиента', 'клиентов')}" if n else ""), style="List Bullet")
    if stories:
        doc.add_heading("Что пишут клиенты", level=1)
        for s_ in stories:
            pl_ = s_.get("place") or {}
            body = f"«{s_['quote']}»" if s_.get("quote_ok", True) and s_.get("quote") else (s_.get("episode") or "")
            doc.add_paragraph(f"{s_.get('date')} · {pl_.get('city') or ''}, ВСП {pl_.get('name') or ''}: {body}",
                              style="List Bullet")
    if benign:
        doc.add_heading("Чем это может объясняться безобидно", level=1)
        doc.add_paragraph(benign[0].upper() + benign[1:] + ".")
    return _docx_response(doc, "ВСП_запрос_данных.docx")


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

