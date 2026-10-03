"""«Обратная связь»: обращения пользователей к команде AuditLens (миграция 081).

Вход — строка «Обратная связь» внизу меню. Человек выбирает тип, раздел
подставляется сам, фронт прикладывает контекст (адрес с фильтрами, версия,
браузер, последние ошибки страницы) и снимки экрана. Команда отвечает во
вкладке «Обращения» «Пульса»; ответ и смена статуса приходят автору туда же,
где он писал, — точкой у строки меню и списком «Мои обращения».

Слова feedback/track/support в адресах не используем: блокировщики рекламы
режут такие запросы (см. /api/journal в app.py).
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

KINDS = {"idea": "Идея", "bug": "Ошибка", "numbers": "Неверные цифры",
         "howto": "Вопрос", "other": "Другое"}
STATUSES = {"new": "Новое", "accepted": "Принято", "in_progress": "В работе",
            "done": "Сделано", "wontfix": "Не будем делать", "exists": "Уже есть"}
OPEN = ("new", "accepted", "in_progress")

MAX_BODY = 4000
MAX_MSG = 2000
MAX_FILES = 3
MAX_FILE = 3 * 1024 * 1024
MAX_CTX = 8000
PER_HOUR = 10


class TicketError(ValueError):
    """Ошибка ввода — текст показывается человеку как есть."""


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params or {}).mappings().all()]


def _int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ── проверка ввода ───────────────────────────────────────────────────────────

def sniff_image(data: bytes) -> str | None:
    """Тип снимка по сигнатуре, а не по словам браузера. SVG не принимаем:
    это документ со скриптами, а не картинка."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image(raw: str) -> tuple[bytes, str]:
    raw = str(raw or "")
    if raw.startswith("data:") and "," in raw[:100]:
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception as e:  # noqa: BLE001
        raise TicketError("Снимок не читается — вставьте его ещё раз") from e
    if not data:
        raise TicketError("Снимок пустой")
    if len(data) > MAX_FILE:
        raise TicketError("Снимок больше 3 МБ — уменьшите его")
    mime = sniff_image(data)
    if not mime:
        raise TicketError("Снимок должен быть PNG, JPEG или WebP")
    return data, mime


def clean_context(ctx: Any) -> dict:
    """Контекст от фронта: плоский словарь коротких значений, не больше 8 КБ."""
    if not isinstance(ctx, dict):
        return {}
    out: dict = {}
    for k, v in list(ctx.items())[:30]:
        key = str(k)[:40]
        if isinstance(v, (int, float, bool)) or v is None:
            out[key] = v
        elif isinstance(v, str):
            out[key] = v[:500]
        elif isinstance(v, list):
            out[key] = [str(x)[:300] for x in v[:8]]
    while len(json.dumps(out, ensure_ascii=False)) > MAX_CTX and out:
        out.pop(next(reversed(out)))
    return out


def clean_body(body: Any, limit: int = MAX_BODY) -> str:
    t = str(body or "").strip()
    if len(t) < 3:
        raise TicketError("Опишите, что случилось, — хотя бы одной фразой")
    return t[:limit]


# ── сторона пользователя ─────────────────────────────────────────────────────

def create(username: str, kind: str, section: str | None, section_label: str | None,
           body: str, context: Any = None, files: list | None = None) -> dict:
    body = clean_body(body)
    kind = kind if kind in KINDS else "other"
    decoded = [decode_image((f or {}).get("data")) + ((f or {}).get("w"), (f or {}).get("h"))
               for f in (files or [])[:MAX_FILES]]
    ctx = clean_context(context)
    with db.session() as s:
        n = s.execute(text("""SELECT count(*) FROM user_ticket WHERE username = :u
                               AND created_at > now() - interval '1 hour'"""),
                      {"u": username}).scalar_one()
        if n >= PER_HOUR:
            raise TicketError("За час отправлено много обращений — попробуйте чуть позже")
        tid = s.execute(text("""
            INSERT INTO user_ticket (username, kind, section, section_label, body, context, user_seen_at)
            VALUES (:u, :k, :s, :l, :b, CAST(:c AS jsonb), now())
            RETURNING ticket_id"""), {
            "u": username, "k": kind, "s": (section or "")[:40] or None,
            "l": (section_label or "")[:120] or None, "b": body,
            "c": json.dumps(ctx, ensure_ascii=False)}).scalar_one()
        for data, mime, w, h in decoded:
            s.execute(text("""INSERT INTO user_ticket_file (ticket_id, mime, data, width, height)
                              VALUES (:t, :m, :d, :w, :h)"""),
                      {"t": tid, "m": mime, "d": data, "w": _int(w), "h": _int(h)})
    return {"ticket_id": int(tid)}


