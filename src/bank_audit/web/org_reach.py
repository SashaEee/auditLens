"""Охват подразделений для «Пульса»: какая доля штата пользуется инструментом.

Штат владелец загружает в «Пульсе» кадровой выгрузкой (xlsx: табельный номер,
статус, ФИО, должность, грейд, номер и вышестоящее подразделение). Пользователь
сопоставляется со штатом по табельному номеру в конце логина («…-1234567»), а
если номера в штате нет — по имени и фамилии из системы входа (у части людей
логин заведён под другим номером). ФИО в базу не попадают: только ключ
сопоставления — sha256 нормализованных «имя фамилия».

Подписи подразделений — из env ORG_UNIT_NAMES («номер=название,…» или JSON), иначе
сокращение из вышестоящего подразделения («… по НКБ» → «НКБ»), иначе номер.
Уволенные (ORG_FIRED_STATUSES, по умолчанию F) в штат не входят и в «заходили» не засчитываются; численность
штата подразделений можно задать из другого источника (ORG_UNIT_HEADCOUNT).
"""
from __future__ import annotations

import collections
import hashlib
import io
import json
import os
import re
from datetime import timedelta

from sqlalchemy import text

from .. import db
from . import telemetry

_TAB_RX = re.compile(r"-(\d{3,})$")


def _words(s: str | None) -> list[str]:
    return re.sub(r"[^а-яa-z ]", " ", (s or "").lower().replace("ё", "е")).split()


def name_key(first: str, last: str) -> str:
    return hashlib.sha256(" ".join(sorted([first, last])).encode()).hexdigest()


def _fired() -> set[str]:
    return {x.strip() for x in os.getenv("ORG_FIRED_STATUSES", "F").split(",") if x.strip()}


