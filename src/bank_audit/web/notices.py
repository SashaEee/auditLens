"""Уведомления в приложении (миграция 083).

Колокольчик рядом с карточкой пользователя внизу меню: верхнюю панель не
трогаем — там и так тема, «Аудит-дела» и поиск. Сюда сходятся события, о
которых человек иначе не узнает: его добавили в дело или сменили роль,
коллега приобщил материалы, с ним поделились отчётом, команда ответила на
обращение. Почты на проде нет — всё внутри.

Правила:
• автору события уведомление не шлём — он и так знает;
• склейка: пока уведомление не прочитано, новые материалы того же коллеги в
  то же дело копятся в одной строке («5 новых материалов»), ответы и статусы
  одного обращения — тоже;
• группы можно выключить в настройках колокольчика (app_user.prefs.notify_off);
• открыл дело / отчёт / обращение любым путём — уведомления о нём прочитаны;
• уведомление никогда не роняет само действие: ошибка — только в лог.

Слово notification в адресах не используем: списки блокировщиков против
всплывающих уведомлений режут такие запросы. Адреса — /api/bell.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

# группа → подпись в настройках
GROUPS = {
    "items": "Новые материалы в делах",
    "access": "Доступ к делам и отчётам",
    "inbox": "Ответы на обращения",
}
KIND_GROUP = {
    "case_items": "items",
    "case_added": "access", "case_role": "access", "case_removed": "access",
    "case_owner": "access", "case_left": "access", "case_deleted": "access",
    "case_restored": "access", "report_shared": "access",
    "ticket": "inbox",
}
# эти склеиваются, пока не прочитаны (остальные — по одной строке на событие)
_MERGE = {"case_items", "ticket", "report_shared"}
KEEP_DAYS = 90
LIST_LIMIT = 60

_purged_at = 0.0


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params or {}).mappings().all()]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _q(s: Any) -> str:
    s = " ".join(str(s or "").split())
    return s[:90] + "…" if len(s) > 90 else s


def title_of(kind: str, ref: dict, count: int = 1) -> str:
    """Заголовок — само событие, без рода: кто сделал, видно строкой ниже."""
    case = _q(ref.get("case"))
    if kind == "case_items":
        if count <= 1:
            return f"В деле «{case}» новый материал"
        return f"В деле «{case}» {count} {_plural(count, 'новый материал', 'новых материала', 'новых материалов')}"
    if kind == "case_added":
        return f"Вас добавили в дело «{case}»"
    if kind == "case_role":
        return f"Ваша роль в деле «{case}»: {ref.get('role_label') or 'изменена'}"
    if kind == "case_removed":
        return f"Вас убрали из дела «{case}»"
    if kind == "case_owner":
        return f"Вам передали дело «{case}» — теперь вы владелец"
    if kind == "case_left":
        return f"Выход из дела «{case}»"
    if kind == "case_deleted":
        return f"Дело «{case}» удалено"
    if kind == "case_restored":
        return f"Дело «{case}» снова доступно"
    if kind == "report_shared":
        return f"С вами поделились отчётом «{_q(ref.get('report'))}»"
    if kind == "ticket":
        no = ref.get("no")
        if ref.get("reply"):
            return f"Команда AuditLens ответила на обращение № {no}"
        return f"Обращение № {no}: {ref.get('status_label') or 'новый статус'}"
    return _q(ref.get("title")) or "Новое событие"


def _muted(usernames: list[str], group: str) -> set[str]:
    rows = _rows("""SELECT username FROM app_user
                     WHERE username = ANY(:u)
                       AND COALESCE(prefs->'notify_off', '[]'::jsonb) ? :g""",
                 {"u": usernames, "g": group})
    return {r["username"] for r in rows}


def notify(usernames: Iterable[str | None], kind: str, *, actor: str | None = None,
           link: str | None = None, ref: dict | None = None, n: int = 1) -> int:
    """Записать уведомление каждому получателю. Возвращает, скольким дошло.
    Никогда не бросает: уведомление — не повод сорвать само действие."""
    try:
        users = sorted({u for u in usernames if u and u != actor})
        if not users or n <= 0:
            return 0
        users = [u for u in users if u not in _muted(users, KIND_GROUP.get(kind, "access"))]
        ref = dict(ref or {})
        sent = 0
        with db.session() as s:
            for u in users:
                prev = None
                if kind in _MERGE:
                    prev = s.execute(text("""
                        SELECT notice_id, count, ref FROM app_notice
                         WHERE username = :u AND kind = :k AND read_at IS NULL
                           AND link IS NOT DISTINCT FROM :l
                           AND (:k <> 'case_items' OR actor IS NOT DISTINCT FROM :a)
                         ORDER BY updated_at DESC LIMIT 1"""),
                        {"u": u, "k": kind, "l": link, "a": actor}).mappings().first()
                if prev:
                    cnt = int(prev["count"]) + (n if kind == "case_items" else 0)
                    merged = {**(prev["ref"] or {}), **ref}
                    if kind == "ticket" and (prev["ref"] or {}).get("reply"):
                        merged["reply"] = True       # ответ важнее смены статуса
                    s.execute(text("""
                        UPDATE app_notice SET count = :c, ref = CAST(:r AS jsonb), title = :t,
                                              actor = :a, updated_at = now()
                         WHERE notice_id = :id"""),
                        {"c": cnt, "r": json.dumps(merged, ensure_ascii=False),
                         "t": title_of(kind, merged, cnt), "a": actor, "id": prev["notice_id"]})
                else:
                    s.execute(text("""
                        INSERT INTO app_notice (username, kind, title, actor, link, ref, count)
                        VALUES (:u, :k, :t, :a, :l, CAST(:r AS jsonb), :c)"""),
                        {"u": u, "k": kind, "t": title_of(kind, ref, n), "a": actor, "l": link,
                         "r": json.dumps(ref, ensure_ascii=False), "c": n})
                sent += 1
        return sent
    except Exception:  # noqa: BLE001
        log.warning("[bell] notify %s failed", kind, exc_info=True)
        return 0


def _purge() -> None:
    """Старше 90 дней — насовсем (лениво, не чаще раза в час)."""
    global _purged_at
    if time.time() - _purged_at < 3600:
        return
    _purged_at = time.time()
    try:
        with db.session() as s:
            s.execute(text(f"DELETE FROM app_notice WHERE updated_at < now() - interval '{KEEP_DAYS} days'"))
    except Exception:  # noqa: BLE001
        log.debug("[bell] purge failed", exc_info=True)


def _iso(r: dict) -> dict:
    for k in ("created_at", "updated_at", "read_at"):
        if r.get(k) is not None:
            r[k] = r[k].isoformat()
    return r


def items(username: str, limit: int = LIST_LIMIT) -> list[dict]:
    _purge()
    rows = _rows("""
        SELECT n.notice_id AS id, n.kind, n.title, n.actor,
               COALESCE(au.display_name, n.actor) AS actor_name,
               n.link, n.ref, n.count, n.created_at, n.updated_at, n.read_at
          FROM app_notice n LEFT JOIN app_user au ON au.username = n.actor
         WHERE n.username = :u
         ORDER BY n.updated_at DESC, n.notice_id DESC LIMIT :lim""",
                 {"u": username, "lim": limit})
    for r in rows:
        r["group"] = KIND_GROUP.get(r["kind"], "access")
        if r["kind"] == "ticket":
            r["actor_name"] = "Команда AuditLens"
        _iso(r)
    return rows


def unread(username: str) -> dict:
    """Сколько непрочитанных и самое свежее — для точки на колокольчике и
    разовой заметки о новом."""
    rows = _rows("""
        SELECT n.notice_id AS id, n.kind, n.title, n.link, n.updated_at,
               COALESCE(au.display_name, n.actor) AS actor_name,
               count(*) OVER () AS n
          FROM app_notice n LEFT JOIN app_user au ON au.username = n.actor
         WHERE n.username = :u AND n.read_at IS NULL
         ORDER BY n.updated_at DESC, n.notice_id DESC LIMIT 1""", {"u": username})
    if not rows:
        return {"unread": 0, "last": None}
    r = _iso(rows[0])
    if r["kind"] == "ticket":
        r["actor_name"] = "Команда AuditLens"
    return {"unread": int(r.pop("n")), "last": r}


def mark_read(username: str, ids: list[int] | None = None, *, everything: bool = False,
              link: str | None = None) -> int:
    if not (ids or everything or link):
        return 0
    cond, p = "username = :u AND read_at IS NULL", {"u": username}
    if link:
        cond += " AND link = :l"
        p["l"] = link
    elif not everything:
        cond += " AND notice_id = ANY(:ids)"
        p["ids"] = [int(i) for i in ids or []][:500]
    try:
        with db.session() as s:
            return s.execute(text(f"UPDATE app_notice SET read_at = now() WHERE {cond}"), p).rowcount
    except Exception:  # noqa: BLE001 — открыть дело можно и без отметки
        log.debug("[bell] mark_read failed", exc_info=True)
        return 0


def settings(prefs: dict | None) -> list[dict]:
    off = set((prefs or {}).get("notify_off") or [])
    return [{"key": k, "label": v, "on": k not in off} for k, v in GROUPS.items()]