def add_file(username: str, ticket_id: int, raw: str, w: Any = None, h: Any = None) -> dict:
    """Снимок отдельным запросом: три снимка одним телом упирались бы в лимит
    размера запроса на прокси."""
    data, mime = decode_image(raw)
    with db.session() as s:
        own = s.execute(text("""SELECT 1 FROM user_ticket WHERE ticket_id = :t AND username = :u
                                 AND created_at > now() - interval '1 day'"""),
                        {"t": ticket_id, "u": username}).first()
        if not own:
            raise TicketError("Обращение не найдено")
        n = s.execute(text("SELECT count(*) FROM user_ticket_file WHERE ticket_id = :t"),
                      {"t": ticket_id}).scalar_one()
        if n >= MAX_FILES:
            raise TicketError("К обращению можно приложить до трёх снимков")
        fid = s.execute(text("""INSERT INTO user_ticket_file (ticket_id, mime, data, width, height)
                                VALUES (:t, :m, :d, :w, :h) RETURNING file_id"""),
                        {"t": ticket_id, "m": mime, "d": data, "w": _int(w), "h": _int(h)}).scalar_one()
    return {"file_id": int(fid)}


def _threads(ids: list[int]) -> tuple[dict, dict]:
    if not ids:
        return {}, {}
    msgs: dict[int, list] = {}
    for r in _rows("""SELECT ticket_id, role, body, created_at FROM user_ticket_msg
                       WHERE ticket_id = ANY(:ids) ORDER BY created_at, msg_id""", {"ids": ids}):
        msgs.setdefault(r["ticket_id"], []).append(
            {"role": r["role"], "body": r["body"], "at": r["created_at"].isoformat()})
    files: dict[int, list] = {}
    for r in _rows("""SELECT file_id, ticket_id, width, height FROM user_ticket_file
                       WHERE ticket_id = ANY(:ids) ORDER BY file_id""", {"ids": ids}):
        files.setdefault(r["ticket_id"], []).append(
            {"file_id": r["file_id"], "w": r["width"], "h": r["height"]})
    return msgs, files


def mine(username: str) -> dict:
    rows = _rows("""
        SELECT t.ticket_id, t.kind, t.section, t.section_label, t.body, t.status, t.confirmed,
               t.created_at, t.updated_at,
               EXISTS (SELECT 1 FROM user_ticket_msg m
                        WHERE m.ticket_id = t.ticket_id AND m.role <> 'user'
                          AND m.created_at > COALESCE(t.user_seen_at, 'epoch'::timestamptz)) AS unread
          FROM user_ticket t
         WHERE t.username = :u
         ORDER BY t.updated_at DESC LIMIT 50""", {"u": username})
    msgs, files = _threads([r["ticket_id"] for r in rows])
    out = []
    for r in rows:
        tid = r["ticket_id"]
        out.append({**r, "created_at": r["created_at"].isoformat(),
                    "updated_at": r["updated_at"].isoformat(),
                    "status_label": STATUSES.get(r["status"], r["status"]),
                    "kind_label": KINDS.get(r["kind"], r["kind"]),
                    "messages": msgs.get(tid, []), "files": files.get(tid, [])})
    return {"tickets": out, "unread": sum(1 for t in out if t["unread"])}


def unread_info(username: str) -> dict:
    """Сколько обращений с непрочитанным ответом и время последнего ответа:
    по нему фронт один раз показывает заметку «Команда ответила»."""
    rows = _rows("""
        SELECT count(DISTINCT t.ticket_id) AS n, max(m.created_at) AS last_at
          FROM user_ticket t
          JOIN user_ticket_msg m ON m.ticket_id = t.ticket_id AND m.role <> 'user'
                                AND m.created_at > COALESCE(t.user_seen_at, 'epoch'::timestamptz)
         WHERE t.username = :u""", {"u": username})
    r = rows[0] if rows else {}
    last = r.get("last_at")
    return {"unread": int(r.get("n") or 0), "last_at": last.isoformat() if last else None}


