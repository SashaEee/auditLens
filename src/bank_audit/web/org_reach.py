"""Охват подразделений для «Пульса»: какая доля штата пользуется инструментом.

Штат владелец загружает в «Пульсе» кадровой выгрузкой (xlsx: табельный номер,
статус, ФИО, должность, грейд, номер и вышестоящее подразделение). Пользователь
сопоставляется со штатом по табельному номеру в конце логина («…-1234567»), а
если номера в штате нет — по имени и фамилии из системы входа (у части людей
логин заведён под другим номером). ФИО в базу не попадают: только ключ
сопоставления — sha256 нормализованных «имя фамилия».

Подписи подразделений — из env ORG_UNIT_NAMES («номер=название,…» или JSON), иначе
сокращение из вышестоящего подразделения («… по НКБ» → «НКБ»), иначе номер.
Уволенные (ORG_FIRED_STATUSES, по умолчанию F) в штат не входят; численность
штата подразделений можно задать из другого источника (ORG_UNIT_HEADCOUNT).
"""
from __future__ import annotations

import collections
import hashlib
import io
import json
import os
import re

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


def report(viewer: str | None = None, with_me: bool | None = None) -> dict:
    """Охват по подразделениям: штат, пользуются, активны за 30 дней, вопросы ИИ,
    постоянные (5+ дней) и часы. Служебные учётки не считаются."""
    excluded = set(telemetry.excluded(viewer, with_me))
    fired = _fired()
    with db.session() as s:
        members = [dict(r) for r in s.execute(text(
            "SELECT tab_no, status, unit_code, unit_hint, position, name_key, loaded_at FROM org_member")).mappings()]
        if not members:
            return {"loaded": False}
        users = s.execute(text("SELECT username, display_name FROM app_user")).all()
        act = s.execute(text("""
            WITH a AS (
              SELECT username, (created_at AT TIME ZONE 'Europe/Moscow')::date d FROM usage_event
               WHERE username IS NOT NULL AND kind IN ('page_view', 'page_leave', 'ui', 'news_click')
              UNION SELECT username, (ts AT TIME ZONE 'Europe/Moscow')::date FROM user_event
               WHERE kind IN ('ai_query', 'report_open', 'share'))
            SELECT username, count(DISTINCT d), max(d) >= (now() AT TIME ZONE 'Europe/Moscow')::date - 29
            FROM a GROUP BY 1""")).all()
        ai = dict(s.execute(text(
            "SELECT username, count(*) FROM user_event WHERE kind = 'ai_query' GROUP BY 1")).all())
        mins = dict(s.execute(text(
            "SELECT username, coalesce(sum(dur_ms), 0) / 60000.0 FROM usage_event"
            " WHERE kind = 'page_leave' AND username IS NOT NULL GROUP BY 1")).all())
    by_tab = {m["tab_no"]: m for m in members}
    by_name = collections.defaultdict(list)
    for m in members:
        if m["name_key"] and m["status"] not in fired:
            by_name[m["name_key"]].append(m)
    actd = {u: (int(n), bool(r30)) for u, n, r30 in act}
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
    other = {"users": 0, "active30": 0, "ai": 0}
    matched = {"tab": 0, "name": 0}
    for uname, dname in users:
        if uname in excluded or uname not in actd:
            continue
        days, r30 = actd[uname]
        m, how = None, None
        t = _TAB_RX.search(uname)
        if t and t.group(1) in by_tab:
            m, how = by_tab[t.group(1)], "tab"
        else:
            w = _words(dname)
            if len(w) == 2 and len(by_name.get(name_key(w[0], w[1]), [])) == 1:
                m, how = by_name[name_key(w[0], w[1])][0], "name"
        q = int(ai.get(uname, 0))
        if not m:
            other["users"] += 1
            other["active30"] += r30
            other["ai"] += q
            continue
        matched[how] += 1
        u = unit(m)
        u["users"] += 1
        u["active30"] += r30
        u["ai_users"] += q > 0
        u["ai"] += q
        u["regular"] += days >= 5
        u["hours"] += float(mins.get(uname, 0)) / 60
        rr = roles.setdefault(_role(m["position"]), {"head": 0, "users": 0, "ai": 0})
        rr["users"] += 1
        rr["ai"] += q
    override = _headcount()
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
    return {"loaded": True, "loaded_at": max(m["loaded_at"] for m in members).isoformat(),
            "label": os.getenv("ORG_UNIT_LABEL", "Подразделение"),
            "head": head, "users": users_n, "reach": round(users_n / head, 4) if head else None,
            "active30": sum(r["active30"] for r in rows), "matched": matched, "other": other,
            "head_set": sum(1 for r in rows if r.get("head_set")),
            "units": rows,
            "roles": [{"role": k, **v, "reach": round(v["users"] / v["head"], 4) if v["head"] else None}
                      for k, v in sorted(roles.items(), key=lambda kv: -kv[1]["users"])]}
