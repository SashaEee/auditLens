"""Письма об обращениях («Обратная связь»): каждое новое обращение и каждое новое сообщение
автора в обращении — письмом команде, без условий колокольчика.

До 05.11.2026 об обращении можно было узнать только из колокольчика: мгновенное письмо уходило,
лишь если уведомление 10 минут оставалось непрочитанным, — открыл приложение, и письма нет.
Владелец попросил: все обращения — ему на почту, обращения по отдельным разделам (например,
«Аудит уязвимостей») — ещё и ответственному за раздел.

Кому — правило в таблице app_setting (ключ inbox_mail, правится в «Пульсе» → «Обращения»):
    {"all": ["<логин>", ...], "sections": {"<раздел>": ["<логин или адрес>", ...]}}
Без правила — владельцу (ADMIN_USERS). Логин → подтверждённая почта человека (колокольчик);
адрес вписывает владелец сам. Имена и адреса коллег в код не пишем: репозиторий публичный.

Отправляет тик рассылки (mail_delivery.tick, раз в несколько минут): курсор inbox_mail_cursor
в app_setting; при первом запуске он встаёт на текущие номера — старые обращения не рассылаем.
Повтор после сбоя не дублирует письма: перед отправкой проверяется журнал app_mail_log.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from sqlalchemy import text

from .. import db
from . import mail_templates as T

log = logging.getLogger(__name__)

KEY_ROUTES = "inbox_mail"
KEY_CURSOR = "inbox_mail_cursor"
BATCH = 20                       # не больше обращений за один тик
MAX_IMAGES = 3


# ── настройки в app_setting ──────────────────────────────────────────────────

def _get(key: str):
    try:
        with db.session() as s:
            v = s.execute(text("SELECT value FROM app_setting WHERE key = :k"), {"k": key}).scalar()
    except Exception:  # noqa: BLE001 — до миграции 111
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v


def _set(key: str, value, by: str | None = None) -> None:
    with db.session() as s:
        s.execute(text("""INSERT INTO app_setting (key, value, updated_at, updated_by)
                          VALUES (:k, CAST(:v AS jsonb), now(), :b)
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                updated_at = now(), updated_by = EXCLUDED.updated_by"""),
                  {"k": key, "v": json.dumps(value, ensure_ascii=False), "b": by})


def _owners() -> list[str]:
    raw = os.getenv("ADMIN_USERS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _clean_list(xs) -> list[str]:
    out: list[str] = []
    for x in xs if isinstance(xs, list) else str(xs or "").replace(";", ",").split(","):
        x = str(x or "").strip()
        if x and x not in out and len(x) <= 200:
            out.append(x)
    return out[:10]


def routes() -> dict:
    """Правило рассылки; без сохранённого — всё владельцу."""
    v = _get(KEY_ROUTES)
    if not isinstance(v, dict):
        return {"all": _owners(), "sections": {}, "saved": False}
    secs = {str(k)[:40]: _clean_list(x) for k, x in (v.get("sections") or {}).items() if _clean_list(x)}
    return {"all": _clean_list(v.get("all")), "sections": secs, "saved": True}


def set_routes(all_: list[str] | str, sections: dict, by: str) -> dict:
    value = {"all": _clean_list(all_),
             "sections": {str(k)[:40]: _clean_list(v) for k, v in (sections or {}).items() if _clean_list(v)}}
    _set(KEY_ROUTES, value, by)
    return routes()


# ── адресаты ─────────────────────────────────────────────────────────────────

_LOGIN_ADDR = re.compile(r"^\s*([^\s<>@]+)\s*<\s*([^<>\s]+@[^<>\s]+)\s*>\s*$")


def _resolve(entry: str) -> dict:
    """Логин → подтверждённая почта из колокольчика; адрес → как есть (его вписал владелец);
    «логин <адрес>» — письмо на адрес, права (есть ли «Пульс», чтобы ответить) — по логину."""
    from . import mail_delivery as MD
    m = _LOGIN_ADDR.match(entry)
    if m:
        login, addr = m.group(1), m.group(2).lower()
        with db.session() as s:
            name = s.execute(text("SELECT display_name FROM app_user WHERE username = :u"), {"u": login}).scalar()
        return {"entry": entry, "username": login, "name": name, "email": addr, "ok": True, "source": "admin"}
    if "@" in entry:
        return {"entry": entry, "username": None, "name": None, "email": entry.lower(), "ok": True,
                "source": "admin"}
    with db.session() as s:
        r = s.execute(text("""SELECT username, display_name, email, email_source FROM app_user
                               WHERE username = :u"""), {"u": entry}).mappings().first()
    if not r:
        return {"entry": entry, "username": entry, "name": None, "email": None, "ok": False,
                "why": "нет такого пользователя"}
    usable = bool(r["email"]) and (r["email_source"] == "user" or MD._sso_enabled())
    return {"entry": entry, "username": r["username"], "name": r["display_name"],
            "email": r["email"] if usable else None, "ok": usable, "source": r["email_source"],
            "why": None if usable else "почта не подключена — человек подключает её сам в колокольчике"}


def recipients_for(section: str | None) -> list[dict]:
    rt = routes()
    out, seen = [], set()
    for scope, xs in (("all", rt["all"]), ("section", rt["sections"].get(section or "", []))):
        for e in xs:
            r = _resolve(e)
            if r["ok"] and r["email"] not in seen:
                seen.add(r["email"])
                out.append({**r, "scope": scope})
    return out


def describe() -> dict:
    """Для «Пульса»: правило, кто из адресатов получит письмо и почему нет."""
    rt = routes()
    return {"all": [_resolve(e) for e in rt["all"]],
            "sections": {k: [_resolve(e) for e in v] for k, v in rt["sections"].items()},
            "saved": rt["saved"], "known_sections": known_sections()}


def known_sections() -> list[dict]:
    """Разделы, по которым бывают обращения: из самих обращений плюс основные разделы."""
    base = {"loophole": "Аудит уязвимостей", "reviews": "Аудит отзывов", "vsp": "Аудит сети ВСП",
            "overview": "Новостные обзоры", "ai": "ИИ-помощник", "knowledge": "База знаний",
            "profile": "Профиль", "pulse": "Пульс"}
    try:
        with db.session() as s:
            for k, lab in s.execute(text("""SELECT section, max(section_label) FROM user_ticket
                                             WHERE section IS NOT NULL GROUP BY section""")).all():
                base.setdefault(k, (lab or k).split(" › ")[0])
    except Exception:  # noqa: BLE001
        pass
    return [{"key": k, "label": v} for k, v in base.items()]


# ── письмо ───────────────────────────────────────────────────────────────────

def render(t: dict, *, msg: dict | None, private: bool, team: bool, images: list[dict],
           by_section: bool = False) -> dict:
    """Обращение (или новое сообщение в нём) → письмо. private — на некорпоративную почту:
    только номер и раздел, без текста и снимков. team — адресат отвечает в «Пульсе»."""
    no = t["ticket_id"]
    base = T.app_base()
    url = f"{base}/#pulse?tab=inbox&ticket={no}" if team else f"{base}/"
    who = t.get("author_name") or t.get("username") or "Пользователь"
    sect = t.get("section_label") or "AuditLens"
    kind = t.get("kind_label") or "Обращение"
    wh = T.when(msg["created_at"] if msg else t["created_at"])
    if msg:
        subject = f"Новое сообщение в обращении № {no} · {sect}"
        title, sub = "Пользователь дописал обращение", f"№ {no} · {kind} · {sect}"
    else:
        subject = f"Обращение № {no} · {kind} · {sect}"
        title, sub = "Новое обращение", f"№ {no} · {kind} · {sect}"
    if private:
        subject = f"Обращение № {no} в AuditLens"
    reason = ("Вы получили это письмо, потому что вам приходят обращения AuditLens"
              + (f" по разделу «{sect.split(' › ')[0]}»" if by_section or not team else "") + ".")
    note = ("Ответ на это письмо автор не увидит — отвечайте в «Пульсе» → «Обращения»." if team
            else "Ответ на это письмо автор не увидит. Ответ пишет команда AuditLens — передайте его владельцу.")
    if private:
        body = (T._eyebrow("Обращение пользователя", T.TICKET) + T._h1(title, f"№ {no}")
                + T._p("Подробности — в AuditLens: адрес почты не корпоративный, текст обращения в письмо не кладём.",
                       color=T.INK3, size=14, top=18)
                + (T._btn("Открыть обращение", url) if team else ""))
        lines = [f"Обращение № {no}", "Подробности — в AuditLens."]
    else:
        ctx = t.get("context") or {}
        rows = [("Автор", who), ("Тип", kind), ("Раздел", sect), ("Когда", wh)]
        if not msg:
            rows += [("Страница", str(ctx.get("url") or "")), ("Браузер", str(ctx.get("browser") or "")),
                     ("Экран", str(ctx.get("screen") or "")), ("Версия", str(ctx.get("version") or ""))]
        text_ = (msg or {}).get("body") or t.get("body") or ""
        body = T._eyebrow("Обращение пользователя", T.TICKET) + T._h1(title, sub)
        body += T._message(who, wh, text_)
        if msg and t.get("body"):
            body += T._card("Само обращение", T._clip(t["body"], 300), accent=T.HAIR, top=18)
        body += T._card("Подробности", f"№ {no}", rows=[r for r in rows if r[1]], top=18)
        errs = ctx.get("errors") if isinstance(ctx, dict) else None
        if errs and not msg:
            body += T._p("Ошибки на странице: " + T._e("; ".join(str(e) for e in errs[:3])), color=T.INK3,
                         size=13, top=14)
        for i, im in enumerate(images):
            body += (f'<div style="margin:18px 0 0"><img src="cid:{im["cid"]}" alt="Снимок {i + 1}" width="536" '
                     f'style="display:block;width:100%;max-width:536px;height:auto;border:1px solid {T.HAIR};'
                     f'border-radius:10px"></div>')
        if team:
            body += T._btn("Ответить в «Пульсе»", url)
        lines = [f"{who}, {wh}:", f"«{text_}»", ""] + [f"{k}: {v}" for k, v in rows if v] + (
            [f"Снимков: {len(images)} (во вложении)"] if images else [])
    pre = T._clip(f"{who}: «{(msg or {}).get('body') or t.get('body') or ''}»", 140) if not private else subject
    return {"subject": subject, "preheader": pre, "url": url,
            "html": T.layout(preheader=pre, title=subject, body=body, reason=reason, note=note,
                             section="Обращения"),
            "text": T._text("Обращение пользователя", f"{title} — {sub}", lines,
                            "Ответить" if team else "Открыть AuditLens", url, reason, note),
            "thread": f"ticket-{no}",
            "images": [(im["cid"], im["data"], im["subtype"]) for im in images]}


# ── отправка ─────────────────────────────────────────────────────────────────

def _ticket(tid: int) -> dict | None:
    with db.session() as s:
        r = s.execute(text("""SELECT t.ticket_id, t.username, t.kind, t.section, t.section_label, t.body,
                                     t.context, t.created_at, au.display_name AS author_name
                                FROM user_ticket t LEFT JOIN app_user au ON au.username = t.username
                               WHERE t.ticket_id = :t"""), {"t": tid}).mappings().first()
    if not r:
        return None
    t = dict(r)
    from .inbox import KINDS
    t["kind_label"] = KINDS.get(t["kind"], "Обращение")
    if isinstance(t.get("context"), str):
        try:
            t["context"] = json.loads(t["context"])
        except ValueError:
            t["context"] = {}
    return t


def _images(tid: int) -> list[dict]:
    with db.session() as s:
        rows = s.execute(text("""SELECT file_id, mime, data FROM user_ticket_file WHERE ticket_id = :t
                                  ORDER BY file_id LIMIT :n"""), {"t": tid, "n": MAX_IMAGES}).mappings().all()
    out = []
    for r in rows:
        sub = (r["mime"] or "image/png").split("/")[-1]
        out.append({"cid": f"ticket-{tid}-{r['file_id']}@auditlens", "data": bytes(r["data"]), "subtype": sub})
    return out


def _sent(kind: str, to: str, n: int) -> bool:
    with db.session() as s:
        return bool(s.execute(text("""SELECT 1 FROM app_mail_log WHERE kind = :k AND to_addr = :to AND n_items = :n
                                        AND ok LIMIT 1"""), {"k": kind, "to": to, "n": n}).scalar())


def _mark_bell_emailed(username: str | None, tid: int) -> None:
    """Письмо об обращении уже ушло — то же событие из колокольчика второй раз не шлём."""
    if not username:
        return
    with db.session() as s:
        s.execute(text("""UPDATE app_notice SET emailed_at = now()
                           WHERE username = :u AND kind = 'team_ticket' AND emailed_at IS NULL
                             AND (ref->>'no') = :t"""), {"u": username, "t": str(tid)})


def _send_item(t: dict, msg: dict | None) -> int:
    """Одно обращение или сообщение — всем адресатам. MailError пробрасывает (повтор на следующем тике)."""
    from . import mail_delivery as MD
    from .telemetry import can_pulse
    kind = "ticket_msg" if msg else "ticket_new"
    n_ref = int(msg["msg_id"]) if msg else int(t["ticket_id"])
    sent = 0
    imgs = None
    for r in recipients_for(t.get("section")):
        to = r["email"]
        if MD.is_blocked(to) or _sent(kind, to, n_ref):
            continue
        private = not MD.is_corporate(to)
        team = bool(r["username"]) and can_pulse(r["username"])
        if imgs is None:
            imgs = [] if msg else _images(int(t["ticket_id"]))
        mail = render(t, msg=msg, private=private, team=team, images=[] if private else imgs,
                      by_section=r.get("scope") == "section")
        MD.deliver(r["username"], kind, to, mail, consented=True, n=n_ref)
        if not msg:
            _mark_bell_emailed(r["username"], int(t["ticket_id"]))
        sent += 1
    return sent


def tick(now: datetime | None = None) -> dict:
    """Новые обращения и сообщения авторов после курсора — письмами. Вызывается из тика рассылки."""
    with db.session() as s:
        try:
            last_t = int(s.execute(text("SELECT COALESCE(max(ticket_id), 0) FROM user_ticket")).scalar() or 0)
            last_m = int(s.execute(text("SELECT COALESCE(max(msg_id), 0) FROM user_ticket_msg")).scalar() or 0)
        except Exception:  # noqa: BLE001 — нет таблиц обращений
            return {"skipped": "нет обращений"}
    cur = _get(KEY_CURSOR)
    if not isinstance(cur, dict) or "ticket" not in cur:
        _set(KEY_CURSOR, {"ticket": last_t, "msg": last_m,
                          "since": (now or datetime.now(timezone.utc)).isoformat()}, "system")
        return {"started": True, "ticket": last_t, "msg": last_m}
    out = {"tickets": 0, "messages": 0, "mails": 0}
    with db.session() as s:
        tids = [int(x) for x in s.execute(text("""SELECT ticket_id FROM user_ticket WHERE ticket_id > :c
                                                   ORDER BY ticket_id LIMIT :n"""),
                                          {"c": int(cur["ticket"]), "n": BATCH}).scalars()]
        msgs = [dict(r) for r in s.execute(text("""SELECT msg_id, ticket_id, body, created_at FROM user_ticket_msg
                                                    WHERE msg_id > :c AND role = 'user'
                                                    ORDER BY msg_id LIMIT :n"""),
                                           {"c": int(cur.get("msg") or 0), "n": BATCH}).mappings()]
        top_msg = int(s.execute(text("SELECT COALESCE(max(msg_id), 0) FROM user_ticket_msg WHERE msg_id > :c"),
                                {"c": int(cur.get("msg") or 0)}).scalar() or 0)
    for tid in tids:
        t = _ticket(tid)
        if t:
            out["mails"] += _send_item(t, None)
            out["tickets"] += 1
        cur["ticket"] = tid
        _set(KEY_CURSOR, cur, "system")
    for m in msgs:
        t = _ticket(int(m["ticket_id"]))
        if t:
            out["mails"] += _send_item(t, m)
            out["messages"] += 1
        cur["msg"] = int(m["msg_id"])
        _set(KEY_CURSOR, cur, "system")
    if not msgs and top_msg > int(cur.get("msg") or 0):        # дальше только ответы команды
        cur["msg"] = top_msg
        _set(KEY_CURSOR, cur, "system")
    return out
