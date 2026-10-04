"""Уведомления в приложении (миграция 083).

Колокольчик рядом с карточкой пользователя внизу меню: верхнюю панель не
трогаем — там и так тема, «Аудит-дела» и поиск. Сюда сходятся события, о
которых человек иначе не узнает: его добавили в дело или сменили роль,
коллега приобщил материалы, с ним поделились отчётом, команда ответила на
обращение. Письма о тех же событиях — web/mail_delivery.py (если человек подключил почту).

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
    "mention": "Упоминания и ответы вам",
    "talk": "Сообщения в обсуждениях дел",
    "items": "Новые материалы и разбор в делах",
    "access": "Доступ, статус дел и отчёты",
    "inbox": "Ответы на обращения",
}
KIND_GROUP = {
    "case_mention": "mention", "case_reply": "mention",
    "case_msg": "talk",
    "case_items": "items", "case_analysis": "items",
    "case_added": "access", "case_role": "access", "case_removed": "access",
    "case_owner": "access", "case_left": "access", "case_deleted": "access",
    "case_restored": "access", "case_status": "access", "report_shared": "access",
    "ticket": "inbox",
}
# «Не следить за делом» глушит эти; упоминания, ответы и доступ — нет
CASE_MUTABLE = {"case_items", "case_msg", "case_status", "case_analysis"}
# эти склеиваются, пока не прочитаны (остальные — по одной строке на событие)
_MERGE = {"case_items", "case_msg", "case_status", "case_analysis", "ticket", "report_shared"}
_COUNTED = {"case_items", "case_msg"}          # склейка копит число
_SAME_ACTOR = {"case_items"}                   # склеиваются только от одного автора
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
        return f"Вас добавили в дело «{case}»" + (f" — команда «{_q(ref['team'])}»" if ref.get("team") else "")
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
    if kind == "case_msg":
        if count <= 1:
            return f"Новое сообщение в деле «{case}»"
        return f"В деле «{case}» {count} {_plural(count, 'новое сообщение', 'новых сообщения', 'новых сообщений')}"
    if kind == "case_mention":
        return f"Вас упомянули в деле «{case}»"
    if kind == "case_reply":
        if ref.get("on_item"):
            return f"Комментарий к вашему материалу в деле «{case}»"
        return f"Ответ на ваше сообщение в деле «{case}»"
    if kind == "case_status":
        if ref.get("archived") is True:
            return f"Дело «{case}» перенесено в архив"
        if ref.get("archived") is False:
            return f"Дело «{case}» возвращено из архива"
        return f"Статус дела «{case}»: {ref.get('status_label') or 'изменён'}"
    if kind == "case_analysis":
        return f"В деле «{case}» новый разбор ИИ"
    if kind == "report_shared":
        return f"С вами поделились отчётом «{_q(ref.get('report'))}»"
    if kind == "ticket":
        no = ref.get("no")
        if ref.get("reply"):
            return f"Команда AuditLens ответила на обращение № {no}"
        return f"Обращение № {no}: {ref.get('status_label') or 'новый статус'}"
    return _q(ref.get("title")) or "Новое событие"


def talk_targets(author: str, participants: list[str], mentions: list[str],
                 reply_author: str | None = None, item_author: str | None = None) -> list[tuple]:
    """Кому что прислать о новом сообщении в деле — каждому одно уведомление,
    самое личное: упомянули → «вас упомянули»; ответили на ваше сообщение →
    «ответ»; прокомментировали ваш материал → «комментарий к вашему материалу»;
    остальным участникам → «новые сообщения» (склеиваются). Автору — ничего."""
    told, out = {author}, []
    ments = [u for u in dict.fromkeys(mentions or []) if u not in told]
    if ments:
        out.append(("case_mention", ments, {}))
        told.update(ments)
    if reply_author and reply_author not in told:
        out.append(("case_reply", [reply_author], {}))
        told.add(reply_author)
    if item_author and item_author not in told:
        out.append(("case_reply", [item_author], {"on_item": True}))
        told.add(item_author)
    rest = [u for u in dict.fromkeys(participants or []) if u not in told]
    if rest:
        out.append(("case_msg", rest, {}))
    return out


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
                           AND link IS NOT DISTINCT FROM CAST(:l AS text)
                           AND (NOT :same OR actor IS NOT DISTINCT FROM CAST(:a AS text))
                         ORDER BY updated_at DESC LIMIT 1"""),
                        {"u": u, "k": kind, "l": link, "a": actor,
                         "same": kind in _SAME_ACTOR}).mappings().first()
                if prev:
                    cnt = int(prev["count"]) + (n if kind in _COUNTED else 0)
                    merged = {**(prev["ref"] or {}), **ref}
                    if kind == "ticket" and (prev["ref"] or {}).get("reply"):
                        merged["reply"] = True       # ответ важнее смены статуса
                    s.execute(text("""
                        UPDATE app_notice SET count = :c, ref = CAST(:r AS jsonb), title = :t,
                                              actor = :a, updated_at = now(), emailed_at = NULL
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
        SELECT n.notice_id AS id, n.kind, n.title, n.link, n.updated_at, n.ref,
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
              link: str | None = None, prefix: bool = False) -> int:
    """prefix=True — и все вложенные ссылки: «case:12:talk» гасит «case:12:talk:55»."""
    if not (ids or everything or link):
        return 0
    cond, p = "username = :u AND read_at IS NULL", {"u": username}
    if link and prefix:
        cond += " AND (link = :l OR link LIKE :lp)"
        p["l"], p["lp"] = link, link.replace("%", "").replace("_", r"\_") + ":%"
    elif link:
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


# ── письма (миграция 087): что ещё не отправлено и не прочитано ────────────────

def pending_mail(username: str, kinds: set[str] | None = None, limit: int = 40) -> list[dict]:
    """Непрочитанные уведомления, о которых ещё не писали письмом (новые сверху).
    kinds — только личные для мгновенных писем; None — всё для утренней сводки."""
    try:
        rows = _rows("""
            SELECT n.notice_id AS id, n.kind, n.title, n.actor,
                   COALESCE(au.display_name, n.actor) AS actor_name,
                   n.link, n.ref, n.count, n.created_at, n.updated_at
              FROM app_notice n LEFT JOIN app_user au ON au.username = n.actor
             WHERE n.username = :u AND n.read_at IS NULL AND n.emailed_at IS NULL
             ORDER BY n.updated_at DESC LIMIT :lim""", {"u": username, "lim": limit})
    except Exception:  # noqa: BLE001 — до миграции 087
        return []
    for r in rows:
        if r["kind"] == "ticket":
            r["actor_name"] = "Команда AuditLens"
    return [r for r in rows if kinds is None or r["kind"] in kinds]


def mark_emailed(ids: list[int]) -> int:
    if not ids:
        return 0
    with db.session() as s:
        return s.execute(text("UPDATE app_notice SET emailed_at = now() WHERE notice_id = ANY(:i)"),
                         {"i": [int(i) for i in ids]}).rowcount