def mark_seen(username: str, ticket_id: int) -> None:
    with db.session() as s:
        s.execute(text("""UPDATE user_ticket SET user_seen_at = now()
                          WHERE ticket_id = :t AND username = :u"""),
                  {"t": ticket_id, "u": username})


def _own_status(s, username: str, ticket_id: int) -> str:
    row = s.execute(text("SELECT status FROM user_ticket WHERE ticket_id = :t AND username = :u"),
                    {"t": ticket_id, "u": username}).first()
    if not row:
        raise TicketError("Обращение не найдено")
    return row[0]


def user_message(username: str, ticket_id: int, body: str) -> None:
    """Уточнение автора. Для команды оно непрочитанное, пока обращение не открыли."""
    body = clean_body(body, MAX_MSG)
    with db.session() as s:
        _own_status(s, username, ticket_id)
        s.execute(text("""INSERT INTO user_ticket_msg (ticket_id, author, role, body)
                          VALUES (:t, :u, 'user', :b)"""), {"t": ticket_id, "u": username, "b": body})
        s.execute(text("""UPDATE user_ticket SET updated_at = now(), user_seen_at = now()
                          WHERE ticket_id = :t"""), {"t": ticket_id})


def confirm(username: str, ticket_id: int, ok: bool, comment: str | None = None) -> None:
    """После «Сделано» автор подтверждает: работает — закрыто; нет — открыто снова."""
    with db.session() as s:
        if _own_status(s, username, ticket_id) != "done":
            raise TicketError("Подтвердить можно только сделанное")
        if comment and comment.strip():
            s.execute(text("""INSERT INTO user_ticket_msg (ticket_id, author, role, body)
                              VALUES (:t, :u, 'user', :b)"""),
                      {"t": ticket_id, "u": username, "b": comment.strip()[:MAX_MSG]})
        s.execute(text("""INSERT INTO user_ticket_msg (ticket_id, author, role, body)
                          VALUES (:t, :u, 'system', :b)"""),
                  {"t": ticket_id, "u": username,
                   "b": "Автор подтвердил: работает" if ok
                   else "Автор: не работает — обращение открыто снова"})
        s.execute(text("""UPDATE user_ticket SET confirmed = :ok, updated_at = now(),
                                 user_seen_at = now(),
                                 status = CASE WHEN :ok THEN status ELSE 'accepted' END
                          WHERE ticket_id = :t"""), {"t": ticket_id, "ok": bool(ok)})


def get_file(file_id: int, username: str, is_admin: bool) -> tuple[bytes, str] | None:
    rows = _rows("""SELECT f.mime, f.data, t.username FROM user_ticket_file f
                      JOIN user_ticket t USING (ticket_id) WHERE f.file_id = :f""", {"f": file_id})
    if not rows:
        return None
    r = rows[0]
    if r["username"] != username and not is_admin:
        return None
    return bytes(r["data"]), r["mime"]


# ── сторона команды («Пульс» → «Обращения») ─────────────────────────────────

_TEAM_UNREAD = """(t.team_seen_at IS NULL OR EXISTS (
        SELECT 1 FROM user_ticket_msg m WHERE m.ticket_id = t.ticket_id AND m.role = 'user'
           AND m.created_at > t.team_seen_at))"""


