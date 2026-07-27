"""Телеметрия использования + метрики дашборда «Пульс» (миграция 016).

Два потока событий в usage_event:
  • фронт: page_view / page_leave(dur_ms) / client_error — батчами через /api/track;
  • бекенд: api_request / api_error — HTTP-middleware (латентность, статусы, исключения).

Доступ к метрикам — только владельцу: env ADMIN_USERS (список username через запятую).
Имя в коде не хардкодим — репозиторий публичный.

Всё best-effort: телеметрия НИКОГДА не ломает основной запрос.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

# kinds, которые принимаем от фронта (всё остальное молча отбрасываем)
_CLIENT_KINDS = {"page_view", "page_leave", "client_error", "ui"}
_MAX_BATCH = 25
_MAX_DUR_MS = 30 * 60 * 1000          # страница «висела» дольше 30 мин → кап

_ID_RE = re.compile(r"/\d+")


def norm_path(path: str) -> str:
    """Нормализация /api-пути для группировки латентности: /api/reports/17 → /api/reports/:id."""
    return _ID_RE.sub("/:id", path or "")[:120]


def is_admin(username: str | None) -> bool:
    admins = {u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()}
    return bool(username) and username in admins


def log_event(username: str | None, kind: str, page: str | None = None,
              dur_ms: int | None = None, status: int | None = None,
              payload: dict | None = None) -> None:
    """Одиночная запись события (sync, зовётся из to_thread). Никогда не кидает."""
    try:
        with db.session() as s:
            s.execute(text("""
                INSERT INTO usage_event (username, kind, page, dur_ms, status, payload)
                VALUES (:u, :k, :p, :d, :st, CAST(:pl AS jsonb))
            """), {"u": (username or None), "k": kind[:40], "p": (page or None),
                   "d": dur_ms, "st": status,
                   "pl": json.dumps(payload or {}, ensure_ascii=False, default=str)[:2000]})
    except Exception:
        log.debug("[telemetry] log_event failed", exc_info=True)


def track_batch(username: str, events: list[dict]) -> int:
    """Батч событий фронта. Возвращает число принятых."""
    accepted = 0
    rows = []
    for ev in (events or [])[:_MAX_BATCH]:
        kind = str(ev.get("kind") or "")
        if kind not in _CLIENT_KINDS:
            continue
        dur = ev.get("dur_ms")
        try:
            dur = min(int(dur), _MAX_DUR_MS) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        rows.append({"u": username, "k": kind, "p": str(ev.get("page") or "")[:60] or None,
                     "d": dur,
                     "pl": json.dumps(ev.get("payload") or {}, ensure_ascii=False,
                                      default=str)[:1000]})
        accepted += 1
    if not rows:
        return 0
    try:
        with db.session() as s:
            s.execute(text("""
                INSERT INTO usage_event (username, kind, page, dur_ms, payload)
                VALUES (:u, :k, :p, :d, CAST(:pl AS jsonb))
            """), rows)
    except Exception:
        log.warning("[telemetry] track_batch failed", exc_info=True)
        return 0
    return accepted


# ── метрики дашборда ──────────────────────────────────────────────────────────

def _rows(sql: str, params: dict | None = None) -> list[dict]:
    try:
        with db.session() as s:
            return [dict(r) for r in s.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        log.warning("[telemetry] metrics query failed", exc_info=True)
        return []


def _scalar(sql: str, params: dict | None = None) -> Any:
    try:
        with db.session() as s:
            return s.execute(text(sql), params or {}).scalar_one_or_none()
    except Exception:
        log.warning("[telemetry] metrics scalar failed", exc_info=True)
        return None


def metrics(days: int = 14) -> dict:
    """Всё для «Пульса» одним ответом: маркетинг + техника. МСК-время в срезах."""
    days = max(3, min(int(days or 14), 60))
    p = {"days": days}

    today = {
        "active": int(_scalar("""SELECT count(DISTINCT username) FROM usage_event
                                 WHERE username IS NOT NULL
                                   AND created_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')
                                                     AT TIME ZONE 'Europe/Moscow'""") or 0),
        "views": int(_scalar("""SELECT count(*) FROM usage_event WHERE kind='page_view'
                                AND created_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')
                                                  AT TIME ZONE 'Europe/Moscow'""") or 0),
        "ai": int(_scalar("""SELECT count(*) FROM user_event WHERE kind='ai_query'
                             AND ts >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')
                                       AT TIME ZONE 'Europe/Moscow'""") or 0),
        "errors": int(_scalar("""SELECT count(*) FROM usage_event
                                 WHERE kind IN ('api_error','client_error')
                                   AND created_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')
                                                     AT TIME ZONE 'Europe/Moscow'""") or 0),
        "online": int(_scalar("""SELECT count(DISTINCT username) FROM usage_event
                                 WHERE username IS NOT NULL
                                   AND created_at > now() - interval '15 minutes'""") or 0),
        "users_total": int(_scalar("SELECT count(*) FROM app_user") or 0),
    }

    dau = _rows("""
        SELECT to_char(d.day, 'YYYY-MM-DD') AS d,
               COALESCE(u.users, 0) AS users, COALESCE(u.views, 0) AS views
        FROM generate_series(
               date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') - (:days - 1) * interval '1 day',
               date_trunc('day', now() AT TIME ZONE 'Europe/Moscow'), interval '1 day') AS d(day)
        LEFT JOIN (
            SELECT date_trunc('day', created_at AT TIME ZONE 'Europe/Moscow') AS day,
                   count(DISTINCT username) AS users,
                   count(*) FILTER (WHERE kind = 'page_view') AS views
            FROM usage_event
            WHERE created_at > now() - (:days || ' days')::interval AND username IS NOT NULL
            GROUP BY 1) u ON u.day = d.day
        ORDER BY d.day""", p)

    new_users = _rows("""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM app_user WHERE created_at > now() - (:days || ' days')::interval
        GROUP BY 1 ORDER BY 1""", p)

    pages = _rows("""
        SELECT v.page, v.views, v.users, COALESCE(l.total_s, 0) AS total_s
        FROM (SELECT page, count(*) AS views, count(DISTINCT username) AS users
              FROM usage_event
              WHERE kind = 'page_view' AND page IS NOT NULL
                AND created_at > now() - (:days || ' days')::interval
              GROUP BY page) v
        LEFT JOIN (SELECT page, round(sum(dur_ms) / 1000.0) AS total_s
                   FROM usage_event
                   WHERE kind = 'page_leave' AND created_at > now() - (:days || ' days')::interval
                   GROUP BY page) l ON l.page = v.page
        ORDER BY v.views DESC LIMIT 12""", p)

    ai_per_day = _rows("""
        SELECT to_char(ts AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM user_event WHERE kind = 'ai_query' AND ts > now() - (:days || ' days')::interval
        GROUP BY 1 ORDER BY 1""", p)

    features = {
        "reports": int(_scalar("""SELECT count(*) FROM report
                                  WHERE created_at > now() - (:days || ' days')::interval""", p) or 0),
        "shares": int(_scalar("""SELECT count(*) FROM user_event WHERE kind = 'share'
                                 AND ts > now() - (:days || ' days')::interval""", p) or 0),
        "ai_total": int(_scalar("""SELECT count(*) FROM user_event WHERE kind = 'ai_query'
                                   AND ts > now() - (:days || ' days')::interval""", p) or 0),
        "fb_likes": int(_scalar("""SELECT count(*) FROM item_feedback
                                   WHERE verdict = 1 AND kind IN ('news','for_you','check')
                                     AND created_at > now() - (:days || ' days')::interval""", p) or 0),
        "fb_dislikes": int(_scalar("""SELECT count(*) FROM item_feedback
                                      WHERE verdict = -1 AND kind IN ('news','for_you','check')
                                        AND created_at > now() - (:days || ' days')::interval""", p) or 0),
        "ai_likes": int(_scalar("""SELECT count(*) FROM item_feedback
                                   WHERE verdict = 1 AND kind = 'ai_answer'
                                     AND created_at > now() - (:days || ' days')::interval""", p) or 0),
        "ai_dislikes": int(_scalar("""SELECT count(*) FROM item_feedback
                                      WHERE verdict = -1 AND kind = 'ai_answer'
                                        AND created_at > now() - (:days || ' days')::interval""", p) or 0),
        "profiles": int(_scalar("""SELECT count(*) FROM app_user
                                   WHERE COALESCE(prefs->>'self_description','') <> ''""") or 0),
    }

    heatmap = _rows("""
        SELECT EXTRACT(isodow FROM created_at AT TIME ZONE 'Europe/Moscow')::int AS dow,
               EXTRACT(hour  FROM created_at AT TIME ZONE 'Europe/Moscow')::int AS hour,
               count(*) AS n
        FROM usage_event
        WHERE kind IN ('page_view', 'api_request')
          AND created_at > now() - (:days || ' days')::interval
        GROUP BY 1, 2""", p)

    latency = _rows("""
        SELECT page AS path, count(*) AS n,
               round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY dur_ms))::int AS p50,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY dur_ms))::int AS p95,
               count(*) FILTER (WHERE status >= 500) AS errs
        FROM usage_event
        WHERE kind IN ('api_request', 'api_error') AND dur_ms IS NOT NULL
          AND created_at > now() - interval '7 days'
        GROUP BY page HAVING count(*) >= 3
        ORDER BY n DESC LIMIT 12""")

    errors_recent = _rows("""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS ts,
               username, kind, page, status,
               left(COALESCE(payload->>'msg', payload->>'error', ''), 160) AS msg
        FROM usage_event
        WHERE kind IN ('api_error', 'client_error')
        ORDER BY created_at DESC LIMIT 20""")

    errors_per_day = _rows("""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM usage_event
        WHERE kind IN ('api_error', 'client_error')
          AND created_at > now() - (:days || ' days')::interval
        GROUP BY 1 ORDER BY 1""", p)

    tokens = _rows("""
        SELECT to_char(digest_date, 'YYYY-MM-DD') AS d,
               sum(COALESCE(tokens_in, 0)) AS tin, sum(COALESCE(tokens_out, 0)) AS tout
        FROM daily_digest WHERE digest_date > (now() AT TIME ZONE 'Europe/Moscow')::date - :days
        GROUP BY 1 ORDER BY 1""", p)

    digest = _rows("""
        SELECT section, status, to_char(generated_at AT TIME ZONE 'Europe/Moscow', 'HH24:MI') AS at,
               gen_ms, error
        FROM daily_digest WHERE digest_date = (SELECT max(digest_date) FROM daily_digest)
        ORDER BY section""")

    feed = _rows("""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'HH24:MI') AS ts,
               username, kind, page, dur_ms, status
        FROM usage_event
        WHERE kind IN ('page_view', 'page_leave', 'api_error', 'client_error')
        ORDER BY created_at DESC LIMIT 14""")

    reports_per_day = _rows("""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM report WHERE created_at > now() - (:days || ' days')::interval
        GROUP BY 1 ORDER BY 1""", p)

    # пофамильно: активность каждого за период + комбинированный скор для сортировки
    users_table = _rows("""
        SELECT au.username, COALESCE(au.display_name, au.username) AS name,
               COALESCE(e.days_active, 0) AS days_active,
               COALESCE(e.views, 0)       AS views,
               COALESCE(e.time_s, 0)      AS time_s,
               COALESCE(q.ai, 0)          AS ai,
               COALESCE(r.reports, 0)     AS reports,
               COALESCE(fb.ratings, 0)    AS ratings,
               to_char(au.last_seen_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS last_seen
        FROM app_user au
        LEFT JOIN (SELECT username,
                          count(DISTINCT date_trunc('day', created_at AT TIME ZONE 'Europe/Moscow')) AS days_active,
                          count(*) FILTER (WHERE kind = 'page_view') AS views,
                          round(COALESCE(sum(dur_ms) FILTER (WHERE kind = 'page_leave'), 0) / 1000.0) AS time_s
                   FROM usage_event
                   WHERE created_at > now() - (:days || ' days')::interval AND username IS NOT NULL
                   GROUP BY 1) e USING (username)
        LEFT JOIN (SELECT username, count(*) AS ai FROM user_event
                   WHERE kind = 'ai_query' AND ts > now() - (:days || ' days')::interval
                   GROUP BY 1) q USING (username)
        LEFT JOIN (SELECT username, count(*) AS reports FROM report
                   WHERE created_at > now() - (:days || ' days')::interval
                   GROUP BY 1) r USING (username)
        LEFT JOIN (SELECT username, count(*) AS ratings FROM item_feedback
                   WHERE created_at > now() - (:days || ' days')::interval
                   GROUP BY 1) fb USING (username)
        ORDER BY (COALESCE(e.time_s, 0) / 60.0 + COALESCE(e.views, 0) * 2
                  + COALESCE(q.ai, 0) * 15 + COALESCE(r.reports, 0) * 30
                  + COALESCE(fb.ratings, 0) * 5) DESC
        LIMIT 15""", p)

    # сегменты аудитории: исследователи (ИИ) / читатели новостей / разовые / спящие
    seg_rows = _rows("""
        SELECT e.username, COALESCE(a.n, 0) AS ai, e.views, e.news_views
        FROM (SELECT username,
                     count(*) FILTER (WHERE kind = 'page_view') AS views,
                     count(*) FILTER (WHERE kind = 'page_view'
                                      AND page IN ('overview', 'foryou')) AS news_views
              FROM usage_event
              WHERE created_at > now() - (:days || ' days')::interval AND username IS NOT NULL
              GROUP BY 1) e
        LEFT JOIN (SELECT username, count(*) AS n FROM user_event
                   WHERE kind = 'ai_query' AND ts > now() - (:days || ' days')::interval
                   GROUP BY 1) a USING (username)""", p)
    researchers = sum(1 for r in seg_rows if (r.get("ai") or 0) > 0)
    readers = sum(1 for r in seg_rows
                  if not (r.get("ai") or 0) and (r.get("views") or 0) > 0
                  and (r.get("news_views") or 0) >= (r.get("views") or 1) * 0.6)
    casual = max(len(seg_rows) - researchers - readers, 0)
    segments = {"researchers": researchers, "readers": readers, "casual": casual,
                "sleepers": max(today["users_total"] - len(seg_rows), 0),
                "active": len(seg_rows)}

    features["report_opens"] = int(_scalar("""SELECT count(*) FROM user_event
                                              WHERE kind = 'report_open'
                                                AND ts > now() - (:days || ' days')::interval""",
                                           p) or 0)

    return {"days": days, "today": today, "dau": dau, "new_users": new_users,
            "pages": pages, "ai_per_day": ai_per_day, "features": features,
            "heatmap": heatmap, "latency": latency,
            "errors_recent": errors_recent, "errors_per_day": errors_per_day,
            "tokens": tokens, "digest": digest, "feed": feed,
            "reports_per_day": reports_per_day, "users_table": users_table,
            "segments": segments,
            "ai_feedback": _ai_feedback(days),
            "persona": _persona(),
            "proposals": _proposals(),
            "ingest": _ingest_health(days),
            "collect": _collect_health(days),
            "topics": _team_topics(days)}


# ── оценки ответов ИИ: «что разбирать» ───────────────────────────────────────
# Владелец видит и жалобы, и похвалы. Текст жалобы показывать этично: кнопка
# подписана «Плохой ответ — команда разберёт», пользователь отправляет это
# команде осознанно. Тексты ОБЫЧНЫХ вопросов к ИИ сюда не попадают — в аудите
# «кто что проверяет» это план проверок коллеги.

AIFB_REASON_RU = {"offtopic": "не по делу", "shallow": "мало конкретики",
                  "wrong": "ошибка в данных", "long": "слишком длинно"}


def _ai_feedback(days: int, limit: int = 20) -> dict:
    p = {"days": days, "lim": limit}
    rows = _rows("""
        SELECT username, verdict, created_at,
               payload->>'question'   AS question,
               payload->>'comment'    AS comment,
               payload->>'mode'       AS mode,
               payload->'reasons'     AS reasons,
               payload->>'report_id'  AS report_id
          FROM item_feedback
         WHERE kind = 'ai_answer'
           AND created_at > now() - (:days || ' days')::interval
         ORDER BY created_at DESC LIMIT :lim""", p)
    out = []
    counts: dict[str, int] = {}
    for r in rows:
        reasons = r.get("reasons")
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except Exception:  # noqa: BLE001
                reasons = []
        reasons = [x for x in (reasons or []) if x]
        for x in reasons:
            counts[x] = counts.get(x, 0) + 1
        out.append({
            "username": r.get("username"), "verdict": int(r.get("verdict") or 0),
            "question": (r.get("question") or "")[:300],
            "comment": (r.get("comment") or "")[:300],
            "mode": r.get("mode"), "report_id": r.get("report_id"),
            "reasons": [AIFB_REASON_RU.get(x, x) for x in reasons],
            "created_at": str(r.get("created_at") or ""),
        })
    likes = sum(1 for x in out if x["verdict"] > 0)
    dislikes = sum(1 for x in out if x["verdict"] < 0)
    # Знаменатель покрытия: сколько ответов вообще выдали за период.
    answers = int(_scalar("""SELECT count(*) FROM user_event WHERE kind = 'ai_query'
                             AND ts > now() - (:days || ' days')::interval""",
                          {"days": days}) or 0)
    return {"items": out, "likes": likes, "dislikes": dislikes,
            "answers": answers,
            "reasons": sorted(({"key": k, "label": AIFB_REASON_RU.get(k, k), "n": v}
                               for k, v in counts.items()), key=lambda x: -x["n"])}


# ── готовность к персонализации ──────────────────────────────────────────────
# Формула повторяет userdata.personalization_score, но одним запросом на всех:
# там ~5 SQL на человека, а «Пульс» перезапрашивается раз в минуту.
# Слагаемое «регулярное использование» не показываем — оно даёт 5 из 5 всем
# безусловно и только размывает картину.
PERSONA_PARTS = [
    ("desc",       "Описание зоны ответственности", 25),
    ("ratings",    "5+ оценок в «Для вас»",         20),
    ("focus",      "3+ темы в фокусе",              15),
    ("queries",    "5+ вопросов ИИ",                15),
    ("ai_ratings", "3+ оценки ответов ИИ",          10),
    ("note",       "ИИ-нарратив собран",            10),
]


def _persona() -> dict:
    rows = _rows("""
        SELECT au.username, COALESCE(au.display_name, au.username) AS name,
               length(COALESCE(au.prefs->>'self_description','')) AS desc_len,
               (au.profile_note IS NOT NULL AND au.profile_note <> '') AS has_note,
               COALESCE(au.prefs->>'personal_digest','') <> 'false' AS personal_on,
               (SELECT count(DISTINCT x) FROM (
                    SELECT jsonb_object_keys(COALESCE(au.interests->'counters'->'products','{}'::jsonb)) x
                    UNION SELECT jsonb_array_elements_text(COALESCE(au.interests->'pinned','[]'::jsonb))
                    UNION SELECT jsonb_array_elements_text(COALESCE(au.interests->'custom','[]'::jsonb))
               ) f) AS focus_n,
               (SELECT count(*) FROM user_event ue
                 WHERE ue.username = au.username AND ue.kind = 'ai_query') AS q_n,
               (SELECT count(*) FROM item_feedback f2
                 WHERE f2.username = au.username
                   AND f2.kind IN ('news','for_you','check')) AS fb_n,
               (SELECT count(*) FROM item_feedback f3
                 WHERE f3.username = au.username AND f3.kind = 'ai_answer') AS ai_n
          FROM app_user au ORDER BY 2""")
    out = []
    for r in rows:
        earned = {
            "desc":       25 * min((r["desc_len"] or 0) / 40, 1),
            "ratings":    20 * min((r["fb_n"] or 0) / 5, 1),
            "focus":      15 * min((r["focus_n"] or 0) / 3, 1),
            "queries":    15 * min((r["q_n"] or 0) / 5, 1),
            "ai_ratings": 10 * min((r["ai_n"] or 0) / 3, 1),
            "note":       10 if r["has_note"] else 0,
        }
        # 95, а не 100: пятое слагаемое «регулярное использование» скрыто
        score = round(sum(earned.values()) / 95 * 100)
        out.append({"username": r["username"], "name": r["name"],
                    "score": min(100, score),
                    "personal_on": bool(r["personal_on"]),
                    "parts": {k: earned[k] >= mx for k, _, mx in PERSONA_PARTS}})
    # Где упирается больше всего людей — это про инструмент, а не про людей
    gaps = []
    if out:
        for key, label, _mx in PERSONA_PARTS:
            miss = sum(1 for u in out if not u["parts"][key])
            if miss:
                gaps.append({"key": key, "label": label, "miss": miss})
        gaps.sort(key=lambda x: -x["miss"])
    scores = sorted(u["score"] for u in out)
    median = scores[len(scores) // 2] if scores else 0
    return {"users": out, "median": median, "gaps": gaps[:3],
            "parts": [{"key": k, "label": l} for k, l, _ in PERSONA_PARTS]}


def _proposals() -> dict:
    """Заявки на источники — прямая очередь задач владельца."""
    rows = _rows("""
        SELECT proposal_id, purpose, domain, title,
               COALESCE(proposer_name, proposed_by) AS author,
               created_at,
               EXTRACT(day FROM now() - created_at)::int AS age_days
          FROM source_proposal WHERE status = 'pending'
         ORDER BY created_at LIMIT 10""")
    return {"pending": len(rows),
            "oldest_days": max((r["age_days"] or 0) for r in rows) if rows else 0,
            "items": [{**r, "created_at": str(r["created_at"])} for r in rows]}


def _ingest_health(days: int) -> dict:
    """Фоновая индексация: доходит ли до конца.

    Счётчики очереди живут в памяти процесса и обнуляются рестартом — это
    подписано в интерфейсе, иначе «0 в очереди» читалось бы как «фон умер».
    """
    q = {}
    try:
        from ..rag import ingest_queue
        q = ingest_queue.stats()
    except Exception:  # noqa: BLE001 — сбой импорта не должен ронять весь «Пульс»
        q = {}
    hist = _rows("""
        SELECT date_trunc('day', created_at AT TIME ZONE 'Europe/Moscow')::date AS d,
               count(*) AS n,
               count(*) FILTER (WHERE status = 204) AS empty,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY dur_ms)) AS p50,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY dur_ms)) AS p95
          FROM usage_event
         WHERE kind = 'rag_ingest'
           AND created_at > now() - (:days || ' days')::interval
         GROUP BY 1 ORDER BY 1""", {"days": days})
    return {"queue": q, "per_day": [{**r, "d": str(r["d"])} for r in hist]}


def _collect_health(days: int) -> dict:
    """Что не доехало до базы знаний. Две группы, а не одна: «не дошло»
    (капча, сеть) и «дошло, но индексировать нечего» (дубль, пустая страница).
    Смешивать нельзя — первое требует вмешательства, второе штатно."""
    rows = _rows("""
        SELECT COALESCE(skipped_reason, 'ok') AS reason, count(*) AS n
          FROM document_origin
         WHERE created_at > now() - (:days || ' days')::interval
         GROUP BY 1 ORDER BY 2 DESC""", {"days": days})
    HARD = {"captcha", "fetch_failed", "empty_after_parse"}
    RU = {"ok": "проиндексировано", "duplicate": "уже было",
          "captcha": "капча", "fetch_failed": "не загрузилось",
          "empty_after_parse": "пустая страница", "no_chunks": "нечего индексировать",
          "sponsored_or_low_trust": "реклама / низкое доверие"}
    ok = sum(r["n"] for r in rows if r["reason"] == "ok")
    hard = sum(r["n"] for r in rows if r["reason"] in HARD)
    soft = sum(r["n"] for r in rows if r["reason"] not in HARD and r["reason"] != "ok")
    domains = _rows("""
        SELECT split_part(url, '/', 3) AS domain, count(*) AS n
          FROM document_origin
         WHERE skipped_reason = ANY(:hard)
           AND created_at > now() - (:days || ' days')::interval
         GROUP BY 1 ORDER BY 2 DESC LIMIT 6""",
        {"days": days, "hard": list(HARD)})
    return {"ok": ok, "hard": hard, "soft": soft,
            "reasons": [{"key": r["reason"], "label": RU.get(r["reason"], r["reason"]),
                         "n": r["n"], "hard": r["reason"] in HARD} for r in rows],
            "domains": domains}


def _team_topics(days: int) -> dict:
    """Что проверяет отдел — агрегат по команде, без имён и без текстов вопросов."""
    banks = _rows("""
        SELECT b AS name, count(*) AS n
          FROM report, unnest(banks) b
         WHERE created_at > now() - (:days || ' days')::interval
         GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", {"days": days})
    return {"banks": banks}
