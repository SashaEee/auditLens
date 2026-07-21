"""Слой данных персонализации: пользователи, история чатов/отчётов, шеринг,
события и профиль интересов, персональный дайджест.

Весь SQL — через sqlalchemy.text() поверх db.session() (коммит на выходе).
Схема auditlens (search_path на роли). См. migrations/014_personalization.sql
и docs/PERSONALIZATION_PLAN.md. Модуль «Лазейки» имеет свой слой — не пересекается.
"""
from __future__ import annotations

import logging
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
               timezone: str | None = None) -> dict | None:
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
    """Директория пользователей инструмента (для шеринга) — все, кто заходил."""
    rows = _rows("""SELECT username, display_name, last_seen_at
                    FROM app_user ORDER BY last_seen_at DESC""")
    if exclude:
        rows = [r for r in rows if r["username"] != exclude]
    return rows


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
    """Сессии пользователя с превью последнего сообщения (для drawer истории)."""
    return _rows("""
        SELECT cs.session_id, cs.title, cs.pinned, cs.created_at, cs.updated_at,
               (SELECT content FROM chat_message cm
                 WHERE cm.session_id = cs.session_id
                 ORDER BY cm.created_at DESC LIMIT 1) AS last_preview,
               (SELECT count(*) FROM chat_message cm
                 WHERE cm.session_id = cs.session_id) AS n_messages
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


def count_reports(username: str) -> int:
    return int(_scalar("SELECT count(*) FROM report WHERE username = :u", {"u": username}) or 0)


def list_reports(username: str, limit: int = 100) -> list[dict]:
    return _rows("""SELECT report_id, session_id, question, title, banks, created_at,
                           left(body, 240) AS preview
                    FROM report WHERE username = :u
                    ORDER BY created_at DESC LIMIT :lim""",
                 {"u": username, "lim": limit})


def report_access(report_id: int, username: str) -> bool:
    """Доступ: владелец ИЛИ отчёт расшарен ему лично ИЛИ всем (shared_with IS NULL)."""
    owner = _scalar("SELECT username FROM report WHERE report_id = :r", {"r": report_id})
    if owner == username:
        return True
    n = _scalar("""SELECT count(*) FROM report_share
                   WHERE report_id = :r AND revoked_at IS NULL
                     AND (shared_with = :u OR shared_with IS NULL)""",
                {"r": report_id, "u": username})
    return bool(n)


def get_report(report_id: int, username: str) -> dict | None:
    if not report_access(report_id, username):
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
    (re.compile(r"перевод|сбп\b|комисси", re.I), "transfers"),
]


def parse_query_signals(question: str) -> dict:
    """Детерминированный разбор запроса: банки + продукты (0 LLM)."""
    from ..ai.llm_utils import detect_bank_slugs
    banks = list(detect_bank_slugs(question or ""))
    products = [slug for rx, slug in _PRODUCT_KEYWORDS if rx.search(question or "")]
    return {"banks": banks, "products": products}


_DECAY = 0.95  # затухание старого веса на каждый новый запрос


def update_interests_from_query(username: str, question: str) -> dict:
    """Обновляет счётчики интересов с затуханием. Возвращает signals."""
    signals = parse_query_signals(question)
    try:
        user = get_user(username) or {}
        interests = user.get("interests") or {}
        if isinstance(interests, str):
            import json
            interests = json.loads(interests or "{}")
        counters = interests.get("counters") or {"banks": {}, "products": {}}
        for dim in ("banks", "products"):
            bucket = counters.setdefault(dim, {})
            # Затухание всех + инкремент попавших.
            for k in list(bucket):
                bucket[k] = round(bucket[k] * _DECAY, 4)
            for k in signals.get(dim, []):
                bucket[k] = round(bucket.get(k, 0.0) * _DECAY + 1.0, 4)
            # Чистим шум.
            counters[dim] = {k: v for k, v in bucket.items() if v >= 0.05}
        interests["counters"] = counters
        import json
        with db.session() as s:
            s.execute(text("UPDATE app_user SET interests = CAST(:i AS jsonb) WHERE username = :u"),
                      {"i": json.dumps(interests, ensure_ascii=False), "u": username})
    except Exception:
        log.warning("[userdata] update_interests failed", exc_info=True)
    return signals


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
    # Сбер — якорь для всех: пользователи это аудиторы Сбербанка.
    if "sberbank" not in muted:
        weights["sberbank"] = max(weights.get("sberbank", 0.0), 2.5)
    return {"weights": weights, "self_desc": self_desc, "custom": custom,
            "pinned": list(pinned), "muted": list(muted)}


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