def admin_list(status: str = "open", kind: str | None = None, section: str | None = None) -> dict:
    cond, p = [], {}
    if status == "new":
        cond.append("t.status = 'new'")
    elif status == "open":
        cond.append("t.status = ANY(:open)")
        p["open"] = list(OPEN)
    if kind in KINDS:
        cond.append("t.kind = :k")
        p["k"] = kind
    if section:
        cond.append("t.section = :s")
        p["s"] = section
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    rows = _rows(f"""
        SELECT t.ticket_id, t.username, COALESCE(au.display_name, t.username) AS name,
               t.kind, t.section, t.section_label, left(t.body, 300) AS body, t.status,
               t.confirmed, t.created_at, t.updated_at,
               (SELECT count(*) FROM user_ticket_file f WHERE f.ticket_id = t.ticket_id) AS n_files,
               (SELECT count(*) FROM user_ticket_msg m WHERE m.ticket_id = t.ticket_id
                                                       AND m.role <> 'system') AS n_msgs,
               {_TEAM_UNREAD} AS unread
          FROM user_ticket t
          LEFT JOIN app_user au ON au.username = t.username
          {where}
         ORDER BY ({_TEAM_UNREAD}) DESC, t.updated_at DESC LIMIT 200""", p)
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
        r["updated_at"] = r["updated_at"].isoformat()
        r["status_label"] = STATUSES.get(r["status"], r["status"])
        r["kind_label"] = KINDS.get(r["kind"], r["kind"])
    return {"tickets": rows, "counts": brief(), "kinds": KINDS, "statuses": STATUSES}


def admin_get(ticket_id: int, mark_seen_: bool = True) -> dict | None:
    rows = _rows("""
        SELECT t.*, COALESCE(au.display_name, t.username) AS name
          FROM user_ticket t LEFT JOIN app_user au ON au.username = t.username
         WHERE t.ticket_id = :t""", {"t": ticket_id})
    if not rows:
        return None
    t = rows[0]
    msgs, files = _threads([ticket_id])
    if mark_seen_:
        with db.session() as s:
            s.execute(text("UPDATE user_ticket SET team_seen_at = now() WHERE ticket_id = :t"),
                      {"t": ticket_id})
    for k in ("created_at", "updated_at", "user_seen_at", "team_seen_at"):
        if t.get(k) is not None:
            t[k] = t[k].isoformat()
    return {**t, "status_label": STATUSES.get(t["status"], t["status"]),
            "kind_label": KINDS.get(t["kind"], t["kind"]),
            "messages": msgs.get(ticket_id, []), "files": files.get(ticket_id, [])}


def admin_update(ticket_id: int, admin: str, status: str | None = None,
                 reply: str | None = None) -> dict | None:
    """Смена статуса и ответ автору. Смена статуса пишется служебной строкой в
    переписку — автор видит её так же, как ответ."""
    with db.session() as s:
        row = s.execute(text("SELECT status FROM user_ticket WHERE ticket_id = :t"),
                        {"t": ticket_id}).first()
        if not row:
            return None
        changed = bool(status and status in STATUSES and status != row[0])
        if changed:
            s.execute(text("UPDATE user_ticket SET status = :s, confirmed = NULL WHERE ticket_id = :t"),
                      {"t": ticket_id, "s": status})
            s.execute(text("""INSERT INTO user_ticket_msg (ticket_id, author, role, body)
                              VALUES (:t, :a, 'system', :b)"""),
                      {"t": ticket_id, "a": admin, "b": f"Статус: {STATUSES[status]}"})
        if reply and reply.strip():
            s.execute(text("""INSERT INTO user_ticket_msg (ticket_id, author, role, body)
                              VALUES (:t, :a, 'team', :b)"""),
                      {"t": ticket_id, "a": admin, "b": reply.strip()[:MAX_MSG]})
        s.execute(text("""UPDATE user_ticket SET updated_at = now(), team_seen_at = now()
                          WHERE ticket_id = :t"""), {"t": ticket_id})
    t = admin_get(ticket_id, mark_seen_=False)
    if t is not None:
        t["status_changed"] = changed
    return t


def brief() -> dict:
    """Счётчики для сторожа «Пульса» и подписи вкладки."""
    try:
        rows = _rows(f"""
            SELECT count(*) FILTER (WHERE t.status = 'new') AS new,
                   count(*) FILTER (WHERE t.status = ANY(:open)) AS open,
                   count(*) FILTER (WHERE {_TEAM_UNREAD}) AS unread,
                   count(*) AS total,
                   COALESCE(max(EXTRACT(day FROM now() - t.created_at))
                              FILTER (WHERE t.status = 'new'), 0)::int AS oldest_new_days
              FROM user_ticket t""", {"open": list(OPEN)})
        return rows[0] if rows else {}
    except Exception:  # noqa: BLE001 — таблицы ещё нет: «Пульс» не должен падать
        log.debug("[inbox] brief failed", exc_info=True)
        return {}
