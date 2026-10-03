"""Слой данных персонализации: пользователи, история чатов/отчётов, шеринг,
события и профиль интересов, персональный дайджест.

Весь SQL — через sqlalchemy.text() поверх db.session() (коммит на выходе).
Схема auditlens (search_path на роли). См. migrations/014_personalization.sql
и docs/PERSONALIZATION_PLAN.md. Модуль «Лазейки» имеет свой слой — не пересекается.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params or {}).mappings().all()]


def _one(sql: str, params: dict | None = None) -> dict | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _scalar(sql: str, params: dict | None = None) -> Any:
    with db.session() as s:
        return s.execute(text(sql), params or {}).scalar_one_or_none()


# ── Пользователь ──────────────────────────────────────────────────────────────

def touch_user(username: str, display_name: str | None = None,
               timezone: str | None = None, email: str | None = None) -> dict | None:
    """Upsert пользователя на каждом запросе: обновляет last_seen, имя, TZ.

    display_name/timezone обновляются только если переданы непустыми.
    """
    if not username:
        return None
    with db.session() as s:
        s.execute(text("""
            INSERT INTO app_user (username, display_name, last_seen_at)
            VALUES (:u, :n, now())
            ON CONFLICT (username) DO UPDATE
               SET last_seen_at = now(),
                   display_name = COALESCE(NULLIF(:n, ''), app_user.display_name)
        """), {"u": username, "n": display_name or ""})
        if timezone:
            s.execute(text(
                "UPDATE app_user SET timezone = :tz WHERE username = :u"
            ), {"tz": timezone, "u": username})
    if email:
        # почта из системы входа (X-Authentik-Email) — для писем-уведомлений. Адрес,
        # который человек указал сам ('user') или отключил ('off'), не перезаписываем.
        # Отдельной транзакцией: до миграций 087–088 колонок нет, а вход не должен падать
        try:
            with db.session() as s:
                s.execute(text("""UPDATE app_user SET email = :e, email_at = now(), email_source = 'sso'
                                   WHERE username = :u AND email IS DISTINCT FROM :e
                                     AND COALESCE(email_source, 'sso') = 'sso'"""),
                          {"e": email, "u": username})
        except Exception:  # noqa: BLE001
            log.debug("touch_user: почта не сохранилась", exc_info=True)
    return get_user(username)


def get_user(username: str) -> dict | None:
    return _one("""SELECT username, display_name, timezone, prefs, interests,
                          profile_note, profile_note_at, created_at, last_seen_at
                   FROM app_user WHERE username = :u""", {"u": username})


def update_prefs(username: str, patch: dict) -> None:
    """Мержит patch в app_user.prefs (jsonb ||)."""
    import json
    with db.session() as s:
        s.execute(text(
            "UPDATE app_user SET prefs = prefs || CAST(:p AS jsonb) WHERE username = :u"
        ), {"p": json.dumps(patch, ensure_ascii=False), "u": username})


def set_timezone(username: str, tz: str) -> None:
    with db.session() as s:
        s.execute(text("UPDATE app_user SET timezone = :tz WHERE username = :u"),
                  {"tz": tz, "u": username})


def list_users(exclude: str | None = None) -> list[dict]:
    """Директория пользователей инструмента (для шеринга) — все, кто заходил.
    Служебные учётки (помечены в «Пульсе») сюда не попадают; владелец
    инструмента помечен там же, но он живой человек — его оставляем."""
    owners = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}
    rows = _rows("""SELECT username, display_name, last_seen_at,
                           COALESCE(prefs->>'pulse_hidden', '') = 'true' AS hidden
                    FROM app_user ORDER BY last_seen_at DESC""")
    out = []
    for r in rows:
        if r["username"] == exclude or (r.pop("hidden") and r["username"] not in owners):
            continue
        out.append(r)
    return out


# ── Сессии и сообщения чата ──────────────────────────────────────────────────

def _title_from_question(q: str) -> str:
    q = " ".join((q or "").split())
    return q[:80] if q else "Без названия"


def get_or_create_session(username: str, session_id: int | None,
                          first_question: str) -> int:
    """Возвращает session_id: существующую (если принадлежит юзеру) или новую."""
    if session_id:
        owner = _scalar("SELECT username FROM chat_session WHERE session_id = :s",
                        {"s": session_id})
        if owner == username:
            return int(session_id)
    return int(_scalar("""
        INSERT INTO chat_session (username, title)
        VALUES (:u, :t) RETURNING session_id
    """, {"u": username, "t": _title_from_question(first_question)}))


def add_message(session_id: int, role: str, content: str,
                meta: dict | None = None) -> int:
    import json
    mid = _scalar("""INSERT INTO chat_message (session_id, role, content, meta)
                     VALUES (:s, :r, :c, CAST(:m AS jsonb)) RETURNING message_id""",
                  {"s": session_id, "r": role, "c": content or "",
                   "m": json.dumps(meta or {}, ensure_ascii=False, default=str)})
    with db.session() as s:
        s.execute(text("UPDATE chat_session SET updated_at = now() WHERE session_id = :s"),
                  {"s": session_id})
    return int(mid)


def list_sessions(username: str, limit: int = 100) -> list[dict]:
    """Сессии пользователя с превью последнего сообщения (для drawer истории).

    first_q / n_answers / report_id — для стартовой страницы аналитика: склеить
    повторы одного вопроса (15% сессий — тот же вопрос в течение суток), показать,
    есть ли ответ или отчёт, и подсказать «вы уже спрашивали» при наборе."""
    return _rows("""
        SELECT cs.session_id, cs.title, cs.pinned, cs.created_at, cs.updated_at,
               (SELECT content FROM chat_message cm
                 WHERE cm.session_id = cs.session_id
                 ORDER BY cm.created_at DESC LIMIT 1) AS last_preview,
               (SELECT count(*) FROM chat_message cm
                 WHERE cm.session_id = cs.session_id) AS n_messages,
               (SELECT left(content, 300) FROM chat_message cm
                 WHERE cm.session_id = cs.session_id AND cm.role = 'user'
                 ORDER BY cm.created_at LIMIT 1) AS first_q,
               (SELECT count(*) FROM chat_message cm
                 WHERE cm.session_id = cs.session_id AND cm.role = 'assistant') AS n_answers,
               (SELECT cm.meta->>'report_id' FROM chat_message cm
                 WHERE cm.session_id = cs.session_id AND cm.role = 'assistant'
                   AND cm.meta ? 'report_id'
                 ORDER BY cm.created_at DESC LIMIT 1) AS report_id
        FROM chat_session cs
        WHERE cs.username = :u
        ORDER BY cs.pinned DESC, cs.updated_at DESC
        LIMIT :lim
    """, {"u": username, "lim": limit})


def get_session_messages(session_id: int, username: str) -> list[dict] | None:
    """Сообщения сессии (с проверкой владельца). None если не его сессия."""
    owner = _scalar("SELECT username FROM chat_session WHERE session_id = :s",
                    {"s": session_id})
    if owner != username:
        return None
    return _rows("""SELECT message_id, role, content, meta, created_at
                    FROM chat_message WHERE session_id = :s ORDER BY created_at""",
                 {"s": session_id})


def rename_session(session_id: int, username: str, title: str) -> bool:
    with db.session() as s:
        res = s.execute(text(
            "UPDATE chat_session SET title = :t WHERE session_id = :s AND username = :u"
        ), {"t": title[:120], "s": session_id, "u": username})
        return res.rowcount > 0


def pin_session(session_id: int, username: str, pinned: bool) -> bool:
    with db.session() as s:
        res = s.execute(text(
            "UPDATE chat_session SET pinned = :p WHERE session_id = :s AND username = :u"
        ), {"p": pinned, "s": session_id, "u": username})
        return res.rowcount > 0


def delete_session(session_id: int, username: str) -> bool:
    with db.session() as s:
        owner = s.execute(text("SELECT username FROM chat_session WHERE session_id = :s"),
                          {"s": session_id}).scalar_one_or_none()
        if owner != username:
            return False
        s.execute(text("DELETE FROM chat_message WHERE session_id = :s"), {"s": session_id})
        s.execute(text("DELETE FROM chat_session WHERE session_id = :s"), {"s": session_id})
        return True


# ── Отчёты ────────────────────────────────────────────────────────────────────

def save_report(username: str, session_id: int | None, question: str,
                body: str, payload: dict | None = None,
                banks: list[str] | None = None, title: str | None = None) -> int:
    import json
    return int(_scalar("""
        INSERT INTO report (username, session_id, question, title, body, payload, banks)
        VALUES (:u, :s, :q, :t, :b, CAST(:p AS jsonb), :banks)
        RETURNING report_id
    """, {"u": username, "s": session_id, "q": question,
          "t": title or _title_from_question(question), "b": body or "",
          "p": json.dumps(payload or {}, ensure_ascii=False, default=str),
          "banks": banks or []}))


def set_report_title(report_id: int, title: str) -> None:
    """Название отчёта, составленное после сохранения (быстрая модель в фоне)."""
    if not title:
        return
    with db.session() as s:
        s.execute(text("UPDATE report SET title = :t WHERE report_id = :r"),
                  {"t": title[:200], "r": report_id})


def title_session_from_report(session_id: int | None, title: str) -> bool:
    """Название беседы — по первому отчёту, если его ещё никто не менял:
    до 29.09 беседа называлась первым вопросом («да, сведи в таблицу»).
    Переименованную вручную беседу не трогаем."""
    if not session_id or not title:
        return False
    row = _one("""SELECT s.title,
                         (SELECT m.content FROM chat_message m
                           WHERE m.session_id = s.session_id AND m.role = 'user'
                           ORDER BY m.created_at LIMIT 1) AS first_q
                    FROM chat_session s WHERE s.session_id = :s""", {"s": session_id})
    if not row or row.get("title") != _title_from_question(row.get("first_q") or ""):
        return False
    with db.session() as s:
        s.execute(text("UPDATE chat_session SET title = :t WHERE session_id = :s"),
                  {"t": title[:200], "s": session_id})
    return True


def session_questions(session_id: int | None, limit: int = 8) -> list[str]:
    """Вопросы беседы по порядку — контекст для названия уточняющего отчёта."""
    if not session_id:
        return []
    rows = _rows("""SELECT content FROM chat_message
                    WHERE session_id = :s AND role = 'user'
                    ORDER BY created_at LIMIT :n""", {"s": session_id, "n": limit})
    return [r["content"] for r in rows if r.get("content")]


def count_reports(username: str) -> int:
    return int(_scalar("SELECT count(*) FROM report WHERE username = :u", {"u": username}) or 0)


def list_reports(username: str, limit: int = 100) -> list[dict]:
    return _rows("""SELECT report_id, session_id, question, title, banks, created_at,
                           left(body, 240) AS preview
                    FROM report WHERE username = :u
                    ORDER BY created_at DESC LIMIT :lim""",
                 {"u": username, "lim": limit})


def report_access(report_id: int, username: str) -> bool:
    """Доступ: владелец ИЛИ отчёт расшарен ему лично ИЛИ всем (shared_with IS NULL)
    ИЛИ отчёт приобщён к делу, где он участник: в общем деле отчёт открывают
    «через дело», иначе коллега видел бы карточку, а открыть не мог."""
    owner = _scalar("SELECT username FROM report WHERE report_id = :r", {"r": report_id})
    if owner == username:
        return True
    n = _scalar("""SELECT count(*) FROM report_share
                   WHERE report_id = :r AND revoked_at IS NULL
                     AND (shared_with = :u OR shared_with IS NULL)""",
                {"r": report_id, "u": username})
    if n:
        return True
    try:
        return bool(_scalar("""
            SELECT 1 FROM audit_case_item i JOIN audit_case c ON c.case_id = i.case_id
             WHERE i.kind = 'report' AND i.ref_id = :r AND c.deleted_at IS NULL
               AND (c.username = :u OR EXISTS (SELECT 1 FROM audit_case_member m
                                                WHERE m.case_id = c.case_id AND m.username = :u))
             LIMIT 1""", {"r": report_id, "u": username}))
    except Exception:  # noqa: BLE001
        return False


def get_report(report_id: int, username: str, as_admin: bool = False) -> dict | None:
    """as_admin — служебный доступ владельца инструмента к ЧУЖОМУ отчёту.

    Без него жалобу «отчёты плохие» невозможно разобрать: видно, что человек
    недоволен, а чем именно — нет. Каждое такое открытие пишется в след
    (см. /api/reports/{id} → admin_report_open), чтобы доступ был не тихим.
    """
    if not as_admin and not report_access(report_id, username):
        return None
    return _one("""SELECT r.report_id, r.username AS owner, r.session_id, r.question,
                          r.title, r.body, r.payload, r.banks, r.created_at,
                          au.display_name AS owner_name
                   FROM report r LEFT JOIN app_user au ON au.username = r.username
                   WHERE r.report_id = :r""", {"r": report_id})


def delete_report(report_id: int, username: str) -> bool:
    with db.session() as s:
        res = s.execute(text(
            "DELETE FROM report WHERE report_id = :r AND username = :u"
        ), {"r": report_id, "u": username})
        return res.rowcount > 0


# ── Шеринг ────────────────────────────────────────────────────────────────────

def share_report(report_id: int, owner: str, shared_with: str | None) -> int | None:
    """Расшарить отчёт (только владелец). shared_with=None → всем пользователям."""
    real_owner = _scalar("SELECT username FROM report WHERE report_id = :r",
                         {"r": report_id})
    if real_owner != owner:
        return None
    # Идемпотентность: не плодим дубли той же выдачи.
    existing = _scalar("""SELECT share_id FROM report_share
                          WHERE report_id = :r AND owner = :o AND revoked_at IS NULL
                            AND shared_with IS NOT DISTINCT FROM :w""",
                       {"r": report_id, "o": owner, "w": shared_with})
    if existing:
        return int(existing)
    return int(_scalar("""INSERT INTO report_share (report_id, owner, shared_with)
                          VALUES (:r, :o, :w) RETURNING share_id""",
                       {"r": report_id, "o": owner, "w": shared_with}))


def report_title(report_id: int) -> str:
    return _scalar("SELECT COALESCE(NULLIF(title, ''), question) FROM report WHERE report_id = :r",
                   {"r": report_id}) or "отчёт"


def list_shared_with_me(username: str) -> list[dict]:
    return _rows("""
        SELECT DISTINCT ON (r.report_id)
               r.report_id, r.question, r.title, r.banks, r.created_at,
               r.username AS owner, au.display_name AS owner_name, rs.created_at AS shared_at
        FROM report_share rs
        JOIN report r ON r.report_id = rs.report_id
        LEFT JOIN app_user au ON au.username = r.username
        WHERE rs.revoked_at IS NULL
          AND (rs.shared_with = :u OR rs.shared_with IS NULL)
          AND r.username <> :u
        ORDER BY r.report_id, rs.created_at DESC
    """, {"u": username})


def list_report_shares(report_id: int, owner: str) -> list[dict]:
    return _rows("""SELECT rs.share_id, rs.shared_with, rs.created_at,
                           au.display_name AS with_name
                    FROM report_share rs
                    LEFT JOIN app_user au ON au.username = rs.shared_with
                    WHERE rs.report_id = :r AND rs.owner = :o AND rs.revoked_at IS NULL
                    ORDER BY rs.created_at DESC""", {"r": report_id, "o": owner})


def revoke_share(share_id: int, owner: str) -> bool:
    with db.session() as s:
        res = s.execute(text(
            "UPDATE report_share SET revoked_at = now() WHERE share_id = :s AND owner = :o AND revoked_at IS NULL"
        ), {"s": share_id, "o": owner})
        return res.rowcount > 0


# ── События + профиль интересов ──────────────────────────────────────────────

def log_event(username: str, kind: str, payload: dict | None = None) -> None:
    import json
    try:
        with db.session() as s:
            s.execute(text("""INSERT INTO user_event (username, kind, payload)
                              VALUES (:u, :k, CAST(:p AS jsonb))"""),
                      {"u": username, "k": kind,
                       "p": json.dumps(payload or {}, ensure_ascii=False, default=str)})
    except Exception:
        log.warning("[userdata] log_event failed", exc_info=True)


# Продуктовые ключевые слова → канонический слаг (для профиля интересов).
_PRODUCT_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ипотек", re.I), "ipoteka"),
    (re.compile(r"вклад|депозит", re.I), "deposit"),
    (re.compile(r"кредитн\w* карт|кредитк", re.I), "credit_card"),
    (re.compile(r"дебетов\w* карт|дебетовк", re.I), "debit_card"),
    (re.compile(r"потребит\w* кред|кредит наличн|наличными", re.I), "consumer_loan"),
    (re.compile(r"автокредит|авто[- ]?кредит", re.I), "auto"),
    (re.compile(r"\bрко\b|расчётн\w* счёт|расчетн", re.I), "rko"),
    (re.compile(r"накопит\w* счёт|накопительн", re.I), "savings"),
    (re.compile(r"эквайринг", re.I), "acquiring"),
    (re.compile(r"премиальн|private|прайм", re.I), "premium"),
    # именные формы: голое «перевод» матчило «переводить экономику на военные
    # рельсы» — глагольный мусор получал вес продукта (замер 05.08.2026)
    (re.compile(r"перевод(?:[аеуы]|ов|ам|ами|ах|ом)?\b|сбп\b|\bкомисси", re.I), "transfers"),
]


def parse_query_signals(question: str) -> dict:
    """Детерминированный разбор запроса: банки + продукты (0 LLM)."""
    from ..ai.llm_utils import detect_bank_slugs
    banks = list(detect_bank_slugs(question or ""))
    products = [slug for rx, slug in _PRODUCT_KEYWORDS if rx.search(question or "")]
    return {"banks": banks, "products": products}


# Измерения аудита (dimension новостных источников: compliance/ops/fraud/market
# + conduct на вырост). Выводятся из свободного текста профиля детерминированно —
# ровно так же объяснимо, как продуктовые ключи выше. До 05.08.2026 поле
# dimension размечалось на источниках и протаскивалось через весь пайплайн,
# но не читалось нигде — фрод-аудитор без слова «карта/вклад» в новости получал
# пустую сетку «Для вас» (замер: 0 из 40 позиций).
_DIMENSION_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"мошенн|фрод|фишинг|соц.?инженер|дроп\w|хищени|антифрод", re.I), "fraud"),
    (re.compile(r"комплаенс|под[\s/]?фт|115-?фз|отмыв|санкци|регулятор|надзор|лиценз", re.I), "compliance"),
    (re.compile(r"сбо[йяе]|инцидент|доступност|непрерывност|процессинг|операционн", re.I), "ops"),
    (re.compile(r"тариф|ставк|конкурент|рын[ок]|ценообраз|продуктов", re.I), "market"),
    (re.compile(r"мисселинг|навязыван|продаж|жалоб|обслуживан|клиентск", re.I), "conduct"),
]


def dimension_weights(text: str) -> dict[str, float]:
    """Веса измерений аудита из текста профиля (0 LLM, нормированы к max=1)."""
    if not (text or "").strip():
        return {}
    hits: dict[str, int] = {}
    for rx, dim in _DIMENSION_KEYWORDS:
        n = len(rx.findall(text))
        if n:
            hits[dim] = hits.get(dim, 0) + n
    if not hits:
        return {}
    mx = max(hits.values())
    return {d: round(n / mx, 2) for d, n in hits.items()}


_DECAY = 0.95  # затухание старого веса на каждый новый запрос


def _bump_interest_counters(username: str, signals: dict, weight: float = 1.0) -> None:
    """Ядро обучения счётчиков: затухание, взвешенное СИЛОЙ сигнала.

    weight — насколько событие говорит об интересе: вопрос ИИ-аналитику 1.0,
    клик по новости 0.5, фильтр вкладки 0.3. Затухание масштабируем той же
    силой (_DECAY в степени weight): иначе частая навигация с малым весом
    вымывала бы профиль, накопленный из редких сильных сигналов."""
    if not any(signals.get(d) for d in ("banks", "products")):
        return
    decay = _DECAY ** weight
    try:
        import json
        interests = _load_interests(username)
        counters = interests.get("counters") or {"banks": {}, "products": {}}
        for dim in ("banks", "products"):
            bucket = counters.setdefault(dim, {})
            for k in list(bucket):
                bucket[k] = round(bucket[k] * decay, 4)
            for k in signals.get(dim, []):
                bucket[k] = round(bucket.get(k, 0.0) + weight, 4)
            counters[dim] = {k: v for k, v in bucket.items() if v >= 0.05}
        interests["counters"] = counters
        with db.session() as s:
            s.execute(text("UPDATE app_user SET interests = CAST(:i AS jsonb) WHERE username = :u"),
                      {"i": json.dumps(interests, ensure_ascii=False), "u": username})
    except Exception:
        log.warning("[userdata] bump_interests failed", exc_info=True)


def update_interests_from_query(username: str, question: str) -> dict:
    """Обновляет счётчики интересов из вопроса ИИ-аналитику. Возвращает signals."""
    signals = parse_query_signals(question)
    _bump_interest_counters(username, signals, weight=1.0)
    return signals


def update_interests_from_signal(username: str, *, text_: str = "",
                                 products: list[str] | None = None,
                                 banks: list[str] | None = None,
                                 weight: float = 0.3) -> None:
    """Обучение из ПОВЕДЕНИЯ (этап A, 05.08.2026): фильтры вкладок, клики по
    новостям, drill-ы. До этого профиль рос ТОЛЬКО от вопросов ИИ-аналитику —
    а «читатели» (сегмент из Пульса) не спрашивают ИИ вовсе, и их «Для вас»
    оставалась дефолтной навсегда. Best-effort, никогда не кидает."""
    try:
        signals = parse_query_signals(text_) if text_ else {"banks": [], "products": []}
        if products:
            signals["products"] = list(dict.fromkeys(signals["products"] + products))
        if banks:
            signals["banks"] = list(dict.fromkeys(signals["banks"] + banks))
        _bump_interest_counters(username, signals, weight=weight)
    except Exception:
        log.warning("[userdata] interests_from_signal failed", exc_info=True)


def _load_interests(username: str) -> dict:
    import json
    user = get_user(username) or {}
    interests = user.get("interests") or {}
    if isinstance(interests, str):
        interests = json.loads(interests or "{}")
    return interests


def top_interests(username: str, k: int = 6) -> dict:
    """Профиль интересов: авто-темы (за вычетом заглушённых), закреплённые,
    заглушённые, ручные (custom) — для персон-дайджеста и страницы профиля."""
    interests = _load_interests(username)
    counters = interests.get("counters") or {}
    muted = set(interests.get("muted") or [])
    out = {}
    for dim in ("banks", "products"):
        items = sorted((counters.get(dim) or {}).items(), key=lambda x: -x[1])
        out[dim] = [name for name, _ in items[:k] if name not in muted]
    out["pinned"] = interests.get("pinned") or []
    out["muted"] = interests.get("muted") or []
    out["custom"] = interests.get("custom") or []
    return out


def set_interest_overrides(username: str, pinned: list[str] | None = None,
                           muted: list[str] | None = None,
                           custom: list[str] | None = None) -> None:
    import json
    interests = _load_interests(username)
    if pinned is not None:
        interests["pinned"] = [x for x in pinned if x][:40]
    if muted is not None:
        interests["muted"] = [x for x in muted if x][:40]
    if custom is not None:
        # ручные темы: тримим, дедуп, ограничиваем
        seen, out = set(), []
        for x in custom:
            t = str(x).strip()[:60]
            if t and t.lower() not in seen:
                seen.add(t.lower()); out.append(t)
        interests["custom"] = out[:30]
    with db.session() as s:
        s.execute(text("UPDATE app_user SET interests = CAST(:i AS jsonb) WHERE username = :u"),
                  {"i": json.dumps(interests, ensure_ascii=False), "u": username})


def interest_weight_profile(username: str) -> dict:
    """Весовой профиль тем для персонального дайджеста (Фаза 3):
    self_description ×3 + pinned ×2 + авто-счётчики ×1 − muted (исключить).
    custom (свободный текст) отдаём отдельно — матчим по вхождению в текст.
    """
    import json
    interests = _load_interests(username)
    user = get_user(username) or {}
    prefs = user.get("prefs") or {}
    if isinstance(prefs, str):
        prefs = json.loads(prefs or "{}")
    self_desc = (prefs.get("self_description") or "").strip()
    counters = interests.get("counters") or {}
    muted = set(interests.get("muted") or [])
    pinned = interests.get("pinned") or []
    custom = interests.get("custom") or []
    desc_sig = parse_query_signals(self_desc) if self_desc else {"banks": [], "products": []}

    weights: dict[str, float] = {}
    def _add(topic: str, w: float):
        if not topic or topic in muted:
            return
        weights[topic] = round(weights.get(topic, 0.0) + w, 3)

    for dim in ("banks", "products"):
        for t in desc_sig.get(dim, []):
            _add(t, 3.0)
        for t, c in (counters.get(dim) or {}).items():
            _add(t, min(float(c), 3.0) * 1.0)
    for t in pinned:
        _add(t, 2.0)
    # Явные оценки 👍/👎 — самый сильный сигнал (может и ослаблять тему до нуля).
    try:
        for t, w in reaction_profile(username)["topics"].items():
            if t not in muted:
                weights[t] = round(weights.get(t, 0.0) + w, 3)
    except Exception:
        log.warning("[userdata] reaction profile failed", exc_info=True)
    weights = {t: w for t, w in weights.items() if w > 0}
    # Сбер — якорь для всех: пользователи это аудиторы Сбербанка.
    if "sberbank" not in muted:
        weights["sberbank"] = max(weights.get("sberbank", 0.0), 2.5)
    taste = interests.get("taste") or {}
    return {"weights": weights, "self_desc": self_desc, "custom": custom,
            "pinned": list(pinned), "muted": list(muted),
            # нарратив профиля (LLM-выжимка запросов) и вектор вкуса — в
            # семантический слой «Для вас» (этап A; раньше оба не читались)
            "note": (user.get("profile_note") or "").strip(),
            "taste_pos": taste.get("pos"), "taste_neg": taste.get("neg")}


# Соседние продукты (для AI-рекомендаций «в фокус»).
_ADJACENT: dict[str, list[str]] = {
    "deposit": ["savings", "transfers"], "savings": ["deposit", "transfers"],
    "credit_card": ["debit_card", "consumer_loan", "transfers"],
    "debit_card": ["credit_card", "transfers"],
    "consumer_loan": ["credit_card", "ipoteka"],
    "ipoteka": ["consumer_loan", "auto"], "auto": ["consumer_loan", "ipoteka"],
    "rko": ["acquiring", "transfers"], "acquiring": ["rko", "transfers"],
    "transfers": ["credit_card", "deposit"], "premium": ["deposit", "debit_card"],
}
# Популярно у аудиторов розницы Сбера — для холодного старта.
_POPULAR = ["deposit", "credit_card", "ipoteka", "transfers", "acquiring"]


def recommend_topics(username: str, k: int = 3) -> list[str]:
    """AI-рекомендации продуктов «в фокус»: соседние к текущим + популярные."""
    prof = top_interests(username)
    have = set(prof.get("products") or []) | set(prof.get("pinned") or [])
    muted = set(prof.get("muted") or [])
    rec: list[str] = []
    for p in (prof.get("products") or []):
        for adj in _ADJACENT.get(p, []):
            if adj not in have and adj not in muted and adj not in rec:
                rec.append(adj)
    for p in _POPULAR:
        if len(rec) >= k:
            break
        if p not in have and p not in muted and p not in rec:
            rec.append(p)
    return rec[:k]


def set_profile_note(username: str, note: str) -> None:
    with db.session() as s:
        s.execute(text("""UPDATE app_user
                          SET profile_note = :n, profile_note_at = now()
                          WHERE username = :u"""), {"n": note, "u": username})


def recent_queries(username: str, limit: int = 20) -> list[str]:
    rows = _rows("""SELECT payload->>'question' AS q FROM user_event
                    WHERE username = :u AND kind = 'ai_query'
                      AND payload->>'question' IS NOT NULL
                    ORDER BY ts DESC LIMIT :lim""", {"u": username, "lim": limit})
    return [r["q"] for r in rows if r.get("q")]


# ── Персональный дайджест ─────────────────────────────────────────────────────

def get_personal_digest(username: str, local_date: date) -> dict | None:
    return _one("""SELECT payload, generated_at, llm_model FROM personal_digest
                   WHERE username = :u AND local_date = :d""",
                {"u": username, "d": local_date})


def save_personal_digest(username: str, local_date: date, payload: dict,
                         llm_model: str | None = None,
                         tokens_in: int | None = None,
                         tokens_out: int | None = None) -> None:
    import json
    with db.session() as s:
        s.execute(text("""
            INSERT INTO personal_digest (username, local_date, payload, llm_model, tokens_in, tokens_out)
            VALUES (:u, :d, CAST(:p AS jsonb), :m, :ti, :to)
            ON CONFLICT (username, local_date) DO UPDATE
               SET payload = EXCLUDED.payload, generated_at = now(),
                   llm_model = EXCLUDED.llm_model,
                   tokens_in = EXCLUDED.tokens_in, tokens_out = EXCLUDED.tokens_out
        """), {"u": username, "d": local_date,
               "p": json.dumps(payload, ensure_ascii=False, default=str),
               "m": llm_model, "ti": tokens_in, "to": tokens_out})


def clear_personal_digest(username: str) -> None:
    """Сброс дневного кэша «Для вас» — после изменения профиля (описание/темы)
    следующий GET пересоберёт разворот уже под новый профиль, а не завтра."""
    with db.session() as s:
        s.execute(text("DELETE FROM personal_digest WHERE username = :u"),
                  {"u": username})


# ── Явные оценки 👍/👎 (миграция 015) ─────────────────────────────────────────
# Два контура: news/for_you/check учат ЕГО рекомендации; ai_answer идёт команде
# (разбор косяков ИИ-аналитика). События храним сырыми — профиль пересчитывается
# и остаётся объяснимым.

_FEEDBACK_CONTENT_KINDS = ("news", "for_you", "check")


def save_feedback(username: str, kind: str, item_key: str, verdict: int,
                  topics: list[str] | None = None,
                  payload: dict | None = None) -> dict:
    """Upsert оценки. Повторный клик тем же вердиктом (без reasons/comment) —
    снятие; с reasons/comment — обновление снапшота (детали дизлайка ИИ-ответа)."""
    import json
    meaningful = bool((payload or {}).get("reasons") or (payload or {}).get("comment"))
    with db.session() as s:
        row = s.execute(text("""SELECT verdict FROM item_feedback
                                WHERE username=:u AND kind=:k AND item_key=:i"""),
                        {"u": username, "k": kind, "i": item_key}).mappings().first()
        if row is not None and int(row["verdict"]) == verdict and not meaningful:
            s.execute(text("""DELETE FROM item_feedback
                              WHERE username=:u AND kind=:k AND item_key=:i"""),
                      {"u": username, "k": kind, "i": item_key})
            new_v = 0
        else:
            s.execute(text("""
                INSERT INTO item_feedback (username, kind, item_key, verdict, topics, payload)
                VALUES (:u, :k, :i, :v, CAST(:t AS jsonb), CAST(:p AS jsonb))
                ON CONFLICT (username, kind, item_key) DO UPDATE
                   SET verdict = EXCLUDED.verdict, topics = EXCLUDED.topics,
                       payload = EXCLUDED.payload, created_at = now()
            """), {"u": username, "k": kind, "i": item_key, "v": verdict,
                   "t": json.dumps(topics or [], ensure_ascii=False),
                   "p": json.dumps(payload or {}, ensure_ascii=False, default=str)})
            new_v = verdict
    n = _scalar("""SELECT count(*) FROM item_feedback
                   WHERE username=:u AND kind IN ('news','for_you','check')""",
                {"u": username}) or 0
    # вектор вкуса: лайк/дизлайк НОВОСТНОГО материала двигает центроиды
    if kind in ("news", "for_you") and new_v != 0:
        _update_taste(username, payload or {}, new_v)
    return {"verdict": new_v, "content_ratings": int(n)}


# ── вектор вкуса (этап A, 05.08.2026) ────────────────────────────────────────
# Раньше 👍 учил только продуктовые слаги плитки (лайк новости без слова
# «вклад/карта» давал лишь ±0.3 к ИСТОЧНИКУ), а семантическая нога ранка от
# оценок не училась вовсе. Теперь оценка двигает EMA-центроид эмбеддингов
# лайкнутого (pos) и дизлайкнутого (neg); ранк добавляет cos(pos)−cos(neg).
# Хранение — interests.taste (jsonb), альфа даёт «полураспад» ~10-15 оценок:
# при их сегодняшнем количестве (единицы) важнее отзывчивость, чем инерция.
_TASTE_ALPHA = float(os.getenv("TASTE_ALPHA", "0.2"))


def _update_taste(username: str, payload: dict, verdict: int) -> None:
    """EMA-обновление центроида вкуса. Best-effort: сбой ничего не ломает."""
    try:
        text_ = " ".join(str(payload.get(k) or "")
                         for k in ("title", "summary", "reason")).strip()
        if len(text_) < 12:
            return
        from ..rag import embedder
        vec = embedder.embed_one(text_[:400])
        import json
        interests = _load_interests(username)
        taste = interests.get("taste") or {}
        key = "pos" if verdict > 0 else "neg"
        cur = taste.get(key)
        if cur and len(cur) == len(vec):
            a = _TASTE_ALPHA
            mixed = [(1 - a) * c + a * v for c, v in zip(cur, vec)]
        else:
            mixed = list(vec)
        import math
        n = math.sqrt(sum(x * x for x in mixed)) or 1.0
        taste[key] = [round(x / n, 6) for x in mixed]
        taste[f"n_{key}"] = int(taste.get(f"n_{key}") or 0) + 1
        interests["taste"] = taste
        with db.session() as s:
            s.execute(text("UPDATE app_user SET interests = CAST(:i AS jsonb)"
                           " WHERE username = :u"),
                      {"i": json.dumps(interests, ensure_ascii=False), "u": username})
    except Exception:
        log.warning("[userdata] taste update failed", exc_info=True)


def recent_checks_taken(username: str, limit: int = 8) -> list[str]:
    """Зацепки «в работе» (kind=check_taken, 14 дн) — генератор не должен
    предлагать их заново как новые."""
    rows = _rows("""SELECT payload->>'title' AS t FROM item_feedback
                    WHERE username = :u AND kind = 'check_taken' AND verdict = 1
                      AND created_at > now() - interval '14 days'
                    ORDER BY created_at DESC LIMIT :n""",
                 {"u": username, "n": limit}) or []
    return [r["t"] for r in rows if r.get("t")]


def recent_check_dislikes(username: str, limit: int = 10) -> list[str]:
    """Заголовки недавно отклонённых зацепок — в промпт page_ai («не предлагай
    похожие»). До этого дизлайк зацепки никак не влиял на следующие генерации."""
    rows = _rows("""SELECT payload->>'title' AS t FROM item_feedback
                    WHERE username = :u AND kind = 'check' AND verdict = -1
                      AND created_at > now() - interval '30 days'
                    ORDER BY created_at DESC LIMIT :n""",
                 {"u": username, "n": limit}) or []
    return [r["t"] for r in rows if r.get("t")]


def feedback_map(username: str, kind: str) -> dict:
    """{item_key: verdict} — чтобы UI рендерил уже проставленные оценки."""
    rows = _rows("""SELECT item_key, verdict FROM item_feedback
                    WHERE username=:u AND kind=:k
                    ORDER BY created_at DESC LIMIT 500""",
                 {"u": username, "k": kind}) or []
    return {r["item_key"]: int(r["verdict"]) for r in rows}


def reaction_profile(username: str) -> dict:
    """Обучение на контентных оценках: веса тем (±) и источников (±),
    навсегда скрытые ключи. Экспоненциальное затухание, полураспад 30 дней —
    профиль живой, старые вкусы отмирают сами. Всё детерминированно/объяснимо."""
    import json
    from datetime import datetime, timezone
    rows = _rows("""SELECT item_key, verdict, topics, payload, created_at
                    FROM item_feedback
                    WHERE username=:u AND kind IN ('news','for_you','check')
                    ORDER BY created_at DESC LIMIT 400""", {"u": username}) or []
    topics_w: dict[str, float] = {}
    sources_w: dict[str, float] = {}
    disliked: set[str] = set()
    likes = dislikes = 0
    now = datetime.now(timezone.utc)
    for r in rows:
        v = int(r["verdict"])
        likes += v > 0
        dislikes += v < 0
        if v < 0 and r.get("item_key"):
            disliked.add(str(r["item_key"]))
        try:
            age_d = max((now - r["created_at"]).total_seconds() / 86400.0, 0.0)
        except Exception:
            age_d = 0.0
        decay = 0.5 ** (age_d / 30.0)
        ts = r.get("topics") or []
        if isinstance(ts, str):
            try:
                ts = json.loads(ts)
            except Exception:
                ts = []
        for t in ts:
            delta = (1.0 if v > 0 else -1.2) * decay
            topics_w[t] = max(-2.0, min(2.0, topics_w.get(t, 0.0) + delta))
        p = r.get("payload") or {}
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}
        src = p.get("source")
        if src:
            delta = (0.3 if v > 0 else -0.3) * decay
            sources_w[src] = max(-0.9, min(0.9, sources_w.get(src, 0.0) + delta))
    return {"topics": {t: round(w, 3) for t, w in topics_w.items() if abs(w) > 0.05},
            "sources": {s_: round(w, 3) for s_, w in sources_w.items() if abs(w) > 0.05},
            "disliked_keys": disliked,
            "n_likes": likes, "n_dislikes": dislikes, "n_total": len(rows)}


def personalization_score(username: str) -> dict:
    """«Сила персонализации» 0–100: детерминированная, объяснимая разбивка
    с CTA — пользователь точно видит, какое действие сколько даёт."""
    import json
    user = get_user(username) or {}
    prefs = user.get("prefs") or {}
    if isinstance(prefs, str):
        prefs = json.loads(prefs or "{}")
    desc = (prefs.get("self_description") or "").strip()
    ti = top_interests(username)
    # custom — темы, добавленные руками в «Добавить своё». Они влияют на выдачу
    # («Для вас» их учитывает) и показаны в том же блоке профиля, но в балл
    # не входили: человек добавлял темы, а счётчик не двигался.
    focus_n = len(set((ti.get("products") or []) + (ti.get("pinned") or [])
                      + (ti.get("custom") or [])))
    q_n = int(_scalar("""SELECT count(*) FROM user_event
                         WHERE username=:u AND kind='ai_query'""", {"u": username}) or 0)
    fb_n = int(_scalar("""SELECT count(*) FROM item_feedback
                          WHERE username=:u AND kind IN ('news','for_you','check')""",
                       {"u": username}) or 0)
    ai_n = int(_scalar("""SELECT count(*) FROM item_feedback
                          WHERE username=:u AND kind='ai_answer'""", {"u": username}) or 0)
    note = bool(user.get("profile_note"))

    parts: list[dict] = []

    def part(key, label, earned, mx, done, cta, target):
        parts.append({"key": key, "label": label, "earned": int(round(earned)),
                      "max": mx, "done": bool(done), "cta": cta, "target": target})

    part("desc", "Описание зоны ответственности", 25 * min(len(desc) / 40, 1), 25,
         len(desc) >= 40, "Опишите, что вы проверяете в Сбере", "profile")
    part("ratings", "5+ оценок в «Для вас»", 20 * min(fb_n / 5, 1), 20,
         fb_n >= 5, "Оцените публикации 👍/👎 на «Для вас»", "foryou")
    part("focus", "3+ темы в фокусе", 15 * min(focus_n / 3, 1), 15,
         focus_n >= 3, "Закрепите темы в профиле", "profile")
    part("queries", "5+ вопросов ИИ-помощнику", 15 * min(q_n / 5, 1), 15,
         q_n >= 5, "Спросите ИИ-помощника о своей теме", "ai")
    part("ai_ratings", "3+ оценки ответов ИИ", 10 * min(ai_n / 3, 1), 10,
         ai_n >= 3, "Оцените пару ответов ИИ-помощника", "ai")
    part("note", "ИИ-нарратив профиля собран", 10 if note else 0, 10,
         note, "Соберите профиль кнопкой «Пересобрать»", "profile")
    part("regular", "Регулярное использование", 5, 5, True, "", "")
    score = min(100, sum(p["earned"] for p in parts))
    return {"score": score, "parts": parts,
            "counts": {"queries": q_n, "content_ratings": fb_n, "ai_ratings": ai_n}}


def ai_feedback_stats(limit: int = 10) -> dict:
    """Для вкладки «Качество» (контур владельца): пульс оценок ИИ-ответов
    и последние дизлайки с причинами — сырьё для разбора косяков."""
    import json
    likes = int(_scalar("""SELECT count(*) FROM item_feedback
                           WHERE kind='ai_answer' AND verdict=1
                             AND created_at > now() - interval '7 days'""") or 0)
    dislikes = int(_scalar("""SELECT count(*) FROM item_feedback
                              WHERE kind='ai_answer' AND verdict=-1
                                AND created_at > now() - interval '7 days'""") or 0)
    rows = _rows("""SELECT username, payload, created_at FROM item_feedback
                    WHERE kind='ai_answer' AND verdict=-1
                    ORDER BY created_at DESC LIMIT :l""", {"l": limit}) or []
    out = []
    for r in rows:
        p = r.get("payload") or {}
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}
        out.append({"username": r.get("username"),
                    "question": (p.get("question") or "")[:200],
                    "reasons": p.get("reasons") or [],
                    "comment": (p.get("comment") or "")[:300],
                    "quote": (p.get("quote") or "")[:600],
                    "mode": p.get("mode"),
                    "created_at": str(r.get("created_at") or "")})
    return {"likes_7d": likes, "dislikes_7d": dislikes, "recent_dislikes": out}


# ── Аудит-дело: подборка доказательств ───────────────────────────────────────
#
# До этого «дело» жило в localStorage браузера: закрыл вкладку — подборка
# пропала, показать коллеге нечего, к проверке не приложить. Теперь на сервере,
# с приобщением документов из базы знаний и выгрузкой.

# Участники и роли (миграция 082). Владелец — audit_case.username: доступ,
# роли, передача владения, переименование, удаление. «Может добавлять»
# (editor) — приобщает материалы, убирает своё, пишет комментарий к своему,
# запускает разбор. «Только смотрит» (viewer) — видит и выгружает. Доступа
# «всем пользователям AuditLens» больше нет: дело было видно 111 людям.
CASE_ROLE_RU = {"owner": "владелец", "editor": "может добавлять", "viewer": "только смотрит"}
_CASE_TTL_DAYS = 30            # удалённое дело можно вернуть столько дней
# Статус меняет владелец (миграция 084). Архив — отдельно от статуса: дело
# только читается, в него не добавляют и не пишут, из выбора «В дело» оно уходит.
CASE_STATUS = {"collect": "Сбор материалов", "work": "В работе", "done": "Завершено"}
CASE_MSG_MAX = 4000


class CaseError(ValueError):
    """Отказ, текст которого показывается человеку как есть."""


def case_log(case_id: int, username: str | None, kind: str, payload: dict | None = None) -> None:
    """Строка истории дела. Отдельной транзакцией и без исключений: история —
    след действия, а не условие его успеха."""
    import json
    try:
        with db.session() as s:
            s.execute(text("""INSERT INTO audit_case_event (case_id, username, kind, payload)
                              VALUES (:c, :u, :k, CAST(:p AS jsonb))"""),
                      {"c": case_id, "u": username, "k": kind,
                       "p": json.dumps(payload or {}, ensure_ascii=False, default=str)})
    except Exception:  # noqa: BLE001
        log.warning("case %s: история (%s) не записалась", case_id, kind, exc_info=True)


def _case_flags(case_id: int, username: str) -> dict | None:
    """Роль, архив, статус и название живого (не удалённого) дела — одним запросом."""
    r = _one("""
        SELECT CASE WHEN c.username = :u THEN 'owner' ELSE m.role END AS role,
               c.archived_at IS NOT NULL AS archived, c.status, c.title, c.username AS owner
          FROM audit_case c
          LEFT JOIN audit_case_member m ON m.case_id = c.case_id AND m.username = :u
         WHERE c.case_id = :c AND c.deleted_at IS NULL""", {"c": case_id, "u": username})
    return r if r and r.get("role") else None


def case_role(case_id: int, username: str, *, deleted: bool = False) -> str | None:
    """owner | editor | viewer | None. Удалённое дело видно только владельцу
    (deleted=True — для «вернуть»)."""
    r = _scalar(f"""
        SELECT CASE WHEN c.username = :u THEN 'owner' ELSE m.role END
          FROM audit_case c
          LEFT JOIN audit_case_member m ON m.case_id = c.case_id AND m.username = :u
         WHERE c.case_id = :c
           AND {"c.deleted_at IS NOT NULL" if deleted else "c.deleted_at IS NULL"}""",
                {"c": case_id, "u": username})
    return r or None


def _purge_deleted_cases() -> None:
    """Удалённые больше 30 дней назад — насовсем (лениво, при открытии списка)."""
    try:
        with db.session() as s:
            s.execute(text(f"""DELETE FROM audit_case
                               WHERE deleted_at < now() - interval '{_CASE_TTL_DAYS} days'"""))
    except Exception:  # noqa: BLE001 — список дел открывается и без чистки
        log.debug("case purge failed", exc_info=True)


def list_cases(username: str) -> list[dict]:
    _purge_deleted_cases()
    rows = _rows(f"""
        SELECT c.case_id, c.title, c.note, c.created_at, c.updated_at, c.deleted_at,
               c.status, c.archived_at,
               (SELECT count(*) FROM audit_case_msg mm
                 WHERE mm.case_id = c.case_id AND mm.deleted_at IS NULL AND mm.username <> :u
                   AND mm.created_at > COALESCE(p.talk_seen_at, '-infinity')) talk_unread,
               (SELECT count(*) FROM audit_case_item i
                 WHERE i.case_id = c.case_id) items,
               (SELECT count(*) FROM audit_case_item i
                 WHERE i.case_id = c.case_id AND i.kind = 'review') reviews,
               (SELECT count(*) FROM audit_case_member m2 WHERE m2.case_id = c.case_id) members,
               (c.username = :u) AS mine, c.username AS owner,
               COALESCE(au.display_name, c.username) AS owner_name,
               CASE WHEN c.username = :u THEN 'owner' ELSE m.role END AS role
          FROM audit_case c
          LEFT JOIN audit_case_member m ON m.case_id = c.case_id AND m.username = :u
          LEFT JOIN audit_case_pref p ON p.case_id = c.case_id AND p.username = :u
          LEFT JOIN app_user au ON au.username = c.username
         WHERE (c.username = :u OR m.username IS NOT NULL)
           AND (c.deleted_at IS NULL
                OR (c.username = :u AND c.deleted_at > now() - interval '{_CASE_TTL_DAYS} days'))
         ORDER BY (c.deleted_at IS NOT NULL), (c.archived_at IS NOT NULL), c.updated_at DESC LIMIT 200
    """, {"u": username})
    for r in rows:
        r["deleted"] = r.get("deleted_at") is not None
        r["archived"] = r.get("archived_at") is not None
        r["status_label"] = CASE_STATUS.get(r.get("status") or "collect", "")
        r["shared"] = bool(r.get("members"))
        r["role_label"] = CASE_ROLE_RU.get(r.get("role"), "")
        r["can_add"] = r.get("role") in ("owner", "editor") and not r["deleted"] and not r["archived"]
    return rows


def create_case(username: str, title: str, note: str | None = None) -> int:
    cid = int(_scalar("""
        INSERT INTO audit_case (username, title, note)
        VALUES (:u, :t, :n) RETURNING case_id
    """, {"u": username, "t": title[:200], "n": (note or None)}))
    case_log(cid, username, "created", {"title": title[:200]})
    return cid


def _may_read_case(case_id: int, username: str) -> bool:
    return case_role(case_id, username) is not None


def _may_add_case(case_id: int, username: str) -> bool:
    """Приобщать, убирать своё, запускать разбор — владелец и «может добавлять»,
    и только пока дело не в архиве."""
    f = _case_flags(case_id, username)
    return bool(f) and f["role"] in ("owner", "editor") and not f["archived"]


def _owns_case(case_id: int, username: str) -> bool:
    return case_role(case_id, username) == "owner"


def get_case(case_id: int, username: str) -> dict | None:
    role = case_role(case_id, username)
    if not role:
        return None
    case = _one("""SELECT c.case_id, c.username AS owner, c.title, c.note,
                          c.created_at, c.updated_at, c.analysis, c.analysis_at, c.analysis_items,
                          c.analysis_item_ids,
                          c.status, c.status_at, c.archived_at,
                          COALESCE(au.display_name, c.username) AS owner_name,
                          COALESCE(p.muted, false) AS muted, p.talk_seen_at
                     FROM audit_case c LEFT JOIN app_user au ON au.username = c.username
                     LEFT JOIN audit_case_pref p ON p.case_id = c.case_id AND p.username = :u
                    WHERE c.case_id = :c""", {"c": case_id, "u": username})
    if not case:
        return None
    archived = case["archived_at"] is not None
    case["archived"] = archived
    case["status"] = case.get("status") or "collect"
    case["status_label"] = CASE_STATUS.get(case["status"], "")
    case["role"] = role
    case["role_label"] = CASE_ROLE_RU.get(role, "")
    case["mine"] = role == "owner"
    case["can_add"] = role in ("owner", "editor") and not archived
    case["can_edit"] = case["can_add"]           # прежнее имя поля — для старых вкладок
    case["can_manage"] = role == "owner"
    case["can_talk"] = not archived              # пишут все участники, включая «только смотрит»
    case["members"] = case_members(case_id, username) or []
    case["teams"] = case_teams(case_id)
    case["my_team"] = next((m.get("team_name") for m in case["members"]
                            if m["username"] == username and m.get("team_id")), None)
    case["shared"] = len(case["members"]) > 1
    # Документы подтягиваем свежими: доверие и дата обхода могли измениться
    # с момента приобщения, и в деле должно стоять актуальное состояние.
    case["items"] = _rows("""
        SELECT i.item_id, i.kind, i.ref_id, i.url, i.title, i.note, i.added_at, i.added_by, i.meta,
               d.trust_score, d.fetched_at, d.doc_type::text doc_type,
               b.name bank_name,
               r.title AS report_title, (i.kind = 'report' AND r.report_id IS NULL) AS report_gone
          FROM audit_case_item i
          LEFT JOIN document d ON d.document_id = i.ref_id AND i.kind = 'document'
          LEFT JOIN bank b ON b.bank_id = d.bank_id
          LEFT JOIN report r ON r.report_id = i.ref_id AND i.kind = 'report'
         WHERE i.case_id = :c ORDER BY i.added_at
    """, {"c": case_id})
    for it in case["items"]:
        it["meta"] = it.get("meta") or {}
        rt = it.pop("report_title", None)
        if it["kind"] == "report" and rt:
            it["title"] = rt                      # отчёт могли переименовать — название свежее
        if not it.get("report_gone"):
            it.pop("report_gone", None)
    _attach_review_items(case["items"])
    analysis_state(case)
    names = {m["username"]: m["name"] for m in case["members"]}
    talk = _talk_rows(case_id, username, role)
    by_item: dict[int, list[dict]] = {}
    for m in talk:
        if m.get("item_id"):
            by_item.setdefault(m["item_id"], []).append(m)
    for it in case["items"]:
        mine_item = it.get("added_by") == username
        it["added_by_name"] = names.get(it.get("added_by")) or _display_name(it.get("added_by"))
        # убрать — владелец или тот, кто приобщил (если у него ещё есть право добавлять)
        it["can_remove"] = (role == "owner" or (role == "editor" and mine_item)) and not archived
        it["can_note"] = False                    # одного «комментария» больше нет — лента
        it["comments"] = [m for m in by_item.get(it["item_id"], []) if not m.get("deleted")]
        # для выгрузок и разбора ИИ: комментарии лентой одной строкой
        it["note"] = "\n".join(f"{puname(m['name'])}: {m['body']}" for m in it["comments"]) or None
    seen = case.pop("talk_seen_at", None)
    case["talk_unread"] = sum(1 for m in talk if not m.get("deleted") and m["username"] != username
                              and (seen is None or m["created_at"] > seen))
    case["talk_n"] = sum(1 for m in talk if not m.get("deleted"))
    vers = _rows("""SELECT a.analysis_id, a.created_at, a.n_items, a.username,
                           COALESCE(au.display_name, a.username) AS name
                      FROM audit_case_analysis a LEFT JOIN app_user au ON au.username = a.username
                     WHERE a.case_id = :c ORDER BY a.created_at DESC LIMIT 20""", {"c": case_id})
    case["analysis_versions"] = vers
    if vers:
        case["analysis_by_name"] = vers[0]["name"]
    return case


def puname(name: str | None) -> str:
    """«Елена Волкова» → «Елена В.» — как в интерфейсе."""
    p = (name or "").split()
    return f"{p[0]} {p[1][0]}." if len(p) >= 2 else (name or "")


def _participants(case_id: int) -> dict[str, str]:
    """Логин → имя: владелец и участники дела."""
    rows = _rows("""
        SELECT x.username, COALESCE(au.display_name, x.username) AS name FROM (
            SELECT username FROM audit_case WHERE case_id = :c
            UNION SELECT username FROM audit_case_member WHERE case_id = :c) x
          LEFT JOIN app_user au ON au.username = x.username""", {"c": case_id})
    return {r["username"]: r["name"] for r in rows}


def _talk_rows(case_id: int, username: str, role: str | None) -> list[dict]:
    """Вся лента дела (обсуждение и комментарии к материалам) по возрастанию.
    Удалённые остаются заглушками: на них могут ссылаться ответы."""
    rows = _rows("""
        SELECT m.msg_id, m.item_id, m.username, COALESCE(au.display_name, m.username) AS name,
               m.body, m.mentions, m.refs, m.reply_to, m.created_at, m.edited_at,
               m.deleted_at IS NOT NULL AS deleted
          FROM audit_case_msg m LEFT JOIN app_user au ON au.username = m.username
         WHERE m.case_id = :c ORDER BY m.created_at, m.msg_id LIMIT 1000""", {"c": case_id})
    names = _participants(case_id)
    for m in rows:
        m["mine"] = m["username"] == username
        m["can_edit"] = m["mine"] and not m["deleted"]
        m["can_delete"] = (m["mine"] or role == "owner") and not m["deleted"]
        m["mention_names"] = {u: names.get(u) or u for u in (m.get("mentions") or [])}
        if m["deleted"]:
            m["body"] = ""
    return rows


def _display_name(username: str | None) -> str | None:
    if not username:
        return None
    return _scalar("SELECT COALESCE(display_name, username) FROM app_user WHERE username = :u",
                   {"u": username}) or username


def case_people(case_id: int) -> dict | None:
    """Название, владелец и участники дела (и удалённого) — кому слать
    уведомление. Без проверки доступа: зовётся после действия, которое её прошло."""
    c = _one("SELECT title, username AS owner FROM audit_case WHERE case_id = :c", {"c": case_id})
    if not c:
        return None
    c["members"] = [r["username"] for r in _rows(
        "SELECT username FROM audit_case_member WHERE case_id = :c", {"c": case_id})]
    c["everyone"] = [c["owner"], *c["members"]]
    try:
        c["muted"] = {r["username"] for r in _rows(
            "SELECT username FROM audit_case_pref WHERE case_id = :c AND muted", {"c": case_id})}
    except Exception:  # noqa: BLE001 — до миграции 084
        c["muted"] = set()
    return c


def case_members(case_id: int, username: str) -> list[dict] | None:
    """Владелец и участники с ролями. None — нет доступа к делу."""
    if not _may_read_case(case_id, username):
        return None
    rows = _rows("""
        SELECT * FROM (
            SELECT c.username, COALESCE(au.display_name, c.username) AS name, 'owner' AS role,
                   NULL::text AS added_by, c.created_at AS added_at,
                   NULL::bigint AS team_id, NULL::text AS team_name
              FROM audit_case c LEFT JOIN app_user au ON au.username = c.username
             WHERE c.case_id = :c
            UNION ALL
            SELECT m.username, COALESCE(au.display_name, m.username), m.role, m.added_by, m.added_at,
                   m.team_id, t.name
              FROM audit_case_member m LEFT JOIN app_user au ON au.username = m.username
              LEFT JOIN audit_team t ON t.team_id = m.team_id
             WHERE m.case_id = :c) x
         ORDER BY (role = 'owner') DESC, added_at""", {"c": case_id})
    for r in rows:
        r["role_label"] = CASE_ROLE_RU.get(r["role"], "")
    return rows


def set_case_member(case_id: int, owner: str, member: str, role: str) -> str | None:
    """Добавить коллегу или сменить ему роль. Возвращает текст ошибки или None."""
    if role not in ("editor", "viewer"):
        return "неизвестная роль"
    if not _owns_case(case_id, owner):
        return "управлять доступом может только владелец дела"
    if not member or member == owner:
        return "владелец уже в деле"
    if not _scalar("SELECT 1 FROM app_user WHERE username = :u", {"u": member}):
        return "такого пользователя нет в AuditLens"
    prev = case_role(case_id, member)
    with db.session() as s:
        s.execute(text("""
            INSERT INTO audit_case_member (case_id, username, role, added_by)
            VALUES (:c, :m, :r, :o)
            ON CONFLICT (case_id, username) DO UPDATE SET role = EXCLUDED.role, team_id = NULL"""),
                  {"c": case_id, "m": member, "r": role, "o": owner})
        s.execute(text("UPDATE audit_case SET updated_at = now() WHERE case_id = :c"),
                  {"c": case_id})
    if prev is None:
        case_log(case_id, owner, "member_added", {"member": member, "role": role})
    elif prev != role:
        case_log(case_id, owner, "member_role", {"member": member, "role": role, "from": prev})
    return None


def remove_case_member(case_id: int, actor: str, member: str) -> bool:
    """Владелец убирает участника; участник может выйти сам."""
    role = case_role(case_id, actor)
    if not role:
        return False
    if role == "owner" and member == actor:          # владелец не выходит — передаёт
        return False
    if role != "owner" and member != actor:          # участник убирает только себя
        return False
    with db.session() as s:
        r = s.execute(text("DELETE FROM audit_case_member WHERE case_id = :c AND username = :m"),
                      {"c": case_id, "m": member})
    if r.rowcount:
        case_log(case_id, actor, "member_left" if member == actor else "member_removed",
                 {"member": member})
        _sync_case_teams(case_id)
    return bool(r.rowcount)


def transfer_case(case_id: int, owner: str, new_owner: str) -> str | None:
    """Передать владение участнику дела; прежний владелец остаётся «может добавлять»."""
    if not _owns_case(case_id, owner):
        return "передать дело может только владелец"
    if case_role(case_id, new_owner) not in ("editor", "viewer"):
        return "передать можно только участнику дела"
    with db.session() as s:
        s.execute(text("DELETE FROM audit_case_member WHERE case_id = :c AND username = :n"),
                  {"c": case_id, "n": new_owner})
        s.execute(text("UPDATE audit_case SET username = :n, updated_at = now() WHERE case_id = :c"),
                  {"c": case_id, "n": new_owner})
        s.execute(text("""INSERT INTO audit_case_member (case_id, username, role, added_by)
                          VALUES (:c, :o, 'editor', :n)
                          ON CONFLICT (case_id, username) DO UPDATE SET role = 'editor'"""),
                  {"c": case_id, "o": owner, "n": new_owner})
    case_log(case_id, owner, "owner", {"member": new_owner})
    return None


def _attach_review_items(items: list[dict]) -> None:
    """Жалобы дела — с разметкой на сегодня: продукт, главная проблема,
    признаки, суть и цитата. Текст берётся из хранилища площадки, а если отзыв
    оттуда пропал, остаётся снимок, сохранённый при приобщении (title)."""
    urls = [it["url"] for it in items if it["kind"] == "review" and it.get("url")]
    if not urls:
        return
    try:
        from ..rag import review_codebook as cb
        from ..rag import reviews_dash as rd
        rows = {r["url"]: r for r in _rows("""
            SELECT i.url, i.bank, i.dt, i.city, i.source, i.issue, a.product, a.summary,
                   CASE WHEN a.quote_ok THEN a.quote END AS quote, a.esc, a.esc_to,
                   a.vulnerable, a.no_consent, a.misled, a.amount
              FROM review_index i
              LEFT JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
             WHERE i.url = ANY(:u)""", {"u": urls, "sv": rd._ann_schema()})}
    except Exception as e:  # noqa: BLE001 — дело открывается и без разметки
        log.warning("case: разметка жалоб не подтянулась (%s)", e)
        return
    for it in items:
        r = rows.get(it.get("url") or "") if it["kind"] == "review" else None
        if not r:
            continue
        o = cb.issue_obj(r["issue"]) or {}
        it["review"] = {
            "bank": r["bank"], "date": r["dt"].date().isoformat() if r["dt"] else None,
            "city": r["city"], "source": rd._SOURCE_LABEL.get(r["source"], r["source"]),
            "product": cb.product_label(r["product"]), "issue": r["issue"],
            "issue_label": o.get("label"), "risk": o.get("risk"),
            "summary": r["summary"], "quote": r["quote"], "esc": r["esc"],
            "esc_to": list(r["esc_to"] or []), "vulnerable": list(r["vulnerable"] or []),
            "no_consent": bool(r["no_consent"]), "misled": bool(r["misled"]),
            "amount": float(r["amount"]) if r["amount"] is not None else None}


def add_case_item(case_id: int, username: str, *, kind: str,
                  ref_id: int | None = None, url: str | None = None,
                  title: str | None = None, note: str | None = None) -> bool:
    return add_case_items(case_id, username, [{"kind": kind, "ref_id": ref_id, "url": url,
                                               "title": title, "note": note}]) is not None


# Что можно приобщить к делу (этап 4: отчёты и ответы ИИ-помощника, новости
# выпуска, продукты «Рынка»). Данные карточки — в meta (миграция 085); отчёт
# в дело не копируется — только название, вопрос и короткий вывод.
CASE_KINDS = ("review", "document", "report", "answer", "news", "offer")


def _clip(v, n: int, lines: bool = False) -> str:
    s = str(v or "")
    s = re.sub(r"[ \t]+", " ", s).strip() if lines else " ".join(s.split())
    return s[:n]


_LEAD_HEAD = re.compile(r"^#{1,4}\s*\**\s*(резюме|коротко|кратко|главное|главный вывод|ключевые выводы|"
                        r"выводы?|итоги?|summary)\b", re.I)


def report_lead(body: str | None, limit: int = 420) -> str:
    """Короткий вывод отчёта для дела и выгрузки: раздел «Резюме»/«Коротко»/
    «Выводы», если он есть, иначе первый абзац. Без ссылок на источники [N],
    таблиц и маркеров графиков — в деле у отчёта своя нумерация материалов."""
    lines = (body or "").splitlines()
    start = next((i + 1 for i, ln in enumerate(lines) if _LEAD_HEAD.match(ln.strip())), None)
    chunk = lines[start:] if start is not None else lines
    paras: list[str] = []
    cur: list[str] = []
    for ln in chunk:
        t = ln.strip()
        if t.startswith("#"):
            if cur or paras:
                break
            continue
        if not t:
            if cur:
                paras.append(" ".join(cur))
                cur = []
                if sum(len(p) for p in paras) >= limit:
                    break
            continue
        if t.startswith(("|", "[[", "```", ">")) or set(t) <= set("-*_ "):
            continue
        cur.append(re.sub(r"^([-*•]|\d+[.)])\s+", "", t))
    if cur:
        paras.append(" ".join(cur))
    out = " ".join(paras)
    out = re.sub(r"\[\[[A-Z]+:\d+\]\]", "", out)
    out = re.sub(r"\s*\[\d+(?:\s*[,;–-]\s*\d+)*\]", "", out)
    out = re.sub(r"\*\*|__|`", "", out)
    out = re.sub(r"(?<!\.)\.\.(?!\.)", ".", " ".join(out.split()))     # «п.п. [1].» → «п.п.»
    if len(out) <= limit:
        return out
    cut = out[:limit]
    dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if dot > limit * 0.55:
        return cut[:dot + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:—- ") + "…"


def _report_meta(report_id, username: str) -> dict | None:
    """Карточка отчёта для дела — с сервера, а не со слов браузера. Приобщить
    можно только отчёт, который вы сами можете открыть."""
    try:
        rid = int(report_id)
    except (TypeError, ValueError):
        return None
    if not report_access(rid, username):
        return None
    r = _one("""SELECT r.report_id, r.username, r.question, r.title, r.body, r.created_at,
                       r.payload->>'mode' AS mode,
                       CASE WHEN jsonb_typeof(r.payload->'sources') = 'array'
                            THEN jsonb_array_length(r.payload->'sources') END AS n_sources,
                       COALESCE(au.display_name, r.username) AS owner_name
                  FROM report r LEFT JOIN app_user au ON au.username = r.username
                 WHERE r.report_id = :r""", {"r": rid})
    if not r:
        return None
    return {"report_id": rid, "title": _clip(r["title"] or r["question"], 300),
            "question": _clip(r["question"], 500), "mode": r["mode"] or "quick",
            "lead": report_lead(r["body"]), "n_sources": r["n_sources"],
            "owner": r["username"], "owner_name": r["owner_name"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "chars": len(r["body"] or "")}


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _offer_meta(offer_id) -> dict | None:
    """Снимок условий продукта НА ДАТУ приобщения: витрина «Рынка» меняется,
    а доказательство в деле должно остаться тем, что видел аудитор."""
    try:
        oid = int(offer_id)
    except (TypeError, ValueError):
        return None
    o = None
    for view in ("v_market_rub_offer", "v_offer_current"):
        try:
            rows = _rows(f"SELECT * FROM {view} WHERE offer_id = :o LIMIT 1", {"o": oid})
        except Exception:  # noqa: BLE001 — витрины может не быть в тестовой базе
            rows = []
        if rows:
            o = rows[0]
            break
    if not o:
        # снят с витрины (протух, старый ключ тарифа РКО): снимок всё равно нужен
        try:
            from .app import _OFFER_ANY_SQL
            rows = _rows(_OFFER_ANY_SQL, {"o": oid})
        except Exception:  # noqa: BLE001
            rows = []
        o = rows[0] if rows else None
    if not o:
        return None
    from .. import categories as cat_meta
    from ..clock import today_msk
    vf = o.get("valid_from")
    cat = next((c for c in cat_meta.CATEGORIES if c["id"] == o.get("category")), {})
    return {"offer_id": oid, "bank": _clip(o.get("bank_name"), 120),
            "product": _clip(o.get("title"), 200), "category": o.get("category"),
            "category_label": cat.get("label"), "rate_label": cat.get("rate_label") or "Ставка",
            "psk_min": _num(o.get("psk_min")),
            "rate_pct": _num(o.get("rate_pct")), "rate_kind": _clip(o.get("rate_kind"), 60) or None,
            "amount_min": _num(o.get("amount_min")), "amount_max": _num(o.get("amount_max")),
            "term_min": _num(o.get("term_months_min")), "term_max": _num(o.get("term_months_max")),
            "fee_service": _num(o.get("fee_service")), "cashback_pct": _num(o.get("cashback_pct")),
            "grace_days": _num(o.get("grace_days")),
            "valid_from": str(vf)[:10] if vf else None, "as_of": today_msk().strftime("%Y-%m-%d"),
            "url": o.get("url")}


def _prepare_item(it: dict, username: str) -> dict | None:
    """Строка материала: вид, ссылка, название и данные карточки. None — не
    приобщается (нет доступа к отчёту, продукт не найден, пусто)."""
    import hashlib
    import json
    kind = it.get("kind") or "review"
    if kind not in CASE_KINDS:
        return None
    ref, url, title, meta = it.get("ref_id"), it.get("url"), it.get("title"), None
    m = it.get("meta") if isinstance(it.get("meta"), dict) else {}
    if kind == "report":
        meta = _report_meta(ref, username)
        if not meta:
            return None
        ref, url, title = meta["report_id"], None, meta["title"]
    elif kind == "offer":
        meta = _offer_meta(ref)
        if not meta:
            return None
        ref, url = meta["offer_id"], meta.get("url") or url
        title = " · ".join(x for x in (meta["bank"], meta["product"]) if x)
    elif kind == "news":
        if not str(url or "").startswith(("http://", "https://")):
            return None
        meta = {"source": _clip(m.get("source"), 80), "domain": _clip(m.get("domain"), 80),
                "ts": _clip(m.get("ts"), 40) or None, "summary": _clip(m.get("summary"), 500),
                "tg": bool(m.get("tg")), "group": _clip(m.get("group"), 60) or None}
        ref = None
    elif kind == "answer":
        q_, body = _clip(m.get("question"), 500), _clip(m.get("text"), 6000, lines=True)
        if not body:
            return None
        srcs = []
        for x in (m.get("sources") or [])[:12]:
            if isinstance(x, dict) and str(x.get("url") or "").startswith(("http://", "https://")):
                srcs.append({"n": x.get("n"), "url": x["url"][:500],
                             "title": _clip(x.get("title") or x.get("bank_name"), 160)})
        meta = {"question": q_, "text": body, "sources": srcs}
        title = q_ or body[:160]
        # у ответа нет ни ссылки, ни номера: адрес-отпечаток не даёт приобщить
        # один и тот же ответ дважды (уникальность по case_id, kind, url)
        url = "answer:" + hashlib.sha1((q_ + "\n" + body).encode()).hexdigest()[:20]
        ref = None
    elif not (url or ref):
        return None
    try:
        ref = int(ref) if ref is not None else None
    except (TypeError, ValueError):
        return None
    return {"k": kind, "r": ref, "u": (str(url)[:2000] if url else None),
            "t": (str(title or "")[:1500] or None), "n": (it.get("note") or None),
            "m": json.dumps(meta, ensure_ascii=False, default=str) if meta else None}


def add_case_items(case_id: int, username: str, items: list[dict], *,
                   with_ids: bool = False):
    """Приобщить материалы. Повтор того же материала в то же дело молча
    пропускается. Возвращает число добавленных (with_ids — их номера, для
    «Отменить»); None — нет прав (нет доступа, «только смотрит», архив)."""
    if not _may_add_case(case_id, username):
        return None
    rows = [r for r in (_prepare_item(it, username) for it in items[:500]) if r]
    added: list[tuple[int, dict]] = []
    if rows:
        with db.session() as s:
            for r in rows:
                iid = s.execute(text("""
                    INSERT INTO audit_case_item (case_id, kind, ref_id, url, title, note, added_by, meta)
                    VALUES (:c, :k, :r, :u, :t, NULL, :by, CAST(:m AS jsonb))
                    ON CONFLICT DO NOTHING RETURNING item_id"""),
                    {**r, "c": case_id, "by": username}).scalar()
                if iid:
                    added.append((int(iid), r))
            if added:
                s.execute(text("UPDATE audit_case SET updated_at = now() WHERE case_id = :c"),
                          {"c": case_id})
    if added:
        case_log(case_id, username, "items_added",
                 {"n": len(added), "titles": [(r["t"] or r["u"] or "")[:120] for _, r in added[:3]]})
        # «зачем приобщено», переданное вместе с материалом, — первое сообщение его ленты
        for iid, r in added:
            if (r.get("n") or "").strip():
                _first_comment(case_id, iid, r["n"], username)
    ids = [iid for iid, _ in added]
    return ids if with_ids else len(ids)


def _first_comment(case_id: int, item_id: int, body: str, username: str) -> None:
    try:
        with db.session() as s:
            s.execute(text("""INSERT INTO audit_case_msg (case_id, item_id, username, body)
                              VALUES (:c, :i, :u, :b)"""),
                      {"c": case_id, "i": item_id, "u": username,
                       "b": body.strip()[:CASE_MSG_MAX]})
    except Exception:  # noqa: BLE001
        log.warning("case %s: комментарий при приобщении не записался", case_id, exc_info=True)


def case_refs(username: str) -> dict[str, dict]:
    """Что уже лежит в доступных делах — для пометки «в деле» по всему
    инструменту. Ключ: «u:<адрес>» для жалоб, документов и новостей,
    «<вид>:<номер>» для отчётов и продуктов."""
    rows = _rows("""
        SELECT DISTINCT ON (i.kind, COALESCE(i.ref_id::text, i.url))
               i.kind, i.ref_id, i.url, c.case_id, c.title
          FROM audit_case_item i JOIN audit_case c ON c.case_id = i.case_id
         WHERE c.deleted_at IS NULL
           AND (c.username = :u
                OR EXISTS (SELECT 1 FROM audit_case_member m
                            WHERE m.case_id = c.case_id AND m.username = :u))
         ORDER BY i.kind, COALESCE(i.ref_id::text, i.url), i.added_at DESC""", {"u": username})
    out: dict[str, dict] = {}
    for r in rows:
        key = (f"{r['kind']}:{r['ref_id']}" if r["kind"] in ("report", "offer") and r["ref_id"]
               else f"u:{r['url']}" if r["url"] else None)
        if key:
            out[key] = {"case_id": r["case_id"], "title": r["title"]}
    return out


def remove_case_item(case_id: int, item_id: int, username: str) -> bool:
    f = _case_flags(case_id, username)
    if not f or f["role"] not in ("owner", "editor") or f["archived"]:
        return False
    with db.session() as s:
        it = s.execute(text("""
            SELECT i.title, i.url FROM audit_case_item i
             WHERE i.item_id = :i AND i.case_id = :c AND (:owner OR i.added_by = :u)"""),
            {"i": item_id, "c": case_id, "u": username, "owner": f["role"] == "owner"}).first()
        if not it:
            return False
        # Комментарии коллег к материалу не стираем каскадом (аудит 03.10, ДЕЛ-01):
        # они уходят в общее обсуждение с пометкой, к какому материалу были
        s.execute(text("""
            UPDATE audit_case_msg
               SET item_id = NULL,
                   refs = COALESCE(refs, '{}'::jsonb) || jsonb_build_object('_from_item', CAST(:t AS text))
             WHERE case_id = :c AND item_id = :i"""),
            {"c": case_id, "i": item_id, "t": (it[0] or it[1] or "материал")[:200]})
        gone = s.execute(text("""
            DELETE FROM audit_case_item i WHERE i.item_id = :i AND i.case_id = :c
            RETURNING i.title, i.url"""), {"i": item_id, "c": case_id}).first()
        if not gone:
            return False
        s.execute(text("UPDATE audit_case SET updated_at = now() WHERE case_id = :c"),
                  {"c": case_id})
    case_log(case_id, username, "item_removed", {"title": (gone[0] or gone[1] or "")[:120]})
    return True


def case_review_urls(username: str) -> dict[str, str]:
    """Какие жалобы уже приобщены к делам, доступным пользователю: ссылка →
    название дела (последнего). Нужно ленте, чтобы показать «в деле»."""
    rows = _rows("""
        SELECT DISTINCT ON (i.url) i.url, c.title
          FROM audit_case_item i JOIN audit_case c ON c.case_id = i.case_id
         WHERE i.kind = 'review' AND i.url IS NOT NULL AND c.deleted_at IS NULL
           AND (c.username = :u
                OR EXISTS (SELECT 1 FROM audit_case_member m
                            WHERE m.case_id = c.case_id AND m.username = :u))
         ORDER BY i.url, i.added_at DESC""", {"u": username})
    return {r["url"]: r["title"] for r in rows}


def update_case_item_note(case_id: int, item_id: int, username: str, note: str | None) -> bool:
    """Комментарий аудитора к материалу — зачем приобщён, что в нём важно.
    Меняют владелец дела и тот, кто приобщил (раньше — любой участник, молча)."""
    f = _case_flags(case_id, username)
    role = f["role"] if f and not f["archived"] else None
    if role not in ("owner", "editor"):
        return False
    with db.session() as s:
        r = s.execute(text("""UPDATE audit_case_item SET note = :n
                              WHERE item_id = :i AND case_id = :c
                                AND (:owner OR added_by = :u)"""),
                      {"n": (note or "").strip()[:2000] or None, "i": item_id, "c": case_id,
                       "owner": role == "owner", "u": username})
        s.execute(text("UPDATE audit_case SET updated_at = now() WHERE case_id = :c"),
                  {"c": case_id})
        return bool(r.rowcount)


def update_case(case_id: int, username: str, *, title: str | None = None,
                note: str | None = None) -> bool:
    if not _owns_case(case_id, username):
        return False
    old = _one("SELECT title, note FROM audit_case WHERE case_id = :c", {"c": case_id}) or {}
    with db.session() as s:
        s.execute(text("""UPDATE audit_case
                             SET title = COALESCE(NULLIF(:t, ''), title),
                                 note = CASE WHEN :setn THEN NULLIF(:n, '') ELSE note END,
                                 updated_at = now()
                           WHERE case_id = :c"""),
                  {"t": (title or "").strip()[:200], "n": (note or "").strip()[:2000],
                   "setn": note is not None, "c": case_id})
    t = (title or "").strip()[:200]
    if t and t != old.get("title"):
        case_log(case_id, username, "renamed", {"from": old.get("title"), "to": t})
    if note is not None and (note or "").strip()[:2000] != (old.get("note") or ""):
        case_log(case_id, username, "note", {"note": (note or "").strip()[:300]})
    return True


def set_case_shared(case_id: int, owner: str, shared: bool) -> bool:
    """Прежняя кнопка «Открыть команде» (доступ всем пользователям) — убрана:
    открыть дело можно только поимённо. Закрыть — значит убрать всех участников."""
    if not _owns_case(case_id, owner) or shared:
        return False
    with db.session() as s:
        gone = s.execute(text("DELETE FROM audit_case_member WHERE case_id = :c RETURNING username"),
                         {"c": case_id}).scalars().all()
    for m in gone:
        case_log(case_id, owner, "member_removed", {"member": m})
    return True


def analysis_state(case: dict) -> None:
    """Свежесть разбора и перевод его [N] в текущие номера материалов.

    [N] в разборе — порядковый номер материала на момент разбора. Состав
    сравниваем целиком (какие материалы и в каком порядке), а не по числу:
    «убрали один, добавили другой» — тоже новый состав. analysis_refs: номер в
    разборе → текущий номер материала (None — материал удалён из дела)."""
    if not case.get("analysis"):
        return
    now_ids = [it["item_id"] for it in case.get("items") or []]
    ids = case.get("analysis_item_ids")
    if ids is None:                    # разбор до 03.10 — состав не записан
        case["analysis_stale"] = case.get("analysis_items") != len(now_ids)
        return
    ids = [int(x) for x in ids]
    case["analysis_item_ids"] = ids
    case["analysis_stale"] = ids != now_ids
    pos = {iid: k for k, iid in enumerate(now_ids, 1)}
    case["analysis_refs"] = {str(k): pos.get(iid) for k, iid in enumerate(ids, 1)}


def remap_analysis(case: dict) -> str | None:
    """Текст разбора с номерами [N] текущего списка материалов (для выгрузки)."""
    body = case.get("analysis")
    refs = case.get("analysis_refs")
    if not body or not refs:
        return body

    def _sub(m):
        cur = refs.get(m.group(1), "keep")
        if cur == "keep":
            return m.group(0)
        return f"[{cur}]" if cur else "[материал удалён]"
    return re.sub(r"\[(\d{1,3})\]", _sub, body)


def save_case_analysis(case_id: int, username: str, analysis: str, n_items: int,
                       item_ids: list[int] | None = None) -> bool:
    """item_ids — состав дела в порядке, по которому модель нумеровала [N]."""
    if not _may_add_case(case_id, username):
        return False
    ids = [int(x) for x in item_ids] if item_ids is not None else None
    with db.session() as s:
        s.execute(text("""UPDATE audit_case SET analysis = :a, analysis_at = now(),
                                  analysis_items = :n, analysis_item_ids = :ids
                            WHERE case_id = :c"""),
                  {"a": analysis, "n": n_items, "c": case_id, "ids": ids})
        # прошлые версии не затираются: разбор — подпись «кто и когда»
        s.execute(text("""INSERT INTO audit_case_analysis (case_id, body, n_items, username, item_ids)
                          VALUES (:c, :a, :n, :u, :ids)"""),
                  {"c": case_id, "a": analysis, "n": n_items, "u": username, "ids": ids})
    case_log(case_id, username, "analysis", {"n": n_items})
    return True


def delete_case(case_id: int, username: str) -> bool:
    """Мягко: дело ведут вместе, и удаление одним кликом уносило материалы
    коллег навсегда. 30 дней владелец может его вернуть."""
    if not _owns_case(case_id, username):
        return False
    with db.session() as s:
        s.execute(text("""UPDATE audit_case SET deleted_at = now(), deleted_by = :u
                           WHERE case_id = :c"""), {"c": case_id, "u": username})
    case_log(case_id, username, "deleted")
    return True


def restore_case(case_id: int, username: str) -> bool:
    if case_role(case_id, username, deleted=True) != "owner":
        return False
    with db.session() as s:
        s.execute(text("""UPDATE audit_case SET deleted_at = NULL, deleted_by = NULL,
                                  updated_at = now() WHERE case_id = :c"""), {"c": case_id})
    case_log(case_id, username, "restored")
    return True


def share_case(case_id: int, owner: str, shared_with: str | None) -> bool:
    """Прежний адресный шеринг — теперь участник «может добавлять». Без адресата
    (всем пользователям) — нельзя."""
    if not shared_with:
        return False
    return set_case_member(case_id, owner, shared_with, "editor") is None


# ── Совместная работа в деле (миграция 084) ──────────────────────────────────
# Статус и архив — владелец. Лента сообщений — все участники, включая «только
# смотрит» (решение владельца инструмента 03.10): обсуждение дела и комментарии
# к материалам с авторами, @упоминаниями, ссылками [N] и ответами. История —
# кто что сделал; версии разбора — кто и когда его запускал.

_MENTION_LIMIT = 20


def set_case_status(case_id: int, owner: str, status: str | None = None,
                    archived: bool | None = None) -> list[dict]:
    """Сменить статус и/или архив. Возвращает список перемен (для уведомлений).
    Отказ — CaseError с текстом для человека."""
    f = _case_flags(case_id, owner)
    if not f:
        raise CaseError("дело не найдено")
    if f["role"] != "owner":
        raise CaseError("статус и архив меняет владелец дела")
    if status is not None and status not in CASE_STATUS:
        raise CaseError("неизвестный статус")
    cur = f["status"] or "collect"
    arch = f["archived"] if archived is None else bool(archived)
    if status is not None and status != cur and arch:
        raise CaseError("дело в архиве — сначала верните его")
    changes: list[dict] = []
    with db.session() as s:
        if archived is not None and bool(archived) != f["archived"]:
            s.execute(text("""UPDATE audit_case
                                 SET archived_at = CASE WHEN :a THEN now() ELSE NULL END,
                                     updated_at = now() WHERE case_id = :c"""),
                      {"a": bool(archived), "c": case_id})
            changes.append({"kind": "archived", "on": bool(archived)})
        if status is not None and status != cur:
            s.execute(text("""UPDATE audit_case SET status = :s, status_at = now(), updated_at = now()
                               WHERE case_id = :c"""), {"s": status, "c": case_id})
            changes.append({"kind": "status", "from": cur, "to": status,
                            "label": CASE_STATUS[status]})
    for ch in changes:
        case_log(case_id, owner, ch["kind"], {k: v for k, v in ch.items() if k != "kind"})
    return changes


def _case_items_map(case_id: int) -> dict[int, dict]:
    return {r["item_id"]: r for r in _rows(
        "SELECT item_id, added_by, title, url FROM audit_case_item WHERE case_id = :c",
        {"c": case_id})}


def add_case_msg(case_id: int, username: str, body: str, *, mentions: list | None = None,
                 refs: dict | None = None, reply_to: int | None = None,
                 item_id: int | None = None) -> dict:
    """Сообщение в обсуждение дела (item_id=None) или комментарий к материалу.
    Возвращает сообщение и всё, что нужно уведомлениям: кого упомянули, кому
    ответили, чей материал прокомментировали, кто ещё в деле."""
    f = _case_flags(case_id, username)
    if not f:
        raise CaseError("дело не найдено")
    if f["archived"]:
        raise CaseError("дело в архиве — писать в него нельзя")
    body = (body or "").strip()
    if not body:
        raise CaseError("пустое сообщение")
    if len(body) > CASE_MSG_MAX:
        raise CaseError(f"сообщение длиннее {CASE_MSG_MAX} знаков — сократите")
    people = _participants(case_id)
    ments = sorted({str(m) for m in (mentions or []) if m in people and m != username})
    ments = ments[:_MENTION_LIMIT]
    items = _case_items_map(case_id)
    clean_refs: dict[str, int] = {}
    for k, v in (refs or {}).items():
        try:
            n, iv = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if iv in items and 0 < n < 10000:
            clean_refs[str(n)] = iv
    if item_id is not None and int(item_id) not in items:
        raise CaseError("материал не найден в деле")
    parent = None
    if reply_to is not None:
        parent = _one("""SELECT msg_id, username, deleted_at, item_id FROM audit_case_msg
                          WHERE msg_id = :m AND case_id = :c""", {"m": reply_to, "c": case_id})
        if not parent:
            raise CaseError("сообщение, на которое вы отвечаете, не найдено")
    import json
    with db.session() as s:
        row = s.execute(text("""
            INSERT INTO audit_case_msg (case_id, item_id, username, body, mentions, refs, reply_to)
            VALUES (:c, :i, :u, :b, :m, CAST(:r AS jsonb), :p)
            RETURNING msg_id, created_at"""),
            {"c": case_id, "i": item_id, "u": username, "b": body, "m": ments,
             "r": json.dumps(clean_refs), "p": reply_to}).mappings().first()
        s.execute(text("UPDATE audit_case SET updated_at = now() WHERE case_id = :c"), {"c": case_id})
        # своё сообщение — значит, ленту видели: «новые» у себя не копим
        s.execute(text("""INSERT INTO audit_case_pref (case_id, username, talk_seen_at)
                          VALUES (:c, :u, now())
                          ON CONFLICT (case_id, username) DO UPDATE SET talk_seen_at = now()"""),
                  {"c": case_id, "u": username})
    it = items.get(int(item_id)) if item_id is not None else None
    return {"msg_id": row["msg_id"], "created_at": row["created_at"], "case_title": f["title"],
            "participants": list(people), "mentions": ments, "refs": clean_refs,
            "reply_author": parent["username"] if parent and not parent["deleted_at"] else None,
            "item_author": (it or {}).get("added_by"),
            "item_title": ((it or {}).get("title") or (it or {}).get("url") or "")[:120] or None}


def edit_case_msg(case_id: int, msg_id: int, username: str, body: str) -> bool:
    """Править своё сообщение (пометка «изменено»). Уведомления не повторяются."""
    body = (body or "").strip()
    if not body:
        raise CaseError("пустое сообщение")
    if len(body) > CASE_MSG_MAX:
        raise CaseError(f"сообщение длиннее {CASE_MSG_MAX} знаков — сократите")
    f = _case_flags(case_id, username)
    if not f or f["archived"]:
        return False
    with db.session() as s:
        r = s.execute(text("""UPDATE audit_case_msg SET body = :b, edited_at = now()
                               WHERE msg_id = :m AND case_id = :c AND username = :u
                                 AND deleted_at IS NULL"""),
                      {"b": body, "m": msg_id, "c": case_id, "u": username})
    return bool(r.rowcount)


def delete_case_msg(case_id: int, msg_id: int, username: str) -> bool:
    """Своё — автор, любое — владелец дела. Мягко: на сообщение могут ссылаться ответы."""
    f = _case_flags(case_id, username)
    if not f or f["archived"]:
        return False
    with db.session() as s:
        r = s.execute(text("""UPDATE audit_case_msg SET deleted_at = now(), body = ''
                               WHERE msg_id = :m AND case_id = :c AND deleted_at IS NULL
                                 AND (username = :u OR :owner)"""),
                      {"m": msg_id, "c": case_id, "u": username, "owner": f["role"] == "owner"})
    return bool(r.rowcount)


def case_talk(case_id: int, username: str) -> dict | None:
    """Лента дела: обсуждение и комментарии к материалам, по возрастанию."""
    role = case_role(case_id, username)
    if not role:
        return None
    seen = _scalar("""SELECT talk_seen_at FROM audit_case_pref
                       WHERE case_id = :c AND username = :u""", {"c": case_id, "u": username})
    rows = _talk_rows(case_id, username, role)
    for m in rows:
        m["new"] = (not m["mine"] and not m["deleted"]
                    and (seen is None or m["created_at"] > seen))
    return {"messages": rows, "people": [{"username": u, "name": n}
                                         for u, n in _participants(case_id).items()]}


def mark_talk_seen(case_id: int, username: str) -> bool:
    if not _may_read_case(case_id, username):
        return False
    with db.session() as s:
        s.execute(text("""INSERT INTO audit_case_pref (case_id, username, talk_seen_at)
                          VALUES (:c, :u, now())
                          ON CONFLICT (case_id, username) DO UPDATE SET talk_seen_at = now()"""),
                  {"c": case_id, "u": username})
    return True


def set_case_mute(case_id: int, username: str, muted: bool) -> bool:
    """«Не следить за делом»: без уведомлений о материалах, сообщениях, статусе и
    разборе. Упоминания, ответы и изменения доступа приходят всё равно."""
    if not _may_read_case(case_id, username):
        return False
    with db.session() as s:
        s.execute(text("""INSERT INTO audit_case_pref (case_id, username, muted)
                          VALUES (:c, :u, :m)
                          ON CONFLICT (case_id, username) DO UPDATE SET muted = :m"""),
                  {"c": case_id, "u": username, "m": bool(muted)})
    return True


def _ev_text(kind: str, p: dict, name) -> str:
    """Строка истории — событие без рода: кто сделал, видно рядом."""
    def q(x):
        x = " ".join(str(x or "").split())
        return f"«{x[:80]}…»" if len(x) > 80 else f"«{x}»"
    if kind == "created":
        return "Дело создано"
    if kind == "renamed":
        return f"Новое название: {q(p.get('from'))} → {q(p.get('to'))}"
    if kind == "note":
        return "Изменена цель дела" + (f": {q(p.get('note'))}" if p.get("note") else "")
    if kind == "items_added":
        n, ts = int(p.get("n") or 1), [t for t in p.get("titles") or [] if t]
        if n == 1:
            return "Добавлен материал" + (f": {q(ts[0])}" if ts else "")
        return f"Добавлено материалов: {n}" + (f" — {', '.join(q(t) for t in ts[:2])}"
                                               + ("…" if n > 2 else "") if ts else "")
    if kind == "item_removed":
        return "Убран материал" + (f": {q(p.get('title'))}" if p.get("title") else "")
    if kind == "member_added":
        via = f" из команды «{p['team']}»" if p.get("team") else ""
        return f"Новый участник{via}: {name(p.get('member'))} — {CASE_ROLE_RU.get(p.get('role'), '')}"
    if kind == "team_added":
        return f"Подключена команда «{p.get('team')}» — {CASE_ROLE_RU.get(p.get('role'), '')}"
    if kind == "team_role":
        return f"Права команды «{p.get('team')}»: {CASE_ROLE_RU.get(p.get('role'), '')}"
    if kind == "team_removed":
        return (f"Команда «{p.get('team')}» удалена её создателем" if p.get("deleted")
                else f"Отключена команда «{p.get('team')}»")
    if kind == "member_role":
        return f"Права участника {name(p.get('member'))}: {CASE_ROLE_RU.get(p.get('role'), '')}"
    if kind == "member_removed":
        return (f"Доступ через команду снят: {name(p.get('member'))}" if p.get("team")
                else f"Исключение из дела: {name(p.get('member'))}")
    if kind == "member_left":
        return "Выход из дела"
    if kind == "owner":
        return f"Передача владения: теперь ведёт {name(p.get('member'))}"
    if kind == "status":
        return (f"Статус: {CASE_STATUS.get(p.get('from'), p.get('from') or '—')}"
                f" → {CASE_STATUS.get(p.get('to'), p.get('to') or '—')}")
    if kind == "archived":
        return "Дело перенесено в архив" if p.get("on") else "Дело возвращено из архива"
    if kind == "analysis":
        n = p.get("n")
        return "Новый разбор ИИ" + (f" — по {n} {'материалу' if n == 1 else 'материалам'}" if n else "")
    if kind == "deleted":
        return "Дело удалено"
    if kind == "restored":
        return "Дело восстановлено"
    return kind


def case_history(case_id: int, username: str, limit: int = 300) -> list[dict] | None:
    if not _may_read_case(case_id, username):
        return None
    rows = _rows("""SELECT e.event_id, e.kind, e.payload, e.created_at, e.username,
                           COALESCE(au.display_name, e.username) AS name
                      FROM audit_case_event e LEFT JOIN app_user au ON au.username = e.username
                     WHERE e.case_id = :c ORDER BY e.created_at DESC, e.event_id DESC LIMIT :n""",
                 {"c": case_id, "n": limit})
    logins = {str((r["payload"] or {}).get("member")) for r in rows if (r["payload"] or {}).get("member")}
    names = {r["username"]: r["display_name"] or r["username"] for r in _rows(
        "SELECT username, display_name FROM app_user WHERE username = ANY(:u)",
        {"u": list(logins)})} if logins else {}

    def name(u):
        return puname(names.get(u) or u) if u else "—"
    for r in rows:
        r["text"] = _ev_text(r["kind"], r["payload"] or {}, name)
        r["who"] = puname(r.pop("name")) if r.get("username") else None
        r.pop("payload", None)
    return rows


def get_case_analysis(case_id: int, username: str, analysis_id: int) -> dict | None:
    if not _may_read_case(case_id, username):
        return None
    return _one("""SELECT a.analysis_id, a.body, a.n_items, a.created_at,
                          COALESCE(au.display_name, a.username) AS name
                     FROM audit_case_analysis a LEFT JOIN app_user au ON au.username = a.username
                    WHERE a.case_id = :c AND a.analysis_id = :a""", {"c": case_id, "a": analysis_id})


# ── Команды (миграция 086) ───────────────────────────────────────────────────
# Сохранённая группа коллег: её ведёт создатель и подключает к своим делам с
# ролью. Подключение «живое» — состав команды пересчитывается в участников
# каждого её дела (строки audit_case_member с team_id). Чужую команду
# подключить нельзя: её создатель мог бы провести в ваше дело кого угодно.

TEAM_MAX = 80


def _sync_case_teams(case_id: int, log_members: bool = True) -> tuple[list[tuple[str, str | None, str]], list[str]]:
    """Пересчитать участников «через команду». Личная строка важнее командной;
    из нескольких команд берётся лучшая роль. Возвращает (кого добавили —
    (логин, команда, роль), кого убрали)."""
    best = """
        SELECT DISTINCT ON (tm.username) tm.username, ct.role, ct.team_id, ct.added_by, t.name
          FROM audit_case_team ct
          JOIN audit_team_member tm ON tm.team_id = ct.team_id
          JOIN audit_team t ON t.team_id = ct.team_id
          JOIN audit_case c ON c.case_id = ct.case_id
         WHERE ct.case_id = :c AND tm.username <> c.username
         ORDER BY tm.username, (ct.role = 'editor') DESC, ct.added_at"""
    try:
        with db.session() as s:
            removed = s.execute(text("""
                DELETE FROM audit_case_member cm
                 WHERE cm.case_id = :c AND cm.team_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM audit_case_team ct
                                     JOIN audit_team_member tm ON tm.team_id = ct.team_id
                                    WHERE ct.case_id = :c AND tm.username = cm.username)
                RETURNING cm.username"""), {"c": case_id}).scalars().all()
            added = s.execute(text(f"""
                INSERT INTO audit_case_member (case_id, username, role, added_by, team_id)
                SELECT :c, b.username, b.role, b.added_by, b.team_id FROM ({best}) b
                ON CONFLICT (case_id, username) DO NOTHING
                RETURNING username, team_id, role"""), {"c": case_id}).all()
            s.execute(text(f"""
                UPDATE audit_case_member cm SET role = b.role, team_id = b.team_id
                  FROM ({best}) b
                 WHERE cm.case_id = :c AND cm.username = b.username AND cm.team_id IS NOT NULL
                   AND (cm.role <> b.role OR cm.team_id <> b.team_id)"""), {"c": case_id})
    except Exception:  # noqa: BLE001 — до миграции 086 команд нет
        log.debug("case %s: пересчёт команд не удался", case_id, exc_info=True)
        return [], []
    names = {}
    if added:
        names = {r["team_id"]: r["name"] for r in _rows(
            "SELECT team_id, name FROM audit_team WHERE team_id = ANY(:t)",
            {"t": list({a[1] for a in added})})}
    if log_members:            # подключение команды — одна строка истории, а не десять
        for u in removed:
            case_log(case_id, None, "member_removed", {"member": u, "team": True})
        for u, t, role in added:
            case_log(case_id, None, "member_added", {"member": u, "role": role, "team": names.get(t)})
    return [(u, names.get(t), role) for u, t, role in added], list(removed)


def _team_cases(team_id: int) -> list[int]:
    return [r["case_id"] for r in _rows("""
        SELECT ct.case_id FROM audit_case_team ct JOIN audit_case c ON c.case_id = ct.case_id
         WHERE ct.team_id = :t AND c.deleted_at IS NULL""", {"t": team_id})]


def _own_team(team_id: int, username: str) -> dict | None:
    return _one("SELECT team_id, name, owner FROM audit_team WHERE team_id = :t AND owner = :u",
                {"t": team_id, "u": username})


def list_teams(username: str) -> list[dict]:
    """Мои команды: состав и в скольких делах подключены."""
    try:
        teams = _rows("""
            SELECT t.team_id, t.name, t.updated_at,
                   (SELECT count(*) FROM audit_case_team ct JOIN audit_case c ON c.case_id = ct.case_id
                     WHERE ct.team_id = t.team_id AND c.deleted_at IS NULL) AS cases
              FROM audit_team t WHERE t.owner = :u ORDER BY lower(t.name)""", {"u": username})
        mem = _rows("""
            SELECT tm.team_id, tm.username, COALESCE(au.display_name, tm.username) AS name
              FROM audit_team_member tm JOIN audit_team t ON t.team_id = tm.team_id
              LEFT JOIN app_user au ON au.username = tm.username
             WHERE t.owner = :u ORDER BY 3""", {"u": username})
    except Exception:  # noqa: BLE001 — до миграции 086
        return []
    by = {}
    for m in mem:
        by.setdefault(m.pop("team_id"), []).append(m)
    for t in teams:
        t["members"] = by.get(t["team_id"], [])
    return teams


def _clean_members(owner: str, members) -> list[str]:
    ms = [str(m) for m in dict.fromkeys(members or []) if m and m != owner][:TEAM_MAX]
    if not ms:
        return []
    known = {r["username"] for r in _rows("SELECT username FROM app_user WHERE username = ANY(:u)",
                                          {"u": ms})}
    return [m for m in ms if m in known]


def create_team(owner: str, name: str, members: list[str] | None = None) -> int:
    name = " ".join((name or "").split())[:120]
    if not name:
        raise CaseError("нужно название команды")
    ms = _clean_members(owner, members)
    with db.session() as s:
        tid = s.execute(text("INSERT INTO audit_team (owner, name) VALUES (:o, :n) RETURNING team_id"),
                        {"o": owner, "n": name}).scalar_one()
        for m in ms:
            s.execute(text("""INSERT INTO audit_team_member (team_id, username) VALUES (:t, :u)
                              ON CONFLICT DO NOTHING"""), {"t": tid, "u": m})
    return int(tid)


def update_team(owner: str, team_id: int, *, name: str | None = None,
                add: list[str] | None = None, remove: list[str] | None = None) -> list[dict]:
    """Переименовать, добавить и убрать участников. Возвращает перемены доступа
    по делам команды: [{case_id, added: [(логин, команда, роль)], removed: [логин]}]."""
    t = _own_team(team_id, owner)
    if not t:
        raise CaseError("команду меняет только тот, кто её создал")
    nm = " ".join((name or "").split())[:120] if name is not None else None
    ad = _clean_members(owner, add)
    rm = [str(x) for x in (remove or [])][:TEAM_MAX]
    with db.session() as s:
        if nm:
            s.execute(text("UPDATE audit_team SET name = :n, updated_at = now() WHERE team_id = :t"),
                      {"n": nm, "t": team_id})
        for m in ad:
            s.execute(text("""INSERT INTO audit_team_member (team_id, username) VALUES (:t, :u)
                              ON CONFLICT DO NOTHING"""), {"t": team_id, "u": m})
        if rm:
            s.execute(text("DELETE FROM audit_team_member WHERE team_id = :t AND username = ANY(:u)"),
                      {"t": team_id, "u": rm})
        if ad or rm:
            s.execute(text("UPDATE audit_team SET updated_at = now() WHERE team_id = :t"), {"t": team_id})
    out = []
    if ad or rm:
        for cid in _team_cases(team_id):
            a, r = _sync_case_teams(cid)
            if a or r:
                out.append({"case_id": cid, "added": a, "removed": r})
    return out


def delete_team(owner: str, team_id: int) -> list[dict]:
    """Удалить команду: её участники теряют доступ к делам, куда их не добавили отдельно."""
    if not _own_team(team_id, owner):
        raise CaseError("удалить команду может только тот, кто её создал")
    cases = _team_cases(team_id)
    name = (_own_team(team_id, owner) or {}).get("name")
    # кто потеряет доступ: строки «через эту команду» уйдут каскадом (team_id → ON DELETE CASCADE)
    lost = {cid: [r["username"] for r in _rows(
        "SELECT username FROM audit_case_member WHERE case_id = :c AND team_id = :t",
        {"c": cid, "t": team_id})] for cid in cases}
    with db.session() as s:
        s.execute(text("DELETE FROM audit_team WHERE team_id = :t"), {"t": team_id})
    out = []
    for cid in cases:
        case_log(cid, owner, "team_removed", {"team": name, "deleted": True})
        a, _r = _sync_case_teams(cid, log_members=False)   # участники других команд возвращаются
        gone = set(lost.get(cid, []))
        back = {u for u, _t, _role in a}
        # вернувшиеся через другую команду доступа не теряли — им ничего не сообщаем
        out.append({"case_id": cid, "added": [x for x in a if x[0] not in gone],
                    "removed": [u for u in gone if u not in back]})
    return out


def case_teams(case_id: int) -> list[dict]:
    try:
        return _rows("""
            SELECT ct.team_id, t.name, ct.role, t.owner, ct.added_at,
                   (SELECT count(*) FROM audit_team_member tm WHERE tm.team_id = ct.team_id) AS n
              FROM audit_case_team ct JOIN audit_team t ON t.team_id = ct.team_id
             WHERE ct.case_id = :c ORDER BY ct.added_at""", {"c": case_id})
    except Exception:  # noqa: BLE001 — до миграции 086
        return []


def attach_team(case_id: int, owner: str, team_id: int, role: str):
    """Подключить свою команду к своему делу (или сменить ей роль).
    Возвращает (added, removed) или текст ошибки."""
    if role not in ("editor", "viewer"):
        return "неизвестная роль"
    if not _owns_case(case_id, owner):
        return "управлять доступом может только владелец дела"
    t = _own_team(team_id, owner)
    if not t:
        return "подключить можно только свою команду"
    with db.session() as s:
        prev = s.execute(text("SELECT role FROM audit_case_team WHERE case_id = :c AND team_id = :t"),
                         {"c": case_id, "t": team_id}).scalar()
        s.execute(text("""INSERT INTO audit_case_team (case_id, team_id, role, added_by)
                          VALUES (:c, :t, :r, :o)
                          ON CONFLICT (case_id, team_id) DO UPDATE SET role = EXCLUDED.role"""),
                  {"c": case_id, "t": team_id, "r": role, "o": owner})
        s.execute(text("UPDATE audit_case SET updated_at = now() WHERE case_id = :c"), {"c": case_id})
    if prev is None:
        case_log(case_id, owner, "team_added", {"team": t["name"], "role": role})
    elif prev != role:
        case_log(case_id, owner, "team_role", {"team": t["name"], "role": role})
    return _sync_case_teams(case_id, log_members=False)


def detach_team(case_id: int, owner: str, team_id: int):
    """Отключить команду от дела. Возвращает (added, removed) или None — нет прав."""
    if not _owns_case(case_id, owner):
        return None
    with db.session() as s:
        name = s.execute(text("""DELETE FROM audit_case_team ct USING audit_team t
                                  WHERE ct.case_id = :c AND ct.team_id = :t AND t.team_id = ct.team_id
                                  RETURNING t.name"""), {"c": case_id, "t": team_id}).scalar()
    if name is None:
        return None
    case_log(case_id, owner, "team_removed", {"team": name})
    return _sync_case_teams(case_id, log_members=False)


def member_team(case_id: int, username: str) -> str | None:
    """Через какую команду человек в деле (None — лично или не в деле)."""
    try:
        return _scalar("""SELECT t.name FROM audit_case_member m JOIN audit_team t ON t.team_id = m.team_id
                           WHERE m.case_id = :c AND m.username = :u""", {"c": case_id, "u": username})
    except Exception:  # noqa: BLE001
        return None
