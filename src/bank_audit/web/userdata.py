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
    return {"verdict": new_v, "content_ratings": int(n)}


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
    focus_n = len(set((ti.get("products") or []) + (ti.get("pinned") or [])))
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
    part("queries", "5+ вопросов ИИ-аналитику", 15 * min(q_n / 5, 1), 15,
         q_n >= 5, "Спросите ИИ-аналитика о своей теме", "ai")
    part("ai_ratings", "3+ оценки ответов ИИ", 10 * min(ai_n / 3, 1), 10,
         ai_n >= 3, "Оцените пару ответов ИИ-аналитика", "ai")
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
                    "mode": p.get("mode"),
                    "created_at": str(r.get("created_at") or "")})
    return {"likes_7d": likes, "dislikes_7d": dislikes, "recent_dislikes": out}
