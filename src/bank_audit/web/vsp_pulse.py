"""«Пульс» → «Сеть ВСП» (только владелец): кто пользуется разделом «Аудит сети ВСП» и кому он открыт.

Новых событий не заводит — считает по тому, что уже пишется: открытия и уходы со страницы
(page_view / page_leave, page = 'vsp'), запросы раздела к серверу (api_request по адресам
/api/vsp/…: по ним видно, что человек открывал — досье отделения, карточку способа действия,
полный отзыв, выгрузку) и материалы раздела, положенные в аудит-дела.

Доступ к разделу хранится в настройке vsp_users (логины и подразделения штата, см. vsp_api);
здесь — справочник для его выдачи: подразделения со штатом и люди, у которых раздела ещё нет.
"""
from __future__ import annotations

import collections

from sqlalchemy import text

from .. import db
from . import org_reach, telemetry, vsp_api

# что человек делал в разделе: ключ, подпись, адреса (точное совпадение или начало пути)
WHAT: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("main", "Главное", ("/api/vsp/brief",), ()),
    ("schemes", "Схемы", ("/api/vsp/waves", "/api/vsp/hotspots"), ()),
    ("where", "Где проверить", ("/api/vsp/home", "/api/vsp/table"), ()),
    ("stories", "Истории", ("/api/vsp/stories",), ()),
    ("wait", "Ожидание", ("/api/vsp/wait", "/api/vsp/wait/list", "/api/vsp/wait/branches"), ()),
    ("dossier", "Досье отделения", (), ("/api/vsp/dossier/",)),
    ("wave", "Карточка способа действия", (), ("/api/vsp/waves/",)),
    ("hot", "Карточка очага", (), ("/api/vsp/hotspots/",)),
    ("evidence", "Полный отзыв", (), ("/api/vsp/evidence/",)),
    ("export", "Выгрузки", (), ("/api/vsp/export/",)),
]
# «глубина»: человек не просто открыл раздел, а дошёл до конкретного отделения, способа или отзыва
DEEP = ("dossier", "wave", "hot", "evidence", "export", "case")


def what_of(path: str) -> str | None:
    for key, _label, exact, prefix in WHAT:
        if path in exact or any(path.startswith(p) and len(path) > len(p) for p in prefix):
            return key
    return None


def _roster() -> tuple[dict, dict, set]:
    """Штат: по табельному номеру, по ключу «имя фамилия» (только работающие) и коды уволенных."""
    fired = org_reach._fired()
    with db.session() as s:
        rows = [dict(r) for r in s.execute(text(
            "SELECT tab_no, status, unit_code, unit_hint, name_key FROM org_member")).mappings()]
    by_tab = {m["tab_no"]: m for m in rows}
    by_name = collections.defaultdict(list)
    for m in rows:
        if m["name_key"] and m["status"] not in fired:
            by_name[m["name_key"]].append(m)
    return by_tab, by_name, fired


def member_of(username: str, name: str | None, by_tab: dict, by_name: dict) -> dict | None:
    """Запись штата для человека — тем же способом, что в охвате и в доступе к разделу."""
    t = org_reach._TAB_RX.search(username or "")
    if t and t.group(1) in by_tab:
        return by_tab[t.group(1)]
    w = org_reach._words(name)
    if len(w) == 2:
        hit = by_name.get(org_reach.name_key(w[0], w[1])) or []
        if len(hit) == 1:
            return hit[0]
    return None


