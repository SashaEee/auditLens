"""Телеметрия использования + метрики дашборда «Пульс» (миграция 016).

Два потока событий в usage_event:
  • фронт: page_view / page_leave(dur_ms) / client_error — батчами через /api/track;
  • бекенд: api_request / api_error — HTTP-middleware (латентность, статусы, исключения).

Доступ к метрикам: владелец (env ADMIN_USERS) и те, кому открыт «Пульс» (env
PULSE_USERS) — списки username через запятую. Права владельца (пересборка выпуска,
заявки на источники, служебные учётки, прогон набора) — только ADMIN_USERS.
Имена в коде не хардкодим — репозиторий публичный.

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
_CLIENT_KINDS = {"page_view", "page_leave", "client_error", "ui", "news_click"}
_MAX_BATCH = 25
_MAX_DUR_MS = 30 * 60 * 1000          # страница «висела» дольше 30 мин → кап
_MAX_AGE_MS = 6 * 3600 * 1000         # возраст события из очереди фронта — не старше 6 ч
# события «человек что-то делает» — по ним двигается «был(а)»
_PRESENCE_KINDS = {"page_view", "page_leave", "news_click", "ui"}

_ID_RE = re.compile(r"/\d+")


def norm_path(path: str) -> str:
    """Нормализация /api-пути для группировки латентности: /api/reports/17 → /api/reports/:id."""
    return _ID_RE.sub("/:id", path or "")[:120]


def _env_users(name: str) -> set[str]:
    return {u.strip() for u in os.getenv(name, "").split(",") if u.strip()}


def is_admin(username: str | None) -> bool:
    """Владелец инструмента: всё, включая пересборку выпуска для всех."""
    return bool(username) and username in _env_users("ADMIN_USERS")


def can_pulse(username: str | None) -> bool:
    """Видит «Пульс» (и служебный просмотр отчётов и диалогов из него), отвечает
    на обращения. Владелец — всегда; остальные — по env PULSE_USERS."""
    return bool(username) and (username in _env_users("ADMIN_USERS")
                               or username in _env_users("PULSE_USERS"))


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


def _age_ms(ev: dict) -> int:
    """Возраст события из очереди фронта, мс. Время события = «сейчас минус
    возраст»: фронт шлёт пачками, и без возраста восемь событий получали одну
    метку — хронология в карточке человека путала порядок. Часам браузера не верим."""
    try:
        return min(max(int(ev.get("age_ms") or 0), 0), _MAX_AGE_MS)
    except (TypeError, ValueError):
        return 0


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
        age = _age_ms(ev)
        rows.append({"u": username, "k": kind, "p": str(ev.get("page") or "")[:60] or None,
                     "d": dur, "age": age,
                     "pl": json.dumps(ev.get("payload") or {}, ensure_ascii=False,
                                      default=str)[:1000]})
        accepted += 1
    if not rows:
        return 0
    try:
        with db.session() as s:
            s.execute(text("""
                INSERT INTO usage_event (username, kind, page, dur_ms, payload, created_at)
                VALUES (:u, :k, :p, :d, CAST(:pl AS jsonb),
                        now() - CAST(:age AS integer) * interval '1 millisecond')
            """), rows)
            # «был(а)» двигали только загрузка приложения, «Для вас» и вопросы ИИ:
            # переходы по разделам его не трогали, и в «Пульсе» человек «был» на
            # часы раньше, чем показывала его же хронология
            if any(r["k"] in _PRESENCE_KINDS for r in rows):
                s.execute(text("""UPDATE app_user SET last_seen_at = greatest(last_seen_at, now())
                                  WHERE username = :u"""), {"u": username})
    except Exception:
        log.warning("[telemetry] track_batch failed", exc_info=True)
        return 0
    # клик по новости — сигнал интереса (этап A): заголовок и продуктовые слаги
    # плитки учат профиль с весом 0.5 (между фильтром 0.3 и вопросом ИИ 1.0)
    try:
        from . import userdata
        for ev in (events or []):
            if str(ev.get("kind")) != "news_click":
                continue
            pl = ev.get("payload") or {}
            userdata.update_interests_from_signal(
                username, text_=str(pl.get("title") or ""),
                products=[s_ for s_ in (pl.get("slugs") or []) if s_][:5],
                weight=0.5)
    except Exception:  # noqa: BLE001
        log.debug("[telemetry] news_click interests failed", exc_info=True)
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


# ── кого считать ─────────────────────────────────────────────────────────────
# Аудит 03.10: 70% просмотров «Пульса» давали владелец (тестирует инструмент)
# и служебные учётки (локальная разработка, админ входа). Метрики про людей
# считаются без них: служебные помечаются владельцем в карточке человека
# (prefs.pulse_hidden), себя владелец включает переключателем «со мной».
# Технические блоки (ошибки, латентность, сбор) считаются по всем.

_NOBODY = "-"          # логином не бывает; пустой массив PG не выводит по типу


def hidden_users() -> list[str]:
    return [r["username"] for r in _rows(
        "SELECT username FROM app_user WHERE prefs->>'pulse_hidden' = 'true' ORDER BY 1")]


def with_me_default(viewer: str | None) -> bool:
    """Считать ли смотрящего по умолчанию. Владелец помечен служебным (он
    проверяет инструмент) — себя не считает; коллеги с доступом к «Пульсу» —
    обычные пользователи, их действия по умолчанию в счёт."""
    return bool(viewer) and viewer not in set(hidden_users())


def excluded(viewer: str | None, with_me: bool | None = None) -> list[str]:
    """Кого не считать в метриках про людей: служебные учётки; смотрящий —
    по переключателю «со мной» (None — по умолчанию, см. with_me_default)."""
    ex = set(hidden_users())
    if viewer:
        if with_me is None:
            with_me = viewer not in ex
        if with_me:
            ex.discard(viewer)
        else:
            ex.add(viewer)
    return sorted(ex)


def set_hidden(username: str, hidden: bool) -> bool:
    """Пометить учётку служебной (не считать в «Пульсе») или снять пометку."""
    try:
        with db.session() as s:
            n = s.execute(text("""
                UPDATE app_user
                   SET prefs = COALESCE(prefs, '{}'::jsonb) || jsonb_build_object('pulse_hidden', CAST(:h AS boolean))
                 WHERE username = :u"""), {"u": username, "h": bool(hidden)}).rowcount
        return bool(n)
    except Exception:
        log.warning("[telemetry] set_hidden failed", exc_info=True)
        return False


def _ex(exclude: list[str] | None) -> list[str]:
    return sorted(set(exclude or [])) or [_NOBODY]


# Период — календарные дни по МСК: «14 дн» = сегодня и 13 полных дней до него.
# Раньше брали now() − 14 суток, и первый день графика был обрезан (с 10:36).
_SINCE = ("((date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') - (:days - 1) * interval '1 day')"
          " AT TIME ZONE 'Europe/Moscow')")
_TODAY = "(date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow')"
# человек, а не служебная учётка и не владелец (если он не включил себя)
_PPL = "COALESCE({col}, '') <> ALL(:ex)"
# «что-то делал»: открыл страницу, кликнул, запрос к API — но не автообновление
# «Пульса»: опрос раз в минуту держал владельца «онлайн» и заливал тепловую карту
_HUMAN = ("(kind IN ('page_view', 'page_leave', 'news_click', 'client_error', 'ui')"
          " OR (kind IN ('api_request', 'api_error') AND COALESCE(page, '') NOT LIKE '/api/admin/%'))")


def _ppl(col: str = "username") -> str:
    return _PPL.format(col=col)


def metrics(days: int = 14, exclude: list[str] | None = None) -> dict:
    """Всё для «Пульса» одним ответом: люди + продукт + техника. МСК-время в срезах.

    exclude — кого не считать в метриках про людей (служебные учётки, владелец)."""
    days = max(3, min(int(days or 14), 60))
    ex = _ex(exclude)
    p = {"days": days, "ex": ex}
    P = _ppl()

    today = {
        # активный = открывал страницы; фоновые запросы открытой вкладки не в счёт
        "active": int(_scalar(f"""SELECT count(DISTINCT username) FROM usage_event
                                  WHERE kind = 'page_view' AND {P} AND created_at >= {_TODAY}""", p) or 0),
        "views": int(_scalar(f"""SELECT count(*) FROM usage_event
                                 WHERE kind = 'page_view' AND {P} AND created_at >= {_TODAY}""", p) or 0),
        "ai": int(_scalar(f"""SELECT count(*) FROM user_event
                              WHERE kind = 'ai_query' AND {P} AND ts >= {_TODAY}""", p) or 0),
        # ошибки — техника: считаем у всех, включая владельца
        "errors": int(_scalar(f"""SELECT count(*) FROM usage_event
                                  WHERE kind IN ('api_error', 'client_error') AND created_at >= {_TODAY}""") or 0),
        "online": int(_scalar(f"""SELECT count(DISTINCT username) FROM usage_event
                                  WHERE username IS NOT NULL AND {P} AND {_HUMAN}
                                    AND created_at > now() - interval '15 minutes'""", p) or 0),
        "users_total": int(_scalar(f"SELECT count(*) FROM app_user WHERE {P}", p) or 0),
    }

    dau = _rows(f"""
        SELECT to_char(d.day, 'YYYY-MM-DD') AS d,
               COALESCE(u.users, 0) AS users, COALESCE(u.views, 0) AS views
        FROM generate_series(
               date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') - (:days - 1) * interval '1 day',
               date_trunc('day', now() AT TIME ZONE 'Europe/Moscow'), interval '1 day') AS d(day)
        LEFT JOIN (
            SELECT date_trunc('day', created_at AT TIME ZONE 'Europe/Moscow') AS day,
                   count(DISTINCT username) AS users, count(*) AS views
            FROM usage_event
            WHERE kind = 'page_view' AND created_at >= {_SINCE} AND username IS NOT NULL AND {P}
            GROUP BY 1) u ON u.day = d.day
        ORDER BY d.day""", p)

    new_users = _rows(f"""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM app_user WHERE created_at >= {_SINCE} AND {P}
        GROUP BY 1 ORDER BY 1""", p)

    pages = _rows(f"""
        SELECT v.page, v.views, v.users, COALESCE(l.total_s, 0) AS total_s
        FROM (SELECT page, count(*) AS views, count(DISTINCT username) AS users
              FROM usage_event
              WHERE kind = 'page_view' AND page IS NOT NULL AND {P}
                AND created_at >= {_SINCE}
              GROUP BY page) v
        LEFT JOIN (SELECT page, round(sum(dur_ms) / 1000.0) AS total_s
                   FROM usage_event
                   WHERE kind = 'page_leave' AND {P} AND created_at >= {_SINCE}
                   GROUP BY page) l ON l.page = v.page
        ORDER BY v.views DESC LIMIT 12""", p)

    ai_per_day = _rows(f"""
        SELECT to_char(ts AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM user_event WHERE kind = 'ai_query' AND {P} AND ts >= {_SINCE}
        GROUP BY 1 ORDER BY 1""", p)

    # report хранит и отчёты (mode=deep), и сохранённые быстрые ответы (mode=quick):
    # «Аудит-отчёты создано: 88» на деле было 47 отчётов и 41 быстрый ответ
    rep = _rows(f"""
        SELECT count(*) FILTER (WHERE COALESCE(payload->>'mode', 'deep') <> 'quick') AS deep,
               count(*) FILTER (WHERE payload->>'mode' = 'quick') AS quick
          FROM report WHERE {P} AND created_at >= {_SINCE}""", p)
    rep = rep[0] if rep else {}
    fb = _rows(f"""
        SELECT count(*) FILTER (WHERE verdict = 1 AND kind IN ('news', 'for_you', 'check', 'digest_card')) AS fb_likes,
               count(*) FILTER (WHERE verdict = -1 AND kind IN ('news', 'for_you', 'check', 'digest_card')) AS fb_dislikes,
               count(*) FILTER (WHERE verdict = 1 AND kind = 'ai_answer') AS ai_likes,
               count(*) FILTER (WHERE verdict = -1 AND kind = 'ai_answer') AS ai_dislikes
          FROM item_feedback WHERE {P} AND created_at >= {_SINCE}""", p)
    fb = fb[0] if fb else {}
    ev = _rows(f"""
        SELECT count(*) FILTER (WHERE kind = 'ai_query') AS ai_total,
               count(*) FILTER (WHERE kind = 'ai_query' AND payload->>'deep_requested' = 'true') AS ai_deep,
               count(*) FILTER (WHERE kind = 'share') AS shares,
               count(*) FILTER (WHERE kind = 'report_open') AS report_opens
          FROM user_event WHERE {P} AND ts >= {_SINCE}""", p)
    ev = ev[0] if ev else {}
    # итоги прогонов ИИ-помощника (событие ai_run_end с 04.10): заказ ≠ отчёт —
    # сорванные и остановленные прогоны раньше не было видно нигде (ПУЛ-02)
    runs = _rows(f"""
        SELECT count(*) FILTER (WHERE payload->>'status' = 'ok') AS ok,
               count(*) FILTER (WHERE payload->>'status' = 'failed') AS failed,
               count(*) FILTER (WHERE payload->>'status' = 'stopped') AS stopped,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY (payload->>'elapsed_s')::numeric)
                   FILTER (WHERE payload->>'status' = 'ok' AND payload->>'mode' = 'deep') AS deep_p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY (payload->>'elapsed_s')::numeric)
                   FILTER (WHERE payload->>'status' = 'ok' AND payload->>'mode' = 'deep') AS deep_p95
          FROM user_event WHERE kind = 'ai_run_end' AND {P} AND ts >= {_SINCE}""", p)
    runs = runs[0] if runs else {}
    # когда последний раз хоть кто-то ставил оценку: с конца августа — никто,
    # и «жалоб нет» читалось как «всё хорошо»
    last_fb = _rows(f"""
        SELECT kind, to_char(max(created_at) AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d,
               EXTRACT(day FROM now() - max(created_at))::int AS age_days
          FROM item_feedback WHERE {P} GROUP BY 1 ORDER BY max(created_at) DESC""", p)
    features = {
        "reports": int(rep.get("deep") or 0),
        "quick_saved": int(rep.get("quick") or 0),
        "shares": int(ev.get("shares") or 0),
        "ai_total": int(ev.get("ai_total") or 0),
        "ai_deep": int(ev.get("ai_deep") or 0),
        "runs_ok": int(runs.get("ok") or 0),
        "runs_failed": int(runs.get("failed") or 0),
        "runs_stopped": int(runs.get("stopped") or 0),
        "deep_p50_s": round(float(runs["deep_p50"])) if runs.get("deep_p50") is not None else None,
        "deep_p95_s": round(float(runs["deep_p95"])) if runs.get("deep_p95") is not None else None,
        "report_opens": int(ev.get("report_opens") or 0),
        "fb_likes": int(fb.get("fb_likes") or 0),
        "fb_dislikes": int(fb.get("fb_dislikes") or 0),
        "ai_likes": int(fb.get("ai_likes") or 0),
        "ai_dislikes": int(fb.get("ai_dislikes") or 0),
        # за всё время — профиль заполняют один раз
        "profiles": int(_scalar(f"""SELECT count(*) FROM app_user
                                    WHERE COALESCE(prefs->>'self_description','') <> '' AND {P}""", p) or 0),
        "ratings_last": last_fb,
    }

    # «Когда пользуются» — по открытиям страниц. Раньше 86% клеток были фоновыми
    # запросами API (из них 2,5 тыс. — автообновление самого «Пульса»)
    heatmap = _rows(f"""
        SELECT EXTRACT(isodow FROM created_at AT TIME ZONE 'Europe/Moscow')::int AS dow,
               EXTRACT(hour  FROM created_at AT TIME ZONE 'Europe/Moscow')::int AS hour,
               count(*) AS n
        FROM usage_event
        WHERE kind = 'page_view' AND {P} AND created_at >= {_SINCE}
        GROUP BY 1, 2""", p)

    # служебные адреса «Пульса» — опрос владельца раз в минуту, а не нагрузка людей
    latency = _rows("""
        SELECT page AS path, count(*) AS n,
               round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY dur_ms))::int AS p50,
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY dur_ms))::int AS p95,
               count(*) FILTER (WHERE status >= 500) AS errs
        FROM usage_event
        WHERE kind IN ('api_request', 'api_error') AND dur_ms IS NOT NULL
          AND COALESCE(page, '') NOT LIKE '/api/admin/%'
          AND created_at > now() - interval '7 days'
        GROUP BY page HAVING count(*) >= 3
        ORDER BY n DESC LIMIT 12""")

    # ошибки за период, одинаковые склеены: «Script error.» ×3 вместо трёх строк.
    # Ключ текста у API — error, у браузера — msg
    errors_recent = _rows(f"""
        SELECT kind, page, max(status) AS status,
               left(COALESCE(payload->>'msg', payload->>'error', payload->>'message', ''), 160) AS msg,
               count(*) AS n, count(DISTINCT username) AS users,
               to_char(max(created_at) AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS ts
        FROM usage_event
        WHERE kind IN ('api_error', 'client_error') AND created_at >= {_SINCE}
        GROUP BY 1, 2, 4
        ORDER BY max(created_at) DESC LIMIT 20""", p)
    errors_total = int(_scalar(f"""SELECT count(*) FROM usage_event
                                   WHERE kind IN ('api_error', 'client_error')
                                     AND created_at >= {_SINCE}""", p) or 0)

    errors_per_day = _rows(f"""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM usage_event
        WHERE kind IN ('api_error', 'client_error') AND created_at >= {_SINCE}
        GROUP BY 1 ORDER BY 1""", p)

    # токены ЕЖЕДНЕВНОГО ВЫПУСКА: расход ИИ-помощника нигде не пишется
    tokens = _rows("""
        SELECT to_char(digest_date, 'YYYY-MM-DD') AS d,
               sum(COALESCE(tokens_in, 0)) AS tin, sum(COALESCE(tokens_out, 0)) AS tout
        FROM daily_digest WHERE digest_date > (now() AT TIME ZONE 'Europe/Moscow')::date - :days
        GROUP BY 1 ORDER BY 1""", p)

    digest = _rows("""
        SELECT section, status, to_char(digest_date, 'YYYY-MM-DD') AS d,
               to_char(generated_at AT TIME ZONE 'Europe/Moscow', 'HH24:MI') AS at,
               gen_ms, error
        FROM daily_digest WHERE digest_date = (SELECT max(digest_date) FROM daily_digest)
        ORDER BY section""")

    # живая лента: кто что открыл и спросил — с именами; закрытия страниц не
    # показываем (вдвое длиннее лента, «Пульс · 0с» ничего не говорит)
    feed = _rows(f"""
        SELECT CASE WHEN x.at >= {_TODAY}
                    THEN to_char(x.at AT TIME ZONE 'Europe/Moscow', 'HH24:MI')
                    ELSE to_char(x.at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') END AS ts,
               x.username, COALESCE(au.display_name, x.username) AS name,
               x.kind, x.page, x.status, x.deep
          FROM (SELECT created_at AS at, username, kind, page, status, NULL::boolean AS deep
                  FROM usage_event
                 WHERE kind IN ('page_view', 'api_error', 'client_error') AND {_ppl('username')}
                   AND created_at > now() - interval '30 days'
                UNION ALL
                SELECT ts, username, kind, NULL, NULL, (payload->>'deep_requested') = 'true'
                  FROM user_event
                 WHERE kind IN ('ai_query', 'share', 'report_open') AND {_ppl('username')}
                   AND ts > now() - interval '30 days') x
          LEFT JOIN app_user au ON au.username = x.username
         ORDER BY x.at DESC LIMIT 16""", p)

    reports_per_day = _rows(f"""
        SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d, count(*) AS n
        FROM report WHERE COALESCE(payload->>'mode', 'deep') <> 'quick' AND {P}
          AND created_at >= {_SINCE}
        GROUP BY 1 ORDER BY 1""", p)

    # сегменты аудитории: исследователи (ИИ) / читатели новостей / разовые / молчат.
    # «Активный» — тот же, что в таблице людей: открывал страницы за период
    seg_rows = _rows(f"""
        SELECT e.username, COALESCE(a.n, 0) AS ai, e.views, e.news_views
        FROM (SELECT username, count(*) AS views,
                     count(*) FILTER (WHERE page IN ('overview', 'foryou')) AS news_views
              FROM usage_event
              WHERE kind = 'page_view' AND created_at >= {_SINCE} AND username IS NOT NULL AND {P}
              GROUP BY 1) e
        LEFT JOIN (SELECT username, count(*) AS n FROM user_event
                   WHERE kind = 'ai_query' AND ts >= {_SINCE} AND {P}
                   GROUP BY 1) a USING (username)""", p)
    researchers = sum(1 for r in seg_rows if (r.get("ai") or 0) > 0)
    readers = sum(1 for r in seg_rows
                  if not (r.get("ai") or 0) and (r.get("views") or 0) > 0
                  and (r.get("news_views") or 0) >= (r.get("views") or 1) * 0.6)
    casual = max(len(seg_rows) - researchers - readers, 0)
    segments = {"researchers": researchers, "readers": readers, "casual": casual,
                "sleepers": max(today["users_total"] - len(seg_rows), 0),
                "active": len(seg_rows)}

    return {"days": days, "excluded": len(exclude or []),
            "today": today, "dau": dau, "new_users": new_users,
            "pages": pages, "ai_per_day": ai_per_day, "features": features,
            "heatmap": heatmap, "latency": latency,
            "errors_recent": errors_recent, "errors_total": errors_total,
            "errors_per_day": errors_per_day,
            "tokens": tokens, "digest": digest, "feed": feed,
            "reports_per_day": reports_per_day,
            "segments": segments,
            "ai_feedback": _ai_feedback(days, ex),
            "persona": _persona(ex),
            "proposals": _proposals(),
            "ingest": _ingest_health(days),
            "collect": _collect_health(days),
            "search": _search_health(days),
            "news_quality": _news_quality(days, ex),
            "review_sources": _review_sources(),
            "signal_journal": _signal_journal(),
            "personalization": _personalization(days, ex),
            "topics": _team_topics(days, ex),
            "eval": _eval_brief(),
            "inbox": _inbox_brief(),
            "mail": _mail_brief(days, ex)}


def _mail_brief(days: int, ex: list[str] | None = None) -> dict:
    """Своя почта для уведомлений (миграция 088): сколько и кто подключил, сколько
    писем ушло, как сработала заметка-приглашение. Счёт — без служебных учёток;
    в списке они остаются с пометкой, иначе владелец не увидит свой адрес."""
    try:
        from . import mail_delivery as MD
        p = {"days": days, "ex": _ex(ex)}
        people = _rows(f"""
            SELECT au.username, COALESCE(au.display_name, au.username) AS name, au.email,
                   au.email_source AS source,
                   to_char(au.email_at AT TIME ZONE 'Europe/Moscow', 'DD.MM.YYYY') AS since,
                   COALESCE(l.sent, 0) AS sent, COALESCE(l.failed, 0) AS failed,
                   to_char(l.last_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS last_mail,
                   NOT {_ppl('au.username')} AS excluded,
                   EXISTS (SELECT 1 FROM usage_event e WHERE e.username = au.username
                            AND e.kind = 'page_view' AND e.created_at >= {_SINCE}) AS active
              FROM app_user au
              LEFT JOIN (SELECT username,
                                count(*) FILTER (WHERE ok AND kind IN ('instant', 'digest')) AS sent,
                                count(*) FILTER (WHERE NOT ok) AS failed,
                                max(sent_at) FILTER (WHERE ok AND kind IN ('instant', 'digest')) AS last_at
                           FROM app_mail_log WHERE sent_at >= {_SINCE} GROUP BY 1) l USING (username)
             WHERE au.email IS NOT NULL AND au.email_source IN ('user', 'sso')
             ORDER BY au.email_at DESC NULLS LAST""", p)
        for r in people:
            r["kind"] = ("sso" if r["source"] == "sso" else
                         "sigma" if MD.is_corporate(r["email"]) else "private")
        ppl = [r for r in people if not r["excluded"]]
        active = int(_scalar(f"""SELECT count(DISTINCT username) FROM usage_event
                                 WHERE kind = 'page_view' AND {_ppl()} AND created_at >= {_SINCE}""", p) or 0)
        pending = int(_scalar(f"""SELECT count(*) FROM app_email_verify v
                                  WHERE v.expires_at > now() AND {_ppl('v.username')}""", p) or 0)
        sent = _rows(f"""SELECT kind, count(*) FILTER (WHERE ok) AS ok, count(*) FILTER (WHERE NOT ok) AS bad
                           FROM app_mail_log WHERE kind <> 'test' AND sent_at >= {_SINCE}
                            AND {_ppl()} GROUP BY 1""", p)
        promo = {r["step"]: int(r["n"]) for r in _rows(f"""
            SELECT payload->>'step' AS step, count(DISTINCT username) AS n FROM usage_event
             WHERE kind = 'ui' AND payload->>'action' = 'mail_promo' AND {_ppl()}
               AND created_at >= {_SINCE} GROUP BY 1""", p) if r.get("step")}
    except Exception:  # noqa: BLE001 — до миграции 088 «Пульс» не падает
        log.debug("[telemetry] mail brief failed", exc_info=True)
        return {}
    by = {r["kind"]: r for r in sent}
    return {"connected": len(ppl), "sigma": sum(1 for r in ppl if r["kind"] == "sigma"),
            "private": sum(1 for r in ppl if r["kind"] == "private"),
            "sso": sum(1 for r in ppl if r["kind"] == "sso"),
            "active": active, "active_connected": sum(1 for r in ppl if r["active"]),
            "pending": pending,
            "sent": sum(int(by.get(k, {}).get("ok") or 0) for k in ("instant", "digest")),
            "codes": int(by.get("verify", {}).get("ok") or 0),
            "failed": sum(int(r.get("bad") or 0) for r in sent),
            "promo": {"shown": promo.get("shown", 0), "connect": promo.get("connect", 0),
                      "later": promo.get("later", 0)},
            "people": people}


def _inbox_brief() -> dict:
    """Обращения из «Обратной связи»: новые и открытые — для сторожа и вкладки."""
    try:
        from . import inbox
        return inbox.brief()
    except Exception:  # noqa: BLE001 — «Пульс» не падает из-за одного блока
        return {}


def _eval_brief() -> dict:
    """Итог двух последних прогонов регрессионного набора по каждому режиму —
    для сторожа: падение балла и давность прогона видно, не открывая вкладку."""
    rows = _rows("""
        SELECT engine, model, score::float AS score, trigger,
               to_char(COALESCE(finished_at, started_at) AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD') AS d,
               EXTRACT(day FROM now() - COALESCE(finished_at, started_at))::int AS age_days
          FROM agent_eval_run
         WHERE COALESCE(note, '') NOT LIKE 'набор v1%' AND score IS NOT NULL
         ORDER BY started_at DESC LIMIT 40""")
    out: dict = {}
    for r in rows:
        eng = r.get("engine") or "hermes"
        b = out.get(eng)
        if b is None:
            out[eng] = {"score": r["score"], "model": r["model"], "d": r["d"],
                        "age_days": r["age_days"], "trigger": r["trigger"], "prev": None}
        elif b["prev"] is None and r["model"] == b["model"]:
            b["prev"] = r["score"]
    for b in out.values():
        b["delta"] = (round(b["score"] - b["prev"], 1)
                      if b.get("prev") is not None and b.get("score") is not None else None)
    return out


def _review_sources() -> dict:
    """Полнота площадок отзывов (rag.reviews_dash.source_health)."""
    try:
        from ..rag import reviews_dash as rd
        return rd.source_health()
    except Exception as e:  # noqa: BLE001 — блок «Пульса» не валит страницу
        return {"error": str(e)[:200]}


def _signal_journal() -> dict:
    """Точность сигналов «Отзывов» по отметкам аудиторов (reviews_work)."""
    try:
        from ..rag import reviews_work
        return reviews_work.journal_stats(180)
    except Exception as e:  # noqa: BLE001 — блок «Пульса» не валит страницу
        return {"error": str(e)[:200]}


def _personalization(days: int, ex: list[str] | None = None) -> dict:
    """Персонализация по людям (этап F): сила профиля, трафик и клики «Для
    вас», оценки — всё за период. Владелец видит, у кого профиль пустой и
    работает ли обучение."""
    out: dict = {"users": [], "ctr": None}
    try:
        from . import userdata as ud
        p = {"days": days, "ex": _ex(ex)}
        P = _ppl()
        views = {r["username"]: int(r["n"]) for r in _rows(f"""
            SELECT username, count(*) AS n FROM usage_event
             WHERE kind = 'page_view' AND page = 'foryou' AND {P}
               AND created_at >= {_SINCE}
             GROUP BY 1""", p)}
        # клики «Для вас» — только со страницы «Для вас»: раньше в CTR шли и клики
        # из «Выпуска дня» (24 клика на 42 открытия = 57%; аудит 03.10, ПУЛ-07)
        ck = {r["username"]: (int(r["fy"]), int(r["ov"])) for r in _rows(f"""
            SELECT username, count(*) FILTER (WHERE page = 'foryou') AS fy,
                   count(*) FILTER (WHERE COALESCE(page, '') <> 'foryou') AS ov
              FROM usage_event
             WHERE kind = 'news_click' AND {P}
               AND created_at >= {_SINCE}
             GROUP BY 1""", p)}
        clicks = {u: v[0] for u, v in ck.items()}
        fb = {r["username"]: int(r["n"]) for r in _rows(f"""
            SELECT username, count(*) AS n FROM item_feedback
             WHERE kind IN ('news', 'for_you', 'check', 'digest_card') AND {P}
               AND created_at >= {_SINCE}
             GROUP BY 1""", p)}
        for r in _rows(f"""SELECT username, COALESCE(display_name, username) AS name FROM app_user
                           WHERE last_seen_at >= {_SINCE} AND {P}
                           ORDER BY last_seen_at DESC LIMIT 20""", p):
            u = r["username"]
            try:
                score = int((ud.personalization_score(u) or {}).get("score") or 0)
            except Exception:  # noqa: BLE001
                score = None
            out["users"].append({"username": u, "name": r["name"], "score": score,
                                 "views": views.get(u, 0),
                                 "clicks": clicks.get(u, 0),
                                 "clicks_issue": (ck.get(u) or (0, 0))[1], "fb": fb.get(u, 0)})
        tv, tc = sum(views.values()), sum(clicks.values())
        out["ctr"] = round(100.0 * tc / tv, 1) if tv else None
    except Exception:  # noqa: BLE001
        log.warning("[telemetry] personalization metrics failed", exc_info=True)
    return out


def _news_quality(days: int, ex: list[str] | None = None) -> dict:
    """Качество новостного выпуска: ночной LLM-судья (digest_news_judge) +
    клики по новостям. Появилось этапом 6 переделки новостей (05.08.2026) —
    до этого качество отбора не измерялось вообще."""
    p = {"days": days, "ex": _ex(ex)}
    P = _ppl()
    series = _rows("""
        SELECT digest_date::text AS d, n_items, junk, borderline, relevant,
               avg_score::float AS avg,
               (detail->>'headline_value')::int AS head,
               (detail->>'strong')::int AS strong,
               jsonb_array_length(coalesce(detail->'missed', '[]'::jsonb)) AS missed,
               detail->>'rubric' AS rubric
          FROM digest_news_judge
         WHERE digest_date > current_date - make_interval(days => :days)
         ORDER BY digest_date""", p)
    clicks = _rows(f"""
        SELECT (created_at AT TIME ZONE 'Europe/Moscow')::date::text AS d,
               count(*) AS n, count(DISTINCT username) AS users
          FROM usage_event
         WHERE kind = 'news_click' AND {P}
           AND created_at >= {_SINCE}
         GROUP BY 1 ORDER BY 1""", p)
    top_clicked = _rows(f"""
        SELECT payload->>'url' AS url, max(payload->>'title') AS title, count(*) AS n
          FROM usage_event
         WHERE kind = 'news_click' AND {P}
           AND created_at >= {_SINCE}
         GROUP BY 1 ORDER BY 3 DESC LIMIT 8""", p)
    # оценки карточек передовицы аудиторами: «полезно» / «не по делу»
    cards = _rows(f"""
        SELECT count(*) FILTER (WHERE verdict = 1) AS useful,
               count(*) FILTER (WHERE verdict = -1) AS noise,
               count(DISTINCT username) AS users
          FROM item_feedback
         WHERE kind = 'digest_card' AND {P} AND created_at >= {_SINCE}""", p)
    try:
        from ..digest import newsflow
        stream = newsflow.health()
        # человеческие названия лент: ключи вида tg_banksta аудитору ничего не говорят
        from .sources_catalog import news_source_label
        for r in (stream.get("sources") or []) + (stream.get("yield_14d") or []):
            r["label"] = news_source_label(r.get("source"))
    except Exception:  # noqa: BLE001 — поток ещё не запускался
        stream = None
    today = series[-1] if series else None
    return {"series": series, "today": today, "clicks": clicks,
            "top_clicked": top_clicked, "cards": (cards[0] if cards else None),
            "stream": stream}


# ── оценки ответов ИИ: «что разбирать» ───────────────────────────────────────
# Владелец видит и жалобы, и похвалы. Текст жалобы показывать этично: кнопка
# подписана «Плохой ответ — команда разберёт», пользователь отправляет это
# команде осознанно. Тексты ОБЫЧНЫХ вопросов к ИИ сюда не попадают — в аудите
# «кто что проверяет» это план проверок коллеги.

AIFB_REASON_RU = {"offtopic": "не по делу", "shallow": "мало конкретики",
                  "wrong": "ошибка в данных", "long": "слишком длинно"}


def _ai_feedback(days: int, ex: list[str] | None = None, limit: int = 20) -> dict:
    p = {"days": days, "lim": limit, "ex": _ex(ex)}
    P = _ppl("f.username")
    rows = _rows(f"""
        SELECT f.username, COALESCE(au.display_name, f.username) AS name,
               f.verdict, f.created_at,
               f.payload->>'question'   AS question,
               f.payload->>'comment'    AS comment,
               f.payload->>'mode'       AS mode,
               f.payload->'reasons'     AS reasons,
               f.payload->>'report_id'  AS report_id,
               f.payload->>'quote'      AS quote
          FROM item_feedback f
          LEFT JOIN app_user au ON au.username = f.username
         WHERE f.kind = 'ai_answer' AND {P}
           AND f.created_at >= {_SINCE}
         ORDER BY f.created_at DESC LIMIT :lim""", p)
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
            "username": r.get("username"), "name": r.get("name"),
            "verdict": int(r.get("verdict") or 0),
            "question": (r.get("question") or "")[:300],
            "comment": (r.get("comment") or "")[:300],
            "mode": r.get("mode"), "report_id": r.get("report_id"),
            "quote": (r.get("quote") or "")[:600] or None,
            "reasons": [AIFB_REASON_RU.get(x, x) for x in reasons],
            "created_at": str(r.get("created_at") or ""),
        })
    # счётчики — по всему периоду, а не по 20 последним строкам журнала
    cnt = _rows(f"""
        SELECT count(*) FILTER (WHERE verdict > 0) AS likes,
               count(*) FILTER (WHERE verdict < 0) AS dislikes
          FROM item_feedback f
         WHERE f.kind = 'ai_answer' AND {P} AND f.created_at >= {_SINCE}""", p)
    likes = int((cnt[0] if cnt else {}).get("likes") or 0)
    dislikes = int((cnt[0] if cnt else {}).get("dislikes") or 0)
    # Знаменатель покрытия: сколько ответов вообще выдали за период.
    answers = int(_scalar(f"""SELECT count(*) FROM user_event WHERE kind = 'ai_query'
                              AND {_ppl()} AND ts >= {_SINCE}""", p) or 0)
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


def _persona(ex: list[str] | None = None) -> dict:
    rows = _rows(f"""
        SELECT au.username, COALESCE(au.display_name, au.username) AS name,
               length(COALESCE(au.prefs->>'self_description','')) AS desc_len,
               (au.profile_note IS NOT NULL AND au.profile_note <> '') AS has_note,
               COALESCE(au.prefs->>'personal_digest','') <> 'false' AS personal_on,
               (SELECT count(DISTINCT x) FROM (
                    SELECT jsonb_object_keys(COALESCE(au.interests->'counters'->'products','{{}}'::jsonb)) x
                    UNION SELECT jsonb_array_elements_text(COALESCE(au.interests->'pinned','[]'::jsonb))
                    UNION SELECT jsonb_array_elements_text(COALESCE(au.interests->'custom','[]'::jsonb))
               ) f) AS focus_n,
               (SELECT count(*) FROM user_event ue
                 WHERE ue.username = au.username AND ue.kind = 'ai_query') AS q_n,
               (SELECT count(*) FROM item_feedback f2
                 WHERE f2.username = au.username
                   AND f2.kind IN ('news','for_you','check')) AS fb_n,
               (SELECT count(*) FROM item_feedback f3
                 WHERE f3.username = au.username AND f3.kind = 'ai_answer') AS ai_n,
               au.last_seen_at > now() - interval '30 days' AS recent
          FROM app_user au WHERE {_ppl('au.username')} ORDER BY 2""", {"ex": _ex(ex)})
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
                    "score": min(100, score), "recent": bool(r.get("recent")),
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
    # сначала — кто ходит в инструмент и у кого профиль пустой: им и нужна помощь
    out.sort(key=lambda u: (not u["recent"], u["score"], u["name"] or ""))
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


def _search_health(days: int) -> dict:
    """Веб-поиск и чтение копий страниц: кто отвечал и как часто сбоило.

    Сбой шлюза и честная пустая выдача выглядят для отчёта одинаково — «ничего
    не нашлось». Здесь их видно раздельно: ok / empty (поиск честно пуст) /
    limited (упёрлись в лимит, ушли на запасной) / down (шлюз недоступен,
    ключ, квота) / error. Текст запросов не пишется.
    """
    rows = _rows("""
        SELECT kind, COALESCE(page, '?') AS backend,
               COALESCE(payload->>'status', '?') AS status, count(*) AS n,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY dur_ms)) AS p50
          FROM usage_event
         WHERE kind IN ('web_search', 'web_read', 'web_search_chain')
           AND created_at > now() - (:days || ' days')::interval
         GROUP BY 1, 2, 3 ORDER BY 1, 2, 4 DESC""", {"days": days})
    out: dict[str, dict] = {}
    for r in rows:
        key = f"{r['kind']}:{r['backend']}"
        b = out.setdefault(key, {"kind": r["kind"], "backend": r["backend"],
                                 "total": 0, "by_status": {}, "p50_ok": None})
        b["total"] += int(r["n"])
        b["by_status"][r["status"]] = int(r["n"])
        if r["status"] == "ok":
            b["p50_ok"] = r["p50"]
    try:
        from ..rag import search_gateway
        gw = search_gateway.status()
    except Exception:  # noqa: BLE001 — сбой импорта не роняет «Пульс»
        gw = {}
    return {"backends": list(out.values()), "gateway": gw}


def _collect_health(days: int) -> dict:
    """Что не доехало до базы знаний. Две группы, а не одна: «не дошло»
    (капча, сеть) и «дошло, но индексировать нечего» (дубль, пустая страница).
    Смешивать нельзя — первое требует вмешательства, второе штатно."""
    rows = _rows("""
        SELECT COALESCE(skipped_reason, 'ok') AS reason, count(*) AS n
          FROM document_origin
         WHERE created_at > now() - (:days || ' days')::interval
         GROUP BY 1 ORDER BY 2 DESC""", {"days": days})
    HARD = {"captcha", "fetch_failed", "empty_after_parse", "antibot_stub"}
    RU = {"ok": "проиндексировано", "duplicate": "уже было",
          "captcha": "капча", "fetch_failed": "не загрузилось",
          "empty_after_parse": "пустая страница", "no_chunks": "нечего индексировать",
          "antibot_stub": "заглушка антибота",
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


def _team_topics(days: int, ex: list[str] | None = None) -> dict:
    """Что проверяет отдел — агрегат по команде, без имён и без текстов вопросов."""
    banks = _rows(f"""
        SELECT x.b AS slug, COALESCE(bk.name, x.b) AS name, count(*) AS n
          FROM report r, unnest(r.banks) x(b)
          LEFT JOIN bank bk ON bk.slug = x.b
         WHERE r.created_at >= {_SINCE} AND {_ppl('r.username')}
         GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8""", {"days": days, "ex": _ex(ex)})
    return {"banks": banks}


# ── Люди: директория и карточка ──────────────────────────────────────────────
# «Сегодня зашло 30 человек» — бесполезное число, если нельзя посмотреть, КТО
# именно и что делал. Таблица «Команда» показывала 15 строк и восемь колонок;
# здесь — все пользователи и полный разрез по каждому.

_DIRECTORY = f"""
    SELECT au.username,
           COALESCE(au.display_name, au.username) AS name,
           au.display_name IS NOT NULL            AS named,
           COALESCE(au.prefs->>'pulse_hidden', '') = 'true' AS hidden,
           to_char(au.created_at   AT TIME ZONE 'Europe/Moscow', 'DD.MM.YYYY') AS first_seen,
           -- «был(а)» — последнее действие: last_seen_at до 03.10 двигался только
           -- загрузкой приложения, у 80 из 110 человек отставал от хронологии
           to_char(greatest(au.last_seen_at, la.at) AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS last_seen,
           EXTRACT(epoch FROM now() - greatest(au.last_seen_at, la.at))::bigint AS last_seen_ago_s,
           au.profile_note IS NOT NULL AS has_note,
           COALESCE(e.days_active, 0) AS days_active,
           COALESCE(e.views, 0)       AS views,
           COALESCE(e.time_s, 0)      AS time_s,
           COALESCE(vs.visits, 0)     AS visits,
           COALESCE(e.errors, 0)      AS errors,
           COALESCE(q.ai, 0)          AS ai,
           COALESCE(r.deep, 0)        AS deep,
           COALESCE(r.deep, 0)        AS reports,
           COALESCE(r.quick, 0)       AS quick,
           COALESCE(sh.shares, 0)     AS shares,
           COALESCE(fb.likes, 0)      AS likes,
           COALESCE(fb.dislikes, 0)   AS dislikes,
           COALESCE(t.today_views, 0) AS today_views,
           top.pages                  AS top_pages
      FROM app_user au
      LEFT JOIN LATERAL (SELECT created_at AS at FROM usage_event
                          WHERE username = au.username AND {_HUMAN}
                          ORDER BY created_at DESC LIMIT 1) la ON TRUE
      -- «дней активности» — дни, когда человек открывал страницы: фоновые
      -- запросы забытой вкладки активностью не считаются (как и в «Аудитории»)
      LEFT JOIN (SELECT username,
                        count(DISTINCT date_trunc('day', created_at AT TIME ZONE 'Europe/Moscow'))
                              FILTER (WHERE kind = 'page_view') AS days_active,
                        count(*) FILTER (WHERE kind = 'page_view') AS views,
                        count(*) FILTER (WHERE kind IN ('api_error', 'client_error')) AS errors,
                        round(COALESCE(sum(dur_ms) FILTER (WHERE kind = 'page_leave'), 0) / 1000.0) AS time_s
                   FROM usage_event
                  WHERE created_at >= {_SINCE}
                    AND username IS NOT NULL
                  GROUP BY 1) e USING (username)
      -- «визит» = приход после паузы больше получаса. Паузу считаем ТОЛЬКО по
      -- просмотрам страниц: между ними идут фоновые api_request, и по всем
      -- событиям подряд пауза никогда не набиралась — у человека с 1198
      -- просмотрами за 19 дней выходило два визита.
      LEFT JOIN (SELECT username, count(*) AS visits
                   FROM (SELECT username,
                                created_at - lag(created_at) OVER (PARTITION BY username
                                                                   ORDER BY created_at) AS gap
                           FROM usage_event
                          WHERE kind = 'page_view' AND username IS NOT NULL
                            AND created_at >= {_SINCE}) v
                  WHERE gap IS NULL OR gap > interval '30 minutes'
                  GROUP BY 1) vs USING (username)
      LEFT JOIN (SELECT username, count(*) AS ai
                   FROM user_event
                  WHERE kind = 'ai_query' AND ts >= {_SINCE}
                  GROUP BY 1) q USING (username)
      -- отчёт (deep) и сохранённый быстрый ответ лежат в одной таблице; режим —
      -- в payload.mode. В событии запроса режима нет (его выбирает маршрутизатор),
      -- поэтому «глубоких» раньше всегда было 0
      LEFT JOIN (SELECT username,
                        count(*) FILTER (WHERE COALESCE(payload->>'mode', 'deep') <> 'quick') AS deep,
                        count(*) FILTER (WHERE payload->>'mode' = 'quick') AS quick
                   FROM report WHERE created_at >= {_SINCE} GROUP BY 1) r USING (username)
      LEFT JOIN (SELECT username, count(*) AS shares FROM user_event
                  WHERE kind = 'share' AND ts >= {_SINCE}
                  GROUP BY 1) sh USING (username)
      LEFT JOIN (SELECT username,
                        count(*) FILTER (WHERE verdict > 0) AS likes,
                        count(*) FILTER (WHERE verdict < 0) AS dislikes
                   FROM item_feedback
                  WHERE created_at >= {_SINCE}
                  GROUP BY 1) fb USING (username)
      LEFT JOIN (SELECT username, count(*) AS today_views FROM usage_event
                  WHERE kind = 'page_view' AND created_at >= {_TODAY}
                  GROUP BY 1) t USING (username)
      LEFT JOIN (SELECT username, string_agg(page, ',' ORDER BY n DESC) AS pages
                   FROM (SELECT username, page, count(*) AS n,
                                row_number() OVER (PARTITION BY username ORDER BY count(*) DESC) AS rn
                           FROM usage_event
                          WHERE kind = 'page_view' AND page IS NOT NULL
                            AND created_at >= {_SINCE}
                          GROUP BY 1, 2) x
                  WHERE rn <= 3 GROUP BY 1) top USING (username)
"""


def users_directory(days: int = 30, exclude: list[str] | None = None) -> dict:
    """ВСЕ пользователи со сводкой по каждому — основа вкладки «Люди».

    Служебные учётки и сам владелец (exclude) остаются в списке — иначе пометку
    «служебная» не снять, — но идут в конце и не входят в итоги."""
    days = max(1, min(int(days or 30), 365))
    exs = set(exclude or [])
    rows = _rows(_DIRECTORY + """
        ORDER BY (COALESCE(e.time_s, 0) / 60.0 + COALESCE(e.views, 0) * 2
                  + COALESCE(q.ai, 0) * 15 + COALESCE(r.deep, 0) * 30
                  + COALESCE(fb.likes, 0) * 5 + COALESCE(fb.dislikes, 0) * 5) DESC,
                 greatest(au.last_seen_at, la.at) DESC""", {"days": days})
    for r in rows:
        r["top_pages"] = [x for x in (r.get("top_pages") or "").split(",") if x]
        r["online"] = (r.get("last_seen_ago_s") or 10 ** 9) < 900
        r["today"] = bool(r.get("today_views"))
        r["excluded"] = r["username"] in exs
    rows.sort(key=lambda r: r["excluded"])          # стабильно: внутри групп — прежний порядок
    ppl = [r for r in rows if not r["excluded"]]
    return {"days": days, "users": rows,
            "total": len(ppl), "excluded": len(rows) - len(ppl),
            "active": sum(1 for r in ppl if r["days_active"]),
            "today": sum(1 for r in ppl if r["today"]),
            "online": sum(1 for r in ppl if r["online"]),
            "silent": sum(1 for r in ppl if not r["days_active"])}


def user_card(username: str, days: int = 30) -> dict:
    """Полный разрез одного человека: чем пользуется, что спрашивал, что оценил.

    Это внутренний инструмент со служебным доступом владельца, поэтому карточка
    показывает фактические действия, а не обезличенные счётчики: разбирать
    жалобу «отчёты плохие» иначе невозможно.
    """
    days = max(1, min(int(days or 30), 365))
    p = {"u": username, "days": days}
    head = _rows(_DIRECTORY + " WHERE au.username = :u", p)
    if not head:
        return {}
    u = head[0]
    u["top_pages"] = [x for x in (u.get("top_pages") or "").split(",") if x]
    u["online"] = (u.get("last_seen_ago_s") or 10 ** 9) < 900

    profile = _rows("""SELECT prefs->>'role_desc' AS role_desc, profile_note,
                              to_char(profile_note_at AT TIME ZONE 'Europe/Moscow',
                                      'DD.MM.YYYY') AS note_at,
                              interests, timezone
                         FROM app_user WHERE username = :u""", p)
    return {
        "user": u,
        "profile": (profile[0] if profile else {}),
        "by_day": _rows("""
            SELECT to_char(d, 'YYYY-MM-DD') AS d,
                   count(*) FILTER (WHERE kind = 'page_view') AS views,
                   round(COALESCE(sum(dur_ms) FILTER (WHERE kind = 'page_leave'), 0)
                         / 1000.0) AS time_s
              FROM (SELECT date_trunc('day', created_at AT TIME ZONE 'Europe/Moscow') AS d,
                           kind, dur_ms
                      FROM usage_event
                     WHERE username = :u
                       AND created_at > now() - (:days || ' days')::interval) z
             GROUP BY 1 ORDER BY 1""", p),
        "pages": _rows("""
            SELECT v.page, v.views, COALESCE(l.total_s, 0) AS total_s
              FROM (SELECT page, count(*) AS views FROM usage_event
                     WHERE username = :u AND kind = 'page_view' AND page IS NOT NULL
                       AND created_at > now() - (:days || ' days')::interval
                     GROUP BY 1) v
              LEFT JOIN (SELECT page, round(sum(dur_ms) / 1000.0) AS total_s
                           FROM usage_event
                          WHERE username = :u AND kind = 'page_leave'
                            AND created_at > now() - (:days || ' days')::interval
                          GROUP BY 1) l USING (page)
             ORDER BY v.views DESC LIMIT 20""", p),
        # Вопросы берём из истории чата, а не из user_event: там рядом лежит
        # ФАКТИЧЕСКИЙ режим ответа и номер отчёта, а в событии запроса режима
        # ещё нет — его выбирает маршрутизатор уже в процессе.
        "questions": _rows("""
            SELECT to_char(m.created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS at,
                   m.content AS question,
                   a.meta->>'mode'      AS mode,
                   a.meta->>'report_id' AS report_id,
                   length(COALESCE(a.content, '')) AS answer_len
              FROM chat_message m
              JOIN chat_session cs ON cs.session_id = m.session_id
              LEFT JOIN LATERAL (
                    SELECT meta, content FROM chat_message x
                     WHERE x.session_id = m.session_id AND x.role = 'assistant'
                       AND x.created_at > m.created_at
                     ORDER BY x.created_at LIMIT 1) a ON TRUE
             WHERE cs.username = :u AND m.role = 'user'
               AND m.created_at > now() - (:days || ' days')::interval
             ORDER BY m.created_at DESC LIMIT 60""", p),
        "reports": _rows("""
            SELECT report_id, question, title, COALESCE(payload->>'mode', 'deep') AS mode,
                   payload->>'status' AS status,
                   CASE WHEN payload->>'elapsed_s' ~ '^[0-9]+$'
                        THEN (payload->>'elapsed_s')::int END AS elapsed_s,
                   to_char(created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS at,
                   length(body) AS body_len
              FROM report WHERE username = :u
             ORDER BY created_at DESC LIMIT 40""", p),
        "ratings": _rows("""
            SELECT kind, verdict, item_key,
                   to_char(created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS at,
                   payload->>'question' AS question, payload->>'comment' AS comment,
                   payload->>'title' AS title, payload->>'report_id' AS report_id,
                   payload->>'session_id' AS session_id, payload->'reasons' AS reasons
              FROM item_feedback WHERE username = :u
             ORDER BY created_at DESC LIMIT 40""", p),
        "errors": _rows("""
            SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS at,
                   kind, page, status,
                   COALESCE(payload->>'msg', payload->>'error', payload->>'message') AS message
              FROM usage_event
             WHERE username = :u AND kind IN ('api_error', 'client_error')
               AND created_at > now() - (:days || ' days')::interval
             ORDER BY created_at DESC LIMIT 20""", p),
        "trail": _rows("""
            SELECT to_char(created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI:SS') AS at,
                   kind, page, dur_ms, status
              FROM usage_event
             WHERE username = :u AND kind <> 'api_request'
               AND created_at > now() - (:days || ' days')::interval
             ORDER BY created_at DESC, id DESC LIMIT 200""", p),
    }


# ── Отчёты всех пользователей (служебный доступ владельца) ───────────────────
# Жалоба «отчёты плохие» неразбираема, если нельзя открыть тот самый отчёт.
# Владелец инструмента видит все; каждое открытие чужого пишется в след.

def reports_all(days: int = 30, limit: int = 200, q: str | None = None,
                username: str | None = None, only_bad: bool = False,
                mode: str | None = None, exclude: list[str] | None = None) -> dict:
    """Отчёты и сохранённые быстрые ответы. mode: deep — только отчёты,
    quick — только быстрые ответы. exclude — служебные учётки и владелец."""
    p = {"days": max(1, min(int(days or 30), 365)), "lim": max(1, min(int(limit or 200), 500)),
         "ex": _ex(exclude)}
    cond = [f"r.created_at >= {_SINCE}", _ppl("r.username")]
    if mode == "quick":
        cond.append("r.payload->>'mode' = 'quick'")
    elif mode == "deep":
        cond.append("COALESCE(r.payload->>'mode', 'deep') <> 'quick'")
    if q:
        cond.append("(r.question ILIKE :q OR r.title ILIKE :q OR r.body ILIKE :q)")
        p["q"] = f"%{q.strip()}%"
    if username:
        cond.append("r.username = :u")
        p["u"] = username
    if only_bad:
        cond.append("fb.dislikes > 0")
    rows = _rows(f"""
        SELECT r.report_id, r.username,
               COALESCE(au.display_name, r.username) AS name,
               r.question, r.title, r.banks,
               COALESCE(r.payload->>'mode', 'deep') AS mode,
               length(r.body) AS body_len,
               r.payload->>'status' AS status,
               CASE WHEN r.payload->>'elapsed_s' ~ '^[0-9]+$'
                    THEN (r.payload->>'elapsed_s')::int END AS elapsed_s,
               to_char(r.created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS at,
               COALESCE(fb.likes, 0) AS likes, COALESCE(fb.dislikes, 0) AS dislikes,
               fb.comment, fb.reasons,
               COALESCE(sh.shares, 0) AS shares,
               COALESCE(op.opens, 0)  AS opens
          FROM report r
          LEFT JOIN app_user au ON au.username = r.username
          LEFT JOIN (SELECT (payload->>'report_id')::bigint AS rid,
                            count(*) FILTER (WHERE verdict > 0) AS likes,
                            count(*) FILTER (WHERE verdict < 0) AS dislikes,
                            max(payload->>'comment') AS comment,
                            max(payload->>'reasons') AS reasons
                       FROM item_feedback
                      WHERE payload ? 'report_id' AND payload->>'report_id' ~ '^[0-9]+$'
                      GROUP BY 1) fb ON fb.rid = r.report_id
          LEFT JOIN (SELECT report_id, count(*) AS shares FROM report_share
                      WHERE revoked_at IS NULL GROUP BY 1) sh ON sh.report_id = r.report_id
          LEFT JOIN (SELECT (payload->>'report_id')::bigint AS rid, count(*) AS opens
                       FROM user_event
                      WHERE kind = 'report_open' AND payload->>'report_id' ~ '^[0-9]+$'
                      GROUP BY 1) op ON op.rid = r.report_id
         WHERE {' AND '.join(cond)}
         ORDER BY (COALESCE(fb.dislikes, 0) > 0) DESC, r.created_at DESC
         LIMIT :lim""", p)
    return {"reports": rows, "total": len(rows),
            "deep": sum(1 for r in rows if r.get("mode") != "quick"),
            "quick": sum(1 for r in rows if r.get("mode") == "quick"),
            "bad": sum(1 for r in rows if (r.get("dislikes") or 0) > 0)}


def complaints(days: int = 30, limit: int = 60,
               exclude: list[str] | None = None) -> dict:
    """Все недовольные оценки с ФИО и ссылкой на предмет жалобы.

    total — сколько их за период всего: список ограничен limit строками."""
    p = {"days": max(1, min(int(days or 30), 365)), "lim": max(1, min(int(limit or 60), 300)),
         "ex": _ex(exclude)}
    P = _ppl("f.username")
    rows = _rows(f"""
        SELECT f.username, COALESCE(au.display_name, f.username) AS name,
               f.kind, f.item_key, f.verdict,
               to_char(f.created_at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS at,
               f.payload->>'question'  AS question,
               f.payload->>'comment'   AS comment,
               f.payload->>'title'     AS title,
               f.payload->>'mode'      AS mode,
               f.payload->>'report_id'  AS report_id,
               f.payload->>'session_id' AS session_id,
               f.payload->'reasons'     AS reasons
          FROM item_feedback f
          LEFT JOIN app_user au ON au.username = f.username
         WHERE f.verdict < 0 AND {P} AND f.created_at >= {_SINCE}
         ORDER BY f.created_at DESC LIMIT :lim""", p)
    total = int(_scalar(f"""SELECT count(*) FROM item_feedback f
                            WHERE f.verdict < 0 AND {P} AND f.created_at >= {_SINCE}""", p) or 0)
    for r in rows:
        rs = r.get("reasons")
        if isinstance(rs, str):
            try:
                rs = json.loads(rs)
            except Exception:  # noqa: BLE001
                rs = []
        r["reasons"] = [AIFB_REASON_RU.get(x, x) for x in (rs or []) if x]
    return {"items": rows, "total": total}


def session_view(session_id: int) -> dict:
    """Переписка целиком — служебный просмотр владельцем.

    Жалоба на БЫСТРЫЙ ответ отчёта не создаёт, и без диалога видно только
    вопрос: на что именно человек пожаловался — неизвестно. В оценке лежит
    session_id, по нему и открываем.
    """
    head = _rows("""SELECT cs.session_id, cs.title, cs.username,
                           COALESCE(au.display_name, cs.username) AS name,
                           to_char(cs.created_at AT TIME ZONE 'Europe/Moscow',
                                   'DD.MM.YYYY HH24:MI') AS at
                      FROM chat_session cs
                      LEFT JOIN app_user au ON au.username = cs.username
                     WHERE cs.session_id = :s""", {"s": session_id})
    if not head:
        return {}
    msgs = _rows("""SELECT role, content, meta,
                           to_char(created_at AT TIME ZONE 'Europe/Moscow',
                                   'DD.MM HH24:MI') AS at
                      FROM chat_message WHERE session_id = :s
                     ORDER BY created_at""", {"s": session_id})
    return {"session": head[0], "messages": msgs}