def _unit_names() -> dict:
    """ORG_UNIT_NAMES: JSON {"номер": "название"} или «номер=название,номер=название»
    (второй вид не ломается, если .env читает оболочка)."""
    raw = os.getenv("ORG_UNIT_NAMES", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {k.strip(): v.strip() for k, _, v in (p.partition("=") for p in raw.split(",")) if k.strip() and v.strip()}


def _headcount() -> dict[str, int]:
    """ORG_UNIT_HEADCOUNT — численность штата подразделений из другого источника
    («номер=число,…»): перекрывает посчитанную по выгрузке, пока выгрузка неполная."""
    out = {}
    for part in os.getenv("ORG_UNIT_HEADCOUNT", "").split(","):
        k, _, v = part.partition("=")
        if k.strip() and v.strip().isdigit():
            out[k.strip()] = int(v.strip())
    return out


_HINT_RX = re.compile(r"\bпо\s+([А-ЯЁ][А-ЯЁа-яё]{0,5}Б)\b")


def unit_label(code: str | None, hint: str | None) -> str:
    code = (code or "").strip()
    names = _unit_names()
    if code in names:
        return names[code]
    m = _HINT_RX.search(hint or "")
    if m:
        return m.group(1)
    return "Центр" if code in ("", "-") else f"№ {code}"


# ── загрузка выгрузки ───────────────────────────────────────────────────────
_COLS = {"tab": ("табельн",), "status": ("статус",), "fio": ("фио",), "position": ("должност",),
         "grade": ("грейд",), "hint": ("вышест",), "unit": ("номер",)}


def parse_roster(data: bytes) -> list[dict]:
    """Строки выгрузки → записи штата (без ФИО). Колонки ищутся по заголовку."""
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    head = [str(h or "").lower() for h in next(rows)]
    col = {}
    for k, keys in _COLS.items():
        for i, h in enumerate(head):
            if any(x in h for x in keys) and not (k == "unit" and "табельн" in h) and i not in col.values():
                col[k] = i
                break
    missing = [k for k in ("tab", "fio", "unit") if k not in col]
    if missing:
        raise ValueError("в выгрузке нет колонок: " + ", ".join(
            {"tab": "табельный номер", "fio": "ФИО", "unit": "номер подразделения"}[k] for k in missing))
    out = []
    for r in rows:
        tab = str(r[col["tab"]] or "").strip()
        if not tab:
            continue
        w = _words(str(r[col["fio"]] or ""))
        g = r[col["grade"]] if "grade" in col else None
        out.append({"tab_no": tab, "status": str(r[col["status"]] or "").strip() if "status" in col else None,
                    "unit_code": str(r[col["unit"]] if r[col["unit"]] is not None else "").strip(),
                    "unit_hint": str(r[col["hint"]] or "").strip() if "hint" in col else None,
                    "position": str(r[col["position"]] or "").strip() if "position" in col else None,
                    "grade": int(g) if isinstance(g, (int, float)) else None,
                    "name_key": name_key(w[1], w[0]) if len(w) >= 2 else None})
    return out


def load_roster(data: bytes) -> dict:
    rows = parse_roster(data)
    if not rows:
        raise ValueError("в выгрузке нет строк")
    with db.session() as s:
        s.execute(text("DELETE FROM org_member"))
        for r in rows:
            s.execute(text("""
                INSERT INTO org_member (tab_no, status, unit_code, unit_hint, position, grade, name_key)
                VALUES (:tab_no, :status, :unit_code, :unit_hint, :position, :grade, :name_key)
                ON CONFLICT (tab_no) DO NOTHING"""), r)
    fired = _fired()
    return {"rows": len(rows), "current": sum(1 for r in rows if r["status"] not in fired)}


# ── отчёт ───────────────────────────────────────────────────────────────────
def _role(position: str | None) -> str:
    p = (position or "").lower()
    if any(x in p for x in ("начальник", "руководител", "менеджер")):
        return "Руководители"
    if "эксперт" in p or "специалист" in p or "исследовател" in p:
        return "Эксперты и специалисты"
    if "аудитор" in p:
        return "Аудиторы"
    return "Прочие"


# С 03.10.2026 время на странице — активное: простой открытой вкладки дольше 5 минут не идёт в счёт
# (до этого забытая вкладка набирала до 30 минут за заход). Часы считаем с этого дня, иначе ТБ, начавшие
# раньше, получают завышенное время: на 13.11 у одного из подразделений больше половины часов набежало до 03.10.
HOURS_SINCE = "2026-10-03"


def _build(members: list[dict], logins: dict[str, dict], excluded: set, fired: set,
           override: dict[str, int], today, people: bool = False) -> dict:
    """Свод по уже прочитанным данным (база не нужна — так его проверяют тесты).

    logins: {логин: {"name": имя из системы входа | None, "days": set[date], "ai": вопросов, "mins": минут}}.
    Человек — запись штата: две учётки одного сотрудника считаются одним человеком (дни
    объединяются, вопросы и часы складываются). Логин с номером уволенного (статус F) в штат не
    засчитывается: человек ушёл из подразделения — разве что найдётся работающим по имени."""
    by_tab = {m["tab_no"]: m for m in members}
    by_name = collections.defaultdict(list)
    for m in members:
        if m["name_key"] and m["status"] not in fired:
            by_name[m["name_key"]].append(m)
    since30 = today - timedelta(days=29)
    persons: dict[str, dict] = {}
    other = {"users": 0, "active30": 0, "ai": 0}
    other_people = []
    first = None
    for uname, lg in logins.items():
        if uname in excluded or not lg.get("days"):
            continue
        first = min(first, min(lg["days"])) if first else min(lg["days"])
        m, how = None, None
        t = _TAB_RX.search(uname)
        if t and t.group(1) in by_tab and by_tab[t.group(1)]["status"] not in fired:
            m, how = by_tab[t.group(1)], "tab"
        else:
            w = _words(lg.get("name"))
            cand = by_name.get(name_key(w[0], w[1]), []) if len(w) == 2 else []
            if len(cand) == 1:
                m, how = cand[0], "name"
        q = int(lg.get("ai") or 0)
        if not m:
            r30 = max(lg["days"]) >= since30
            other["users"] += 1
            other["active30"] += r30
            other["ai"] += q
            if people:
                other_people.append({"username": uname, "name": lg.get("name") or uname,
                                     "days": len(lg["days"]), "last": max(lg["days"]).isoformat(),
                                     "ai": q, "active30": r30})
            continue
        p = persons.setdefault(m["tab_no"], {"m": m, "days": set(), "ai": 0, "mins": 0.0, "how": set(), "logins": 0})
        p["days"] |= set(lg["days"])
        p["ai"] += q
        p["mins"] += float(lg.get("mins") or 0)
        p["how"].add(how)
        p["logins"] += 1

    units: dict[str, dict] = {}
    roles: dict[str, dict] = {}

    def unit(m):
        k = (m["unit_code"] or "-")
        return units.setdefault(k, {"code": k, "name": unit_label(m["unit_code"], m["unit_hint"]),
                                    "head": 0, "users": 0, "active30": 0, "ai_users": 0, "ai": 0,
                                    "regular": 0, "hours": 0.0})
    for m in members:
        if m["status"] not in fired:
            unit(m)["head"] += 1
            roles.setdefault(_role(m["position"]), {"head": 0, "users": 0, "ai": 0})["head"] += 1
    for p in persons.values():
        m, q = p["m"], p["ai"]
        u = unit(m)
        u["users"] += 1
        u["active30"] += max(p["days"]) >= since30
        u["ai_users"] += q > 0
        u["ai"] += q
        u["regular"] += len(p["days"]) >= 5
        u["hours"] += p["mins"] / 60
        rr = roles.setdefault(_role(m["position"]), {"head": 0, "users": 0, "ai": 0})
        rr["users"] += 1
        rr["ai"] += q
    for k, u in units.items():
        u["head_roster"] = u["head"]
        if k in override:
            u["head"] = override[k]
            u["head_set"] = True
    rows = sorted(units.values(), key=lambda x: (-(x["users"] / x["head"]) if x["head"] else 0, -x["users"]))
    for r in rows:
        r["reach"] = round(r["users"] / r["head"], 4) if r["head"] else None
        r["hours"] = round(r["hours"], 1)
    head = sum(r["head"] for r in rows)
    users_n = sum(r["users"] for r in rows)
    out = {"label": os.getenv("ORG_UNIT_LABEL", "Подразделение"),
           "head": head, "users": users_n, "reach": round(users_n / head, 4) if head else None,
           "active30": sum(r["active30"] for r in rows),
           "matched": {"tab": sum(1 for p in persons.values() if "tab" in p["how"]),
                       "name": sum(1 for p in persons.values() if "tab" not in p["how"])},
           "dup_logins": sum(1 for p in persons.values() if p["logins"] > 1),
           "other": other, "first_day": first.isoformat() if first else None, "hours_since": HOURS_SINCE,
           "head_set": sum(1 for r in rows if r.get("head_set")),
           "units": rows,
           "roles": [{"role": k, **v, "reach": round(v["users"] / v["head"], 4) if v["head"] else None}
                     for k, v in sorted(roles.items(), key=lambda kv: -kv[1]["users"])]}
    if people:
        # сначала заходившие за 30 дней, внутри — по последнему заходу
        out["other_people"] = sorted(other_people, key=lambda x: (x["active30"], x["last"]), reverse=True)
    return out


def report(viewer: str | None = None, with_me: bool | None = None, people: bool = False) -> dict:
    """Охват по подразделениям: штат, заходили, активны за 30 дней, вопросы ИИ, постоянные
    (5+ дней) и часы. Служебные учётки не считаются. people — список тех, кто не нашёлся в
    штате (только владельцу: по нему видно, кого не хватает в кадровой выгрузке)."""
    excluded = set(telemetry.excluded(viewer, with_me))
    with db.session() as s:
        members = [dict(r) for r in s.execute(text(
            "SELECT tab_no, status, unit_code, unit_hint, position, name_key, loaded_at FROM org_member")).mappings()]
        if not members:
            return {"loaded": False}
        names = dict(s.execute(text("SELECT username, display_name FROM app_user")).all())
        # учётки без строки app_user (заходили до её появления) тоже считаем — раньше они пропадали
        days = s.execute(text("""
            WITH a AS (
              SELECT username, (created_at AT TIME ZONE 'Europe/Moscow')::date d FROM usage_event
               WHERE username IS NOT NULL AND kind IN ('page_view', 'page_leave', 'ui', 'news_click')
              UNION SELECT username, (ts AT TIME ZONE 'Europe/Moscow')::date FROM user_event
               WHERE username IS NOT NULL AND kind IN ('ai_query', 'report_open', 'share'))
            SELECT username, array_agg(DISTINCT d) FROM a GROUP BY 1""")).all()
        # вопрос, повторённый подряд в том же диалоге (повтор после сбоя, смена глубины, «пропустить
        # уточнение»), — один вопрос: на 13.11 так набегало 13% «вопросов»
        ai = dict(s.execute(text("""
            SELECT username, count(*) FILTER (WHERE NOT rep) FROM (
              SELECT username, lag(ts) OVER w IS NOT NULL
                     AND lag(payload->>'question') OVER w IS NOT DISTINCT FROM payload->>'question' AS rep
                FROM user_event WHERE kind = 'ai_query' AND username IS NOT NULL
              WINDOW w AS (PARTITION BY username, coalesce(payload->>'session_id', '') ORDER BY ts)) q
            GROUP BY 1""")).all())
        mins = dict(s.execute(text(
            "SELECT username, coalesce(sum(dur_ms), 0) / 60000.0 FROM usage_event"
            " WHERE kind = 'page_leave' AND username IS NOT NULL"
            "   AND created_at >= (CAST(:since AS timestamp) AT TIME ZONE 'Europe/Moscow') GROUP BY 1"),
            {"since": HOURS_SINCE}).all())
    logins = {u: {"name": names.get(u), "days": set(d or []), "ai": int(ai.get(u, 0)), "mins": float(mins.get(u, 0))}
              for u, d in days}
    out = _build(members, logins, excluded, _fired(), _headcount(), telemetry._msk_now().date(), people)
    out.update(loaded=True, loaded_at=max(m["loaded_at"] for m in members).isoformat())
    return out