def report(viewer: str | None, days: int = 30, with_me: bool | None = None) -> dict:
    days = max(1, min(int(days or 30), 365))
    ex = set(telemetry.excluded(viewer, with_me))
    p = {"days": days}
    since = telemetry._SINCE
    try:
        by_tab, by_name, fired = _roster()
    except Exception:  # noqa: BLE001 — штат не загружен: считаем без подразделений
        by_tab, by_name, fired = {}, {}, set()
    with db.session() as s:
        users = s.execute(text("SELECT username, display_name, last_seen_at FROM app_user")).all()
        opens = {r[0]: r for r in s.execute(text(f"""
            SELECT username,
                   count(*) FILTER (WHERE kind = 'page_view' AND created_at >= {since}),
                   count(DISTINCT (created_at AT TIME ZONE 'Europe/Moscow')::date)
                       FILTER (WHERE kind = 'page_view' AND created_at >= {since}),
                   min(created_at) FILTER (WHERE kind = 'page_view'),
                   max(created_at) FILTER (WHERE kind = 'page_view'),
                   coalesce(sum(dur_ms) FILTER (WHERE kind = 'page_leave' AND created_at >= {since}
                                                AND dur_ms BETWEEN 1000 AND 3600000), 0) / 60000.0,
                   count(DISTINCT (created_at AT TIME ZONE 'Europe/Moscow')::date) FILTER (WHERE kind = 'page_view')
              FROM usage_event
             WHERE page = 'vsp' AND kind IN ('page_view', 'page_leave') AND username IS NOT NULL
             GROUP BY 1"""), p).all()}
        api = s.execute(text(f"""
            SELECT username, page, count(*) FROM usage_event
             WHERE kind = 'api_request' AND page LIKE '/api/vsp/%' AND username IS NOT NULL
               AND created_at >= {since}
             GROUP BY 1, 2"""), p).all()
        try:
            cases = dict(s.execute(text(f"""
                SELECT added_by, count(*) FROM audit_case_item
                 WHERE kind = 'vsp' AND added_by IS NOT NULL AND added_at >= {since}
                 GROUP BY 1"""), p).all())
        except Exception:  # noqa: BLE001 — таблицы дел нет (стенд)
            cases = {}
        daily = s.execute(text(f"""
            SELECT (created_at AT TIME ZONE 'Europe/Moscow')::date AS d, username, count(*)
              FROM usage_event
             WHERE kind = 'page_view' AND page = 'vsp' AND username IS NOT NULL AND created_at >= {since}
             GROUP BY 1, 2"""), p).all()
        axis = [r[0] for r in s.execute(text(f"""
            SELECT generate_series({since}::date, (now() AT TIME ZONE 'Europe/Moscow')::date, interval '1 day')::date
        """), p).all()]

    did: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for u, path, n in api:
        k = what_of(path)
        if k:
            did[u][k] += int(n)
    for u, n in cases.items():
        did[u]["case"] += int(n)

    explicit = set(vsp_api.beta_users()) | vsp_api._env_users_vsp()
    units_open = set(vsp_api.beta_units())
    unit_of: dict[str, str | None] = {}
    people, rest = [], []
    acc_by_unit: collections.Counter = collections.Counter()
    for uname, dname, seen in users:
        m = member_of(uname, dname, by_tab, by_name)
        working = bool(m) and m["status"] not in fired
        unit = (m["unit_code"] or "-") if working else None
        unit_name = org_reach.unit_label(m["unit_code"], m["unit_hint"]) if working else None
        unit_of[uname] = unit
        if working:
            acc_by_unit[unit] += 1
        owner = telemetry.is_admin(uname)
        allowed = vsp_api.can_vsp(uname, dname)
        row = {"login": uname, "name": dname or uname, "unit": unit, "unit_name": unit_name}
        if not allowed:
            if uname not in ex:
                rest.append(row)
            continue
        if uname in ex:
            continue
        o = opens.get(uname)
        d = did.get(uname) or {}
        people.append({**row,
                       "how": "owner" if owner else "login" if uname in explicit else "unit",
                       "seen": seen.isoformat() if seen else None,
                       "views": int(o[1]) if o else 0, "days": int(o[2]) if o else 0,
                       "first": o[3].isoformat() if o and o[3] else None,
                       "last": o[4].isoformat() if o and o[4] else None,
                       "minutes": round(float(o[5]), 1) if o else 0.0,
                       "days_all": int(o[6]) if o else 0,
                       "did": {k: int(v) for k, v in d.items()},
                       "deep": sum(int(d.get(k, 0)) for k in DEEP)})
    people.sort(key=lambda r: (r["last"] is None, -(_ts(r["last"]) or 0), r["name"].lower()))

    shown = {r["login"] for r in people}
    per_day: dict = collections.defaultdict(lambda: [0, 0])
    for d, u, n in daily:
        if u in shown:
            per_day[d][0] += 1
            per_day[d][1] += int(n)
    by_day = [{"d": d.isoformat(), "users": per_day[d][0], "views": per_day[d][1]} for d in axis]

    sections = []
    for key, label in [(k, lab) for k, lab, _e, _p in WHAT] + [("case", "Положили в аудит-дело")]:
        us = [r for r in people if r["did"].get(key)]
        sections.append({"key": key, "label": label, "users": len(us),
                         "n": sum(r["did"][key] for r in us)})

    # подразделения штата — для выдачи доступа
    heads: dict[str, dict] = {}
    for m in by_tab.values():
        if m["status"] in fired:
            continue
        code = m["unit_code"] or "-"
        u = heads.setdefault(code, {"code": code, "name": org_reach.unit_label(m["unit_code"], m["unit_hint"]),
                                    "head": 0})
        u["head"] += 1
    units = [{**u, "accounts": int(acc_by_unit.get(u["code"], 0)), "open": u["code"] in units_open}
             for u in heads.values()]
    units.sort(key=lambda u: (not u["open"], u["name"]))

    opened = [r for r in people if r["last"]]
    staff = sum(u["head"] for u in units if u["open"])
    in_units = sum(u["accounts"] for u in units if u["open"])
    return {
        "days": days, "roster": bool(by_tab),
        "access": {"users": sorted(explicit), "units": sorted(units_open)},
        "units": units,
        "logins": [{"login": u, **next(({"name": r["name"], "unit_name": r["unit_name"]}
                                        for r in people + rest if r["login"] == u),
                                       {"name": u, "unit_name": None})}
                   for u in sorted(explicit)],
        "people": people, "rest": sorted(rest, key=lambda r: r["name"].lower()),
        # accounts — сколько людей открытых подразделений уже входили в AuditLens (вместе с владельцем);
        # extra — кому раздел открыт по логину сверх этих подразделений
        "summary": {"staff": staff, "accounts": in_units, "no_account": max(0, staff - in_units),
                    "extra": sum(1 for u in explicit if unit_of.get(u) not in units_open),
                    "with_access": len(people), "opened": len(opened),
                    "opened_period": sum(1 for r in people if r["views"]),
                    "returned": sum(1 for r in people if r["days_all"] >= 2),
                    "deep": sum(1 for r in people if r["deep"]),
                    "minutes": round(sum(r["minutes"] for r in people), 1)},
        "by_day": by_day, "sections": sections,
    }


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None
