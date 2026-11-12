"""Раздел «Ожидание» вкладки «Аудит сети ВСП»: жалобы клиентов на очереди и долгое ожидание в отделениях.

Данные — этап чтения «ожидание» сборщика (vsp/signals/wait.py): каждую жалобу со словами об очереди читает
модель и отвечает, жалоба ли это на ожидание в отделении, сколько клиент ждал по его словам, причина, ушёл ли.
Раздел считает по ним вживую (кэш на десять минут, обновление в фоне), без ночного расчёта вкладки: новая
жалоба видна через час после сбора.

Что показывает: сколько жалоб и в скольких отделениях, какая это доля жалоб на отделения, причины и
длительность по словам клиентов, динамика по месяцам, отделения (с отметкой «значимо чаще сети»), ГОСБ,
карта регионов и сами жалобы с дословной цитатой. Маршруты добавляются к маршрутам вкладки.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends

from ..vsp.regions import BY_CODE, subject_name
from ..vsp.signals import wait as W
from ..vsp.signals.stats import bh, binom_tail
from . import vsp_api as V
from .auth import CurrentUser, get_current_user

log = logging.getLogger(__name__)
UTC = timezone.utc
router = V.router

CACHE_S = 600
PERIODS = {"90": 90, "180": 180, "365": 365, "730": 730}
PERIOD_WORDS = {"90": "За три месяца", "180": "За полгода", "365": "За год", "730": "За два года"}
TERR_FEW = 60             # меньше жалоб на территории за период — долю не сравниваем (штриховка на карте)
LONG_MIN = 60             # «час и дольше»
BACKLOG = 50              # столько непрочитанных жалоб — уже «модель дочитывает прошлые годы»
BUCKETS = [("lt30", "меньше получаса", 1, 29), ("30", "от получаса до часа", 30, 59),
           ("60", "от часа до двух", 60, 119), ("120", "два часа и дольше", 120, 10 ** 6)]

_C: dict = {"d": None, "at": 0.0, "busy": False, "tables": {}}
_LOCK = threading.Lock()


def _load() -> dict:
    d = W.load(V.engine())
    for h in d["hits"]:
        h["m"] = W.ym(h["at"])
        h["excerpt"] = W.excerpt(h["text"], h["quote"])
    return d


def data() -> dict:
    """Жалобы на ожидание и знаменатель. Первый запрос ждёт загрузку; дальше раз в CACHE_S данные
    перечитываются в фоне, а пока читаются — отдаются прежние."""
    now = time.monotonic()
    d = _C["d"]
    if d is not None and now - _C["at"] < CACHE_S:
        return d
    if d is not None:
        if not _C["busy"]:
            _C["busy"] = True
            _C["at"] = now

            def work():
                try:
                    fresh = _load()
                    _C.update(d=fresh, at=time.monotonic(), tables={})
                except Exception:  # noqa: BLE001 — остаёмся на прежних данных
                    log.warning("vsp: жалобы на ожидание не перечитаны", exc_info=True)
                finally:
                    _C["busy"] = False

            threading.Thread(target=work, name="vsp-wait-refresh", daemon=True).start()
        return d
    with _LOCK:
        if _C["d"] is None:
            _C.update(d=_load(), at=time.monotonic(), tables={})
        return _C["d"]


def reset() -> None:
    _C.update(d=None, at=0.0, busy=False, tables={})


def _m0(per: str | None) -> str | None:
    return W.month_from(datetime.now(UTC), PERIODS[per]) if per in PERIODS else None


def _table(d: dict, m0: str | None) -> dict:
    """Таблица отделений по всей сети за период — одна на период, фильтры территории накладываются сверху."""
    t = _C["tables"].get(m0)
    if t is None:
        t = W.branch_table(d["hits"], d["neg"], m0=m0)
        t["by_point"] = {r["point"]: r for r in t["rows"]}
        t["neg_pt"] = W.neg_by_point(d["neg"], m0)
        _C["tables"][m0] = t
    return t


def _pts() -> dict:
    try:
        return V._run()["points"]
    except Exception:  # noqa: BLE001 — расчёта вкладки ещё нет: жалобы покажем без адресов
        return {}


def _in_scope(p: dict | None, subject, gosb) -> bool:
    p = p or {}
    return not ((subject and p.get("subject") != subject) or (gosb and p.get("gosb") != gosb))


def _filter(d: dict, pts: dict, *, m0, subject=None, gosb=None, point=None, cause=None, long=False, left=False,
            q=None) -> list[dict]:
    ql = (q or "").strip().lower()
    out = []
    for h in d["hits"]:
        if m0 is not None and h["m"] < m0:
            continue
        if point and h["point"] != point:
            continue
        if (subject or gosb) and not _in_scope(pts.get(h["point"]), subject, gosb):
            continue
        if (cause and h["cause"] != cause) or (long and (h["minutes"] or 0) < LONG_MIN) or (left and not h["left"]):
            continue
        if ql and ql not in h["text"].lower():
            continue
        out.append(h)
    return out


def _neg_scope(t: dict, pts: dict, subject, gosb) -> int:
    if not subject and not gosb:
        return t["neg"]
    return sum(n for p, n in t["neg_pt"].items() if _in_scope(pts.get(p), subject, gosb))


def _minutes_label(m: int | None) -> str:
    if not m:
        return ""
    if m < 60:
        return f"{m} мин"
    h, r = divmod(m, 60)
    return f"{h} ч {r} мин" if r else f"{h} ч"


def _item(h: dict, pts: dict, full: bool = False) -> dict:
    o = {"rid": h["rid"], "point": h["point"], "date": h["at"].date().isoformat(), "rating": h["rating"],
         "src": h["src"], "url": V._safe_url(h.get("url")), "minutes": h["minutes"],
         "minutes_label": _minutes_label(h["minutes"]), "cause": h["cause"], "cause_label": W.CAUSES[h["cause"]],
         "left": h["left"], "quote": h["excerpt"], "place": V._place(pts.get(h["point"]))}
    if full:
        o["text"] = h["text"][:6000]
    return o


def _row(r: dict, pts: dict) -> dict:
    return {**V._place(pts.get(r["point"])), "point": r["point"], "n": r["n"], "neg": r["neg"],
            "share": round(r["share"], 3) if r["share"] is not None else None,
            "ratio": round(r["ratio"], 2) if r["ratio"] is not None else None, "sig": r["sig"],
            "long": r["long"], "left": r["left"], "median": r["median"], "median_label": _minutes_label(r["median"]),
            "last": r["last"].date().isoformat() if r["last"] else None,
            "cause": r["cause"], "cause_label": W.CAUSES.get(r["cause"] or "", "")}


def _terr(hits: list[dict], t: dict, pts: dict, key: str, *, share: float) -> dict[str, dict]:
    """Жалобы на ожидание и все жалобы по территориям (регион или ГОСБ); «значимо чаще» — биномиальный тест
    против доли остальной сети с поправкой на число территорий."""
    k: Counter = Counter()
    n: Counter = Counter()
    npts: dict[str, set] = defaultdict(set)
    for h in hits:
        code = (pts.get(h["point"]) or {}).get(key)
        if code:
            k[code] += 1
            npts[code].add(h["point"])
    for p, v in t["neg_pt"].items():
        code = (pts.get(p) or {}).get(key)
        if code:
            n[code] += v
    K, N = sum(k.values()), sum(n.values())
    pv = {}
    for code, kk in k.items():
        nn = max(n[code], kk)
        if nn < TERR_FEW:
            continue
        rest = (K - kk) / (N - nn) if N > nn else share
        pv[code] = binom_tail(kk, nn, rest)
    sig = {c for c in bh(pv, 0.01, m=len(n)) if share and k[c] / max(n[c], k[c]) >= 1.25 * share}
    out = {}
    for code in set(n) | set(k):
        nn = max(n[code], k[code])
        out[code] = {"n": k[code], "neg": nn, "share": round(k[code] / nn, 3) if nn else None,
                     "ratio": round(k[code] / nn / share, 2) if nn and share else None,
                     "few": nn < TERR_FEW, "sig": code in sig, "points": len(npts[code])}
    return out


def _months(d: dict, pts: dict, subject, gosb, n: int = 24) -> list[dict]:
    now = datetime.now(UTC)
    since = W.ym(d["since"])
    k: Counter = Counter()
    for h in d["hits"]:
        if not (subject or gosb) or _in_scope(pts.get(h["point"]), subject, gosb):
            k[h["m"]] += 1
    neg: Counter = Counter()
    for p, ms in d["neg"].items():
        if not (subject or gosb) or _in_scope(pts.get(p), subject, gosb):
            neg.update(ms)
    out = []
    for i in range(n - 1, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        key = f"{y}-{m:02d}"
        if key < since:
            continue
        out.append({"month": key, "n": k.get(key, 0), "neg": max(neg.get(key, 0), k.get(key, 0)),
                    "share": round(k[key] / max(neg[key], k[key]), 3) if k.get(key) else 0.0,
                    "part": key == W.ym(now)})
    return out


def _share_between(d: dict, pts: dict, subject, gosb, a: str, b: str) -> float | None:
    """Доля жалоб на ожидание среди жалоб за месяцы [a, b)."""
    k = sum(1 for h in d["hits"] if a <= h["m"] < b
            and (not (subject or gosb) or _in_scope(pts.get(h["point"]), subject, gosb)))
    n = sum(v for p, ms in d["neg"].items() if not (subject or gosb) or _in_scope(pts.get(p), subject, gosb)
            for m, v in ms.items() if a <= m < b)
    return k / max(n, k) if max(n, k) else None


def _num(n: int) -> str:
    return f"{int(n):,}".replace(",", "\u00a0")


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{round(x * 100)}\u00a0%"       # неразрывный пробел: «26 %» не рвётся между строк


def _headline(per: str, k: int, points: int, share: float | None, sig: int, scoped: bool) -> dict:
    lead = PERIOD_WORDS.get(per, "За всё время")
    if not k:
        return {"text": f"{lead} жалоб на долгое ожидание нет" + (" в этой территории" if scoped else ""), "accent": None}
    text = f"{lead} — {_num(k)} {V._pl(k, 'жалоба', 'жалобы', 'жалоб')} на долгое ожидание"
    if share is not None:
        text += f": это {_pct(share)} всех жалоб на отделения"
    else:
        text += f" в {_num(points)} {V._pl(points, 'отделении', 'отделениях', 'отделениях')}"
    accent = (f"в {sig} {V._pl(sig, 'отделении', 'отделениях', 'отделениях')} об ожидании пишут значимо чаще, "
              f"чем по сети") if sig else None
    return {"text": text, "accent": accent}


def summary(*, subject=None, gosb=None, per="365", cause=None, long=False, left=False, top: int = 12) -> dict:
    d, pts = data(), _pts()
    per = per if per in PERIODS else "365"
    m0 = _m0(per)
    t = _table(d, m0)
    base = _filter(d, pts, m0=m0, subject=subject, gosb=gosb)                  # для причин и длительности
    hits = [h for h in base if (not cause or h["cause"] == cause) and (not long or (h["minutes"] or 0) >= LONG_MIN)
            and (not left or h["left"])]
    filtered = bool(cause or long or left)
    neg = _neg_scope(t, pts, subject, gosb)
    share = len(base) / max(neg, len(base)) if max(neg, len(base)) else None
    mins = sorted(h["minutes"] for h in hits if h["minutes"])
    by_pt = Counter(h["point"] for h in hits)
    in_scope_rows = [r for r in t["rows"] if r["point"] in by_pt]
    sig_rows = [r for r in in_scope_rows if r["sig"]]
    prev = None
    if per == "365" and d["waiting"] <= BACKLOG:             # пока прошлые годы дочитываются, сравнивать не с чем
        prev = _share_between(d, pts, subject, gosb, W.month_from(datetime.now(UTC), 730), m0)
    causes = Counter(h["cause"] for h in base)
    buckets = [{"key": key, "label": label, "n": sum(1 for h in base if lo <= (h["minutes"] or 0) <= hi)}
               for key, label, lo, hi in BUCKETS]
    buckets.append({"key": "none", "label": "время не названо", "n": sum(1 for h in base if not h["minutes"])})
    # строки отделений: при фильтре причины или длительности — счёт по отфильтрованным, остальное — из таблицы
    rows = []
    for r in in_scope_rows:
        o = _row(r, pts)
        if filtered:
            o["n_all"], o["n"] = o["n"], by_pt[r["point"]]
        rows.append(o)
    rows.sort(key=lambda r: (-r["n"], -(r["share"] or 0)))
    terr_s = _terr(base, t, pts, "subject", share=t["share"])
    terr_g = _terr(base, t, pts, "gosb", share=t["share"])
    grid = {}
    try:
        grid = V._run()["network"].get("tile_grid") or {}
    except Exception:  # noqa: BLE001
        pass
    regions = []
    for code, (col, row) in grid.items():
        c = terr_s.get(code) or {"n": 0, "neg": 0, "share": None, "ratio": None, "few": True, "sig": False,
                                 "points": 0}
        regions.append({"code": code, "name": subject_name(code), "label": BY_CODE[code][2] if code in BY_CODE else code,
                        "col": col, "row": row, **c, "points": c["points"] or (1 if c["neg"] else 0)})
    gos_subj: dict[str, Counter] = defaultdict(Counter)
    for p in pts.values():
        if p.get("gosb"):
            gos_subj[p["gosb"]][p.get("subject") or ""] += 1
    gosbs = []
    for g, c in terr_g.items():
        subj = gos_subj[g].most_common(1)[0][0] if gos_subj.get(g) else ""
        if subject and subj != subject:
            continue
        gosbs.append({"gosb": g, "subject": subj, "subject_name": subject_name(subj) if subj else None, **c})
    gosbs.sort(key=lambda g: (not g["sig"], g["few"], -(g["ratio"] or 0), -g["n"]))
    return {"per": per, "since_month": m0, "kpi": {
                "n": len(hits), "points": len(by_pt), "neg": neg, "share": round(share, 3) if share is not None else None,
                "net_share": round(t["share"], 3), "share_prev": round(prev, 3) if prev is not None else None,
                "long": sum(1 for h in hits if (h["minutes"] or 0) >= LONG_MIN), "left": sum(1 for h in hits if h["left"]),
                "timed": len(mins), "median": mins[len(mins) // 2] if mins else None,
                "median_label": _minutes_label(mins[len(mins) // 2]) if mins else "",
                "sig": len(sig_rows), "tested": t["tested"]},
            "headline": _headline(per, len(hits), len(by_pt), share if not filtered else None, len(sig_rows),
                                  bool(subject or gosb)),
            "filtered": filtered, "scoped": bool(subject or gosb),
            "months": _months(d, pts, subject, gosb),
            "causes": [{"key": k_, "label": W.CAUSES[k_], "n": n_} for k_, n_ in causes.most_common()],
            "durations": buckets,
            "branches": rows[:top], "branches_total": len(rows),
            "regions": regions, "gosbs": gosbs[:40], "few_below": TERR_FEW,
            "progress": {"read": d["read"], "waiting": d["waiting"], "backlog": d["waiting"] > BACKLOG,
                         "since": d["since"].date().isoformat(),
                         "loaded_at": d["loaded_at"].isoformat()}}


BRANCH_SORT = {"n", "share", "long", "left", "last"}


def branch_rows(*, subject=None, gosb=None, per="365", cause=None, long=False, left=False, sort="n") -> list[dict]:
    d, pts = data(), _pts()
    m0 = _m0(per if per in PERIODS else "365")
    t = _table(d, m0)
    hits = _filter(d, pts, m0=m0, subject=subject, gosb=gosb, cause=cause, long=long, left=left)
    by_pt = Counter(h["point"] for h in hits)
    filtered = bool(cause or long or left)
    rows = []
    for r in t["rows"]:
        if r["point"] not in by_pt:
            continue
        o = _row(r, pts)
        if filtered:
            o["n_all"], o["n"] = o["n"], by_pt[r["point"]]
        rows.append(o)
    key = {"n": lambda r: (-r["n"], -(r["share"] or 0)),
           "share": lambda r: (-(r["share"] or 0) if r["n"] >= W.SIG_MIN else 1, -r["n"]),
           "long": lambda r: (-r["long"], -r["n"]), "left": lambda r: (-r["left"], -r["n"]),
           "last": lambda r: (r["last"] or "", r["n"])}[sort if sort in BRANCH_SORT else "n"]
    rows.sort(key=key, reverse=sort == "last")
    return rows


def point_brief(point: str) -> dict | None:
    """Блок «Долгое ожидание» в досье отделения: за год, против сети, последние жалобы."""
    d, pts = data(), _pts()
    m0 = _m0("365")
    t = _table(d, m0)
    hits = [h for h in d["hits"] if h["point"] == point]
    if not hits:
        return {"n": 0, "n_all": 0, "net_share": round(t["share"], 3), "items": []}
    r = t["by_point"].get(point)
    year = [h for h in hits if h["m"] >= m0]
    return {"n": len(year), "n_all": len(hits), "neg": r["neg"] if r else t["neg_pt"].get(point, 0),
            "share": round(r["share"], 3) if r and r["share"] is not None else None,
            "ratio": round(r["ratio"], 2) if r and r["ratio"] is not None else None,
            "net_share": round(t["share"], 3), "sig": bool(r and r["sig"]),
            "long": r["long"] if r else 0, "left": r["left"] if r else 0,
            "median_label": _minutes_label(r["median"]) if r else "",
            "cause_label": W.CAUSES.get((r or {}).get("cause") or "", ""),
            "items": [_item(h, pts) for h in hits[:3]]}


# ── маршруты ─────────────────────────────────────────────────────────────────

@router.get("/wait")
def wait_summary(subject: Optional[str] = None, gosb: Optional[str] = None, per: str = "365",
                 cause: Optional[str] = None, long: bool = False, left: bool = False,
                 user: CurrentUser = Depends(get_current_user)):
    V._need(user)
    return summary(subject=subject or None, gosb=gosb or None, per=per,
                   cause=cause if cause in W.CAUSES else None, long=long, left=left)


@router.get("/wait/list")
def wait_list(subject: Optional[str] = None, gosb: Optional[str] = None, point: Optional[str] = None,
              per: str = "365", cause: Optional[str] = None, long: bool = False, left: bool = False,
              q: Optional[str] = None, sort: str = "new", limit: int = 30, offset: int = 0,
              user: CurrentUser = Depends(get_current_user)):
    """Жалобы на ожидание: свежие первыми или по времени ожидания. per=all — за всё прочитанное время."""
    V._need(user)
    d, pts = data(), _pts()
    hits = _filter(d, pts, m0=_m0(per), subject=subject or None, gosb=gosb or None, point=point or None,
                   cause=cause if cause in W.CAUSES else None, long=long, left=left, q=q)
    if sort == "long":
        hits = sorted(hits, key=lambda h: (-(h["minutes"] or 0), -h["at"].timestamp()))
    off = max(0, offset)
    return {"total": len(hits), "points": len({h["point"] for h in hits}),
            "items": [_item(h, pts, full=True) for h in hits[off:off + max(1, min(limit, 200))]]}


@router.get("/wait/branches")
def wait_branches(subject: Optional[str] = None, gosb: Optional[str] = None, per: str = "365",
                  cause: Optional[str] = None, long: bool = False, left: bool = False, sort: str = "n",
                  limit: int = 50, offset: int = 0, user: CurrentUser = Depends(get_current_user)):
    V._need(user)
    rows = branch_rows(subject=subject or None, gosb=gosb or None, per=per,
                       cause=cause if cause in W.CAUSES else None, long=long, left=left, sort=sort)
    off = max(0, offset)
    return {"total": len(rows), "sig": sum(1 for r in rows if r["sig"]),
            "items": rows[off:off + max(1, min(limit, 500))]}


@router.get("/export/wait.xlsx")
def export_wait(subject: Optional[str] = None, gosb: Optional[str] = None, point: Optional[str] = None,
                per: str = "365", cause: Optional[str] = None, long: bool = False, left: bool = False,
                q: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    V._need(user)
    d, pts = data(), _pts()
    kw = dict(subject=subject or None, gosb=gosb or None, cause=cause if cause in W.CAUSES else None,
              long=long, left=left)
    hits = _filter(d, pts, m0=_m0(per), point=point or None, q=q, **kw)
    cap = 20000
    head = ["Дата", "Город", "Адрес", "ВСП", "ГОСБ", "Регион", "Ждал, мин (по словам клиента)", "Причина",
            "Ушёл, не дождавшись", "Слова клиента", "Оценка", "Площадка", "Отзыв целиком", "Ссылка"]
    rows = []
    for h in hits[:cap]:
        p = pts.get(h["point"]) or {}
        rows.append([h["at"].date().isoformat(), p.get("city"), p.get("address"), p.get("name"), p.get("gosb"),
                     subject_name(p["subject"]) if p.get("subject") else "", h["minutes"] or "",
                     W.CAUSES[h["cause"]], "да" if h["left"] else "", h["excerpt"], h["rating"],
                     "2ГИС" if h["src"] == "2gis" else "Яндекс Карты" if h["src"] == "yandex" else h["src"],
                     h["text"][:3000], V._safe_url(h.get("url")) or ""])
    bhead = ["Город", "Адрес", "ВСП", "ГОСБ", "Регион", "Жалоб на ожидание", "Жалоб всего", "Доля", "К доле по сети, раз",
             "Значимо чаще сети", "Ждали час и дольше", "Ушли, не дождавшись", "Обычно ждали, мин", "Чаще всего причина",
             "Последняя жалоба"]
    brows = [[r.get("city"), r.get("address"), r.get("name"), r.get("gosb"),
              subject_name(r["subject"]) if r.get("subject") else "", r["n"], r["neg"],
              f"{round(r['share'] * 100)} %" if r["share"] is not None else "", r["ratio"] or "",
              "да" if r["sig"] else "", r["long"], r["left"], r["median"] or "", r["cause_label"], r["last"]]
             for r in branch_rows(per=per, **kw)[:cap]] if not point else []
    title = "Аудит сети ВСП · долгое ожидание"
    if len(hits) > cap:
        title += f" · первые {cap:,} из {len(hits):,}".replace(",", " ")
    sheets = [("Жалобы на ожидание", head, rows, {"Слова клиента", "Отзыв целиком", "Адрес", "Ссылка"})]
    if brows:
        sheets.append(("Отделения", bhead, brows, {"Адрес"}))
    return V._xlsx_response(V._xlsx(title, sheets), "ВСП_ожидание.xlsx")
