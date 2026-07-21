from __future__ import annotations
import json, os, asyncio, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Query, BackgroundTasks, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sse_starlette.sse import EventSourceResponse
from .. import db
from ..config import Settings
from ..ai.analyst import stream_analysis
from ..ai.clarify import generate_clarifications, build_enriched_question
from .demo_stream import is_demo_mode_active, find_demo_response, stream_demo_response
from ..notifier.email import EmailNotifier
from ..notifier.alerts import alerts_background_loop, run_once as alerts_run_once
from ..rag import cache as rag_cache
from ..rag.indexer import ingest_document_from_url
from ..rag.url_discovery import bootstrap_bank_profile, TOP_BANK_SITES
from ..rag.crawler import crawl_one_bank, crawl_all_profiles
from .auth import CurrentUser, get_current_user
from . import userdata

STATIC_DIR = Path(__file__).parent / "static"
settings = Settings.load()
db.init(settings)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Фоновые циклы:
    #  • alerts_background_loop — раз в 30 мин quality_flag → email
    #  • digest_background_loop — выпуск «Обзора» в 07:00 МСК (+catch-up)
    #  • ingest_background_loop — автосбор тарифов в 05:00 МСК (+quality)
    # (cookie-warming убран: требовал Playwright, на сервере циклически падал)
    from ..digest.scheduler import digest_background_loop, ingest_background_loop
    tasks = [
        asyncio.create_task(alerts_background_loop()),
        asyncio.create_task(digest_background_loop()),
        asyncio.create_task(ingest_background_loop()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title="Bank Audit Platform", docs_url=None, lifespan=lifespan)
# CORS: за реверс-прокси Облака УВА фронт и API на одном origin поддомена → CORS
# обычно не нужен. Дефолт "*" сохраняет прежнее поведение (локалка); в проде задать
# CORS_ALLOW_ORIGINS=https://<app>.uva-advanced.ru (через запятую), или "" чтобы выключить.
_cors_env = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                       allow_methods=["*"], allow_headers=["*"])


# ── helpers ──────────────────────────────────────────────────────────────────

def q(sql: str, params: dict = {}):
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params).mappings().all()]

def scalar(sql: str, params: dict = {}):
    with db.session() as s:
        return s.execute(text(sql), params).scalar_one_or_none()


# ── auth / identity / user-data ───────────────────────────────────────────────

class MeUpdate(BaseModel):
    timezone: Optional[str] = None
    prefs: Optional[dict] = None

class InterestsUpdate(BaseModel):
    pinned: Optional[list] = None
    muted: Optional[list] = None
    custom: Optional[list] = None

class RenameReq(BaseModel):
    title: str

class PinReq(BaseModel):
    pinned: bool

class ShareReq(BaseModel):
    shared_with: Optional[str] = None    # None → всем пользователям инструмента

class PersonalFeedback(BaseModel):
    topics: list[str] = []               # слаги тем для «× не интересно» → заглушить
    action: str = "mute"


@app.get("/api/whoami")
def whoami(user: CurrentUser = Depends(get_current_user)):
    """Текущий пользователь из заголовков Authentik (за nginx forward-auth)."""
    return {"username": user.username, "name": user.name,
            "authenticated": user.authenticated}


@app.get("/api/me")
def get_me(tz: Optional[str] = None, user: CurrentUser = Depends(get_current_user)):
    """Профиль пользователя (+ upsert app_user, обновление last_seen/TZ)."""
    row = userdata.touch_user(user.username, user.name, timezone=tz) or {}
    return {
        "username": user.username,
        "name": row.get("display_name") or user.name,
        "timezone": row.get("timezone") or "Europe/Moscow",
        "prefs": row.get("prefs") or {},
        "interests": userdata.top_interests(user.username),
        "recommendations": userdata.recommend_topics(user.username),
        "profile_note": row.get("profile_note"),
        "profile_note_at": row.get("profile_note_at"),
        "personalization": userdata.personalization_score(user.username),
        "authenticated": user.authenticated,
    }


@app.put("/api/me")
def put_me(body: MeUpdate, user: CurrentUser = Depends(get_current_user)):
    userdata.touch_user(user.username, user.name)
    if body.timezone:
        userdata.set_timezone(user.username, body.timezone)
    if body.prefs is not None:
        userdata.update_prefs(user.username, body.prefs)
        if "self_description" in body.prefs:   # профиль изменился → «Для вас» устарел
            try:
                userdata.clear_personal_digest(user.username)
            except Exception:
                pass
    return {"ok": True}


@app.put("/api/me/interests")
def put_interests(body: InterestsUpdate, user: CurrentUser = Depends(get_current_user)):
    userdata.set_interest_overrides(user.username, pinned=body.pinned,
                                    muted=body.muted, custom=body.custom)
    try:
        userdata.clear_personal_digest(user.username)   # темы изменились → пересобрать
    except Exception:
        pass
    return {"ok": True, "interests": userdata.top_interests(user.username)}


@app.post("/api/me/profile/refresh")
async def refresh_profile_note(user: CurrentUser = Depends(get_current_user)):
    """Пересобрать LLM-нарратив профиля интересов по недавним запросам."""
    from .profile_ai import generate_profile_note
    note = await generate_profile_note(user.username)
    return {"note": note}


# ── персональный дайджест «Обзора» (Фаза 3) ───────────────────────────────────

@app.get("/api/overview/personal")
async def overview_personal(user: CurrentUser = Depends(get_current_user)):
    """Личный слой «Обзора»: lead + «Для вас» + тишина. None → персонализация выключена."""
    from ..digest import personal
    try:
        userdata.touch_user(user.username, user.name)
    except Exception:
        pass
    p = await personal.build_personal(user.username)
    return {"personal": p}


@app.post("/api/overview/personal/refresh")
async def overview_personal_refresh(user: CurrentUser = Depends(get_current_user)):
    from ..digest import personal
    p = await personal.build_personal(user.username, force=True)
    return {"personal": p}


class FeedbackIn(BaseModel):
    kind: str                       # news | for_you | check | ai_answer
    item_key: str
    verdict: int                    # +1 / -1
    topics: list[str] = []
    payload: dict = {}


@app.post("/api/feedback")
def post_feedback(body: FeedbackIn, user: CurrentUser = Depends(get_current_user)):
    """Единая точка оценок 👍/👎. Контентные (news/for_you/check) учат ЕГО
    рекомендации; ai_answer — контур качества (разбор командой)."""
    if body.kind not in ("news", "for_you", "check", "ai_answer") \
            or body.verdict not in (1, -1) or not body.item_key:
        raise HTTPException(400, "bad feedback")
    res = userdata.save_feedback(user.username, body.kind, body.item_key[:500],
                                 body.verdict, topics=body.topics[:10],
                                 payload=body.payload)
    # 👍 на ответ ИИ дополнительно усиливает темы вопроса в профиле интересов
    if body.kind == "ai_answer" and res.get("verdict") == 1:
        q = str((body.payload or {}).get("question") or "")
        if q:
            try:
                userdata.update_interests_from_query(user.username, q)
            except Exception:
                pass
    return {"ok": True, **res}


@app.get("/api/feedback")
def get_feedback(kind: str, user: CurrentUser = Depends(get_current_user)):
    """Карта оценок пользователя по kind — для рендера уже проставленных."""
    if kind not in ("news", "for_you", "check", "ai_answer"):
        raise HTTPException(400, "bad kind")
    return {"items": userdata.feedback_map(user.username, kind)}


@app.get("/api/quality/ai-feedback")
def quality_ai_feedback(user: CurrentUser = Depends(get_current_user)):
    """Пульс оценок ИИ-ответов для «Качества» (контур владельца)."""
    return userdata.ai_feedback_stats()


@app.get("/api/overview/foryou")
async def overview_foryou(user: CurrentUser = Depends(get_current_user)):
    """Персональный разворот «Для вас»: полноценная страница под профиль аудитора.
    None → персонализация выключена. Никогда не 500-ит (best-effort по дизайну)."""
    from ..digest import personal
    try:
        userdata.touch_user(user.username, user.name)
    except Exception:
        pass
    p = await personal.build_foryou(user.username)
    return {"foryou": p}


@app.post("/api/overview/foryou/refresh")
async def overview_foryou_refresh(user: CurrentUser = Depends(get_current_user)):
    from ..digest import personal
    p = await personal.build_foryou(user.username, force=True)
    return {"foryou": p}


@app.post("/api/overview/personal/feedback")
def overview_personal_feedback(body: PersonalFeedback,
                               user: CurrentUser = Depends(get_current_user)):
    """«× не интересно» на карточке → заглушить темы (учится под пользователя)."""
    if body.topics and body.action == "mute":
        cur = userdata.top_interests(user.username)
        muted = set(cur.get("muted") or []) | {t for t in body.topics if t}
        userdata.set_interest_overrides(user.username, muted=list(muted))
        userdata.log_event(user.username, "personal_feedback",
                           {"muted": body.topics})
    return {"ok": True, "interests": userdata.top_interests(user.username)}


@app.get("/api/users")
def get_users(user: CurrentUser = Depends(get_current_user)):
    """Директория пользователей инструмента (для шеринга)."""
    return {"users": userdata.list_users(exclude=user.username)}


# ── история чатов ─────────────────────────────────────────────────────────────

@app.get("/api/chat/sessions")
def get_sessions(user: CurrentUser = Depends(get_current_user)):
    return {"sessions": userdata.list_sessions(user.username)}


@app.get("/api/chat/sessions/{sid}")
def get_session_ep(sid: int, user: CurrentUser = Depends(get_current_user)):
    msgs = userdata.get_session_messages(sid, user.username)
    if msgs is None:
        raise HTTPException(404, "session not found")
    return {"session_id": sid, "messages": msgs}


@app.post("/api/chat/sessions/{sid}/rename")
def rename_session_ep(sid: int, body: RenameReq,
                      user: CurrentUser = Depends(get_current_user)):
    return {"ok": userdata.rename_session(sid, user.username, body.title)}


@app.post("/api/chat/sessions/{sid}/pin")
def pin_session_ep(sid: int, body: PinReq,
                   user: CurrentUser = Depends(get_current_user)):
    return {"ok": userdata.pin_session(sid, user.username, body.pinned)}


@app.delete("/api/chat/sessions/{sid}")
def delete_session_ep(sid: int, user: CurrentUser = Depends(get_current_user)):
    return {"ok": userdata.delete_session(sid, user.username)}


# ── отчёты + шеринг ───────────────────────────────────────────────────────────

@app.get("/api/reports")
def get_reports(user: CurrentUser = Depends(get_current_user)):
    return {"reports": userdata.list_reports(user.username),
            "shared": userdata.list_shared_with_me(user.username)}


@app.get("/api/reports/{rid}")
def get_report_ep(rid: int, user: CurrentUser = Depends(get_current_user)):
    r = userdata.get_report(rid, user.username)
    if r is None:
        raise HTTPException(404, "report not found")
    return r


@app.delete("/api/reports/{rid}")
def delete_report_ep(rid: int, user: CurrentUser = Depends(get_current_user)):
    return {"ok": userdata.delete_report(rid, user.username)}


@app.post("/api/reports/{rid}/share")
def share_report_ep(rid: int, body: ShareReq,
                    user: CurrentUser = Depends(get_current_user)):
    sid = userdata.share_report(rid, user.username, body.shared_with)
    if sid is None:
        raise HTTPException(403, "not owner")
    userdata.log_event(user.username, "share",
                       {"report_id": rid, "with": body.shared_with})
    return {"ok": True, "share_id": sid}


@app.get("/api/reports/{rid}/shares")
def report_shares_ep(rid: int, user: CurrentUser = Depends(get_current_user)):
    return {"shares": userdata.list_report_shares(rid, user.username)}


@app.post("/api/shares/{share_id}/revoke")
def revoke_share_ep(share_id: int, user: CurrentUser = Depends(get_current_user)):
    return {"ok": userdata.revoke_share(share_id, user.username)}


# ── dashboard ─────────────────────────────────────────────────────────────────

@app.get("/api/summary")
def summary():
    return {
        "banks":     scalar("SELECT count(*) FROM bank"),
        "offers":    scalar("SELECT count(*) FROM product_offer WHERE is_active"),
        "reviews":   scalar("SELECT count(*) FROM review"),
        "changes":   scalar("SELECT count(*) FROM change_history WHERE changed_at > now()-interval '7d'"),
        "flags_err": scalar("SELECT count(*) FROM quality_flag WHERE severity='error' AND created_at > now()-interval '1d'"),
        "flags_warn":scalar("SELECT count(*) FROM quality_flag WHERE severity='warn'  AND created_at > now()-interval '1d'"),
        "last_run":  scalar("SELECT max(finished_at) FROM extraction_run WHERE status='ok'"),
        "categories": q("SELECT category, count(*) n FROM v_offer_current GROUP BY category ORDER BY n DESC"),
    }

# ── дневной дайджест «Обзора» (утренний брифинг) ─────────────────────────────

def _digest_today():
    from ..digest.scheduler import _today_msk
    return _today_msk()


@app.get("/api/overview/digest")
async def overview_digest(date: Optional[str] = None):
    """Выпуск дня (или последний доступный ≤ сегодня). Без date при отсутствии
    сегодняшнего выпуска lazy-запускает генерацию в фоне и СРАЗУ отдаёт вчерашний
    с meta.refreshing=true — никогда не пустой экран и не 500."""
    from ..digest import store as digest_store
    from ..digest.scheduler import ensure_digest
    today = _digest_today()
    want = None
    if date:
        from datetime import date as _date
        try:
            want = _date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"плохая дата: {date}")
    doc = await asyncio.to_thread(digest_store.read_latest, today, want)
    if date and doc["meta"]["empty"]:
        raise HTTPException(404, f"дайджест за {date} не найден")
    if not date and not doc["meta"]["refreshing"]:
        # lazy catch-up и при ПОЛНОМ отсутствии выпуска, и при упавшем на середине
        # прогоне (часть секций есть, но день не полон) — иначе висит до утра.
        # Ночью (до GEN_HOUR) не генерим и refreshing не включаем — иначе фронт
        # поллил бы всю ночь, а выпуск дня рождался бы в 00:xx до автосбора.
        from ..digest.pipeline import REQUIRED
        from ..digest.scheduler import lazy_allowed
        complete = await asyncio.to_thread(digest_store.day_complete, today, REQUIRED)
        if not complete and lazy_allowed():
            asyncio.create_task(ensure_digest("lazy"))     # не ждём
            doc["meta"]["refreshing"] = True
    return doc


@app.get("/api/overview/digest/dates")
def overview_digest_dates():
    from ..digest import store as digest_store
    return {"dates": digest_store.list_dates()}


class DigestRefreshRequest(BaseModel):
    force: bool = True
    sections: Optional[list[str]] = None


@app.post("/api/overview/digest/refresh")
async def overview_digest_refresh(req: DigestRefreshRequest):
    """Ручной перезапуск (целиком или точечно: {"sections":["news","headline"]})."""
    from ..digest import store as digest_store
    from ..digest.scheduler import ensure_digest
    if await asyncio.to_thread(digest_store.run_in_progress, _digest_today()):
        raise HTTPException(409, "Дайджест уже генерируется")
    asyncio.create_task(ensure_digest("manual", force=req.force,
                                      sections=req.sections))
    return Response(status_code=202,
                    content=json.dumps({"started": True}),
                    media_type="application/json")


@app.get("/api/recent-changes")
def recent_changes():
    return q("""
        SELECT b.name bank_name, b.is_sber, o.category, o.title,
               ch.changed_at, ch.diff
          FROM change_history ch
          JOIN product_offer o USING(offer_id)
          JOIN bank b USING(bank_id)
         ORDER BY ch.changed_at DESC LIMIT 20
    """)


# ── market ────────────────────────────────────────────────────────────────────

@app.get("/api/market")
def market(category: str = "deposit", limit: int = 100):
    return q("""
        SELECT bank_slug, bank_name, is_sber, offer_id, title, url,
               rate_pct, rate_kind, currency,
               amount_min, amount_max, term_months_min, term_months_max,
               fee_open, fee_service, early_withdraw, capitalization,
               replenishable, conditions, valid_from
          FROM v_offer_current
         WHERE category = :c
         ORDER BY rate_pct DESC NULLS LAST
         LIMIT :l
    """, {"c": category, "l": limit})

@app.get("/api/market/categories")
def market_categories():
    return q("""
        SELECT category, count(*) total,
               count(*) FILTER (WHERE is_sber) sber_count,
               round(avg(rate_pct),2) avg_rate,
               round(max(rate_pct),2) max_rate
          FROM v_offer_current
         GROUP BY category ORDER BY total DESC
    """)


# ── sber vs market ────────────────────────────────────────────────────────────

@app.get("/api/sber-vs-market")
def sber_vs_market():
    return q("SELECT * FROM v_sber_vs_market ORDER BY category")

@app.get("/api/sber-vs-market/top")
def sber_vs_market_top():
    return q("""
        SELECT bank_name, bank_slug, is_sber, category, title,
               rate_pct, term_months_min, amount_min, rk
          FROM v_offer_top_by_rate WHERE rk <= 5
         ORDER BY category, rk
    """)


# ── reviews ───────────────────────────────────────────────────────────────────

@app.get("/api/reviews/topics")
def reviews_topics(bank_slug: Optional[str] = None):
    if bank_slug:
        return q("""
            SELECT rt.topic, count(*) n, round(avg(r.rating),2) avg_rating
              FROM review r JOIN bank b USING(bank_id)
              JOIN review_topic rt USING(review_id)
             WHERE b.slug = :s
             GROUP BY rt.topic ORDER BY n DESC
        """, {"s": bank_slug})
    return q("SELECT bank_slug, bank_name, topic, n, avg_rating FROM v_review_topics ORDER BY n DESC")


# ── reviews dashboard (риск-радар поверх корпуса banki.ru ~390к) ────────────
def _rd():
    from ..rag import reviews_dash
    return reviews_dash

@app.get("/api/reviews/banks")
def reviews_banks():
    return {"items": _rd().banks()}

@app.get("/api/reviews/overview")
def reviews_overview(bank: str = "Сбербанк", product: Optional[str] = None, days: int = 90):
    return _rd().overview(bank, product or None, days) or {}

@app.get("/api/reviews/trend")
def reviews_trend(bank: str = "Сбербанк", product: Optional[str] = None):
    return _rd().trend(bank, product or None) or {}

@app.get("/api/reviews/themes")
def reviews_themes(bank: str = "Сбербанк", product: Optional[str] = None):
    return _rd().themes(bank, product or None) or {}

@app.get("/api/reviews/vs-market")
def reviews_vs_market(bank: str = "Сбербанк", product: Optional[str] = None, days: int = 90):
    return _rd().vs_market(bank, product or None, days) or {}

@app.get("/api/reviews/geo")
def reviews_geo(bank: str = "Сбербанк", product: Optional[str] = None):
    return _rd().geo(bank, product or None) or {}

@app.get("/api/reviews/products")
def reviews_products(bank: str = "Сбербанк"):
    return _rd().products(bank) or {}

@app.get("/api/reviews/theme-defs")
def reviews_theme_defs():
    from ..rag.reviews_dash import THEMES
    return [{"key": t["key"], "label": t["label"], "risk": t["risk"]} for t in THEMES]

@app.get("/api/reviews/feed")
def reviews_feed(bank: str = "Сбербанк", product: Optional[str] = None,
                 theme: Optional[str] = None, q: Optional[str] = None,
                 city: Optional[str] = None, month: Optional[str] = None, limit: int = 20):
    items = _rd().list_reviews(bank, product or None, theme or None, q or None,
                               city=city or None, month=month or None, limit=limit)
    return {"items": items, "count": len(items)}

@app.get("/api/reviews/feed-classified")
async def reviews_feed_classified(bank: str = "Сбербанк", product: Optional[str] = None,
                                  theme: Optional[str] = None, q: Optional[str] = None,
                                  city: Optional[str] = None, month: Optional[str] = None,
                                  limit: int = 20):
    """Лента + LLM-уточнение тем показанных отзывов (on-demand, по кнопке).
    Regex-темы остаются fallback'ом, если LLM не разобрал строку."""
    import asyncio
    from ..rag import reviews_llm
    items = await asyncio.to_thread(_rd().list_reviews, bank, product or None, theme or None,
                                    q or None, None, city or None, month or None, limit)
    if not items:
        return {"items": [], "count": 0, "llm": False}
    cls = await reviews_llm.classify_reviews(items)
    llm_ok = False
    for it, c in zip(items, cls):
        if c and c.get("themes"):
            it["themes"] = c["themes"]
            it["theme_src"] = "llm"
            llm_ok = True
    return {"items": items, "count": len(items), "llm": llm_ok}

@app.get("/api/reviews/anomalies")
async def reviews_anomalies(bank: str = "Сбербанк", product: Optional[str] = None):
    """Срочные аномалии за 7 дней (audit-радар): детерминированные недельные
    всплески тем/модулей + краткое LLM-объяснение. Грузится отдельно от дашборда."""
    import asyncio
    from ..rag import reviews_llm
    sig = await asyncio.to_thread(_rd().weekly_signals, bank, product or None)
    signals = (sig or {}).get("signals") or []
    if not signals:
        return {"summary": None, "signals": [], "overall": (sig or {}).get("overall"), "calm": True}
    recent = await asyncio.to_thread(_rd().list_reviews, bank, product or None, None, None, 7, None, None, 50)
    unclassified = [r for r in recent if not r.get("themes")]   # кандидаты в новые инциденты
    brief = await reviews_llm.anomaly_brief(sig, recent[:14], unclassified[:14])
    return {"summary": brief, "signals": signals, "overall": sig.get("overall"), "calm": False}

@app.get("/api/reviews/explain")
async def reviews_explain(bank: str = "Сбербанк", product: Optional[str] = None,
                          city: Optional[str] = None, month: Optional[str] = None):
    """On-demand LLM-объяснение причины гео-аномалии или пика динамики (по кнопке)."""
    import asyncio
    from ..rag import reviews_llm
    seg = await asyncio.to_thread(_rd().segment_reviews, bank, product or None,
                                  city or None, month or None)
    if not seg or not seg.get("n"):
        return {"summary": None, "themes": [], "samples": [], "n": 0}
    parts = []
    if city:
        parts.append(f"г. {city}")
    if month:
        parts.append(f"месяц {month}")
    label = f"{bank}" + (" · " + ", ".join(parts) if parts else "")
    summary = await reviews_llm.explain_segment(seg, label=label)
    return {"summary": summary, "themes": seg["themes"], "samples": seg["samples"], "n": seg["n"]}


# ── banks & ratings ───────────────────────────────────────────────────────────

@app.get("/api/banks")
def banks():
    return q("""
        SELECT b.bank_id, b.slug, b.name, b.is_sber,
               t.rate_pct avg_grade,
               (t.raw->>'total_reviews')::int total_reviews,
               round((t.raw->>'solved_pct')::numeric,1) solved_pct,
               (t.raw->>'place')::int place
          FROM bank b
          LEFT JOIN product_offer o ON o.bank_id=b.bank_id AND o.category='other'
          LEFT JOIN product_terms t  ON t.offer_id=o.offer_id AND t.valid_to IS NULL
                                    AND t.rate_kind='avg_grade'
         ORDER BY COALESCE((t.raw->>'total_reviews')::int, 0) DESC
    """)


# ── quality ───────────────────────────────────────────────────────────────────

@app.get("/api/quality")
def quality():
    summary_rows = q("""
        SELECT code, severity, count(*) n
          FROM quality_flag
         WHERE created_at > now()-interval '2d'
         GROUP BY code, severity ORDER BY n DESC
    """)
    flags = q("""
        SELECT qf.flag_id, qf.entity_type, qf.entity_id,
               qf.severity, qf.code,
               qf.detail::text AS detail,
               qf.created_at
          FROM quality_flag qf
         WHERE qf.created_at > now()-interval '2d'
         ORDER BY qf.severity DESC, qf.created_at DESC LIMIT 100
    """)
    # detail приходит из PG как строка-JSON; парсим в dict для удобства фронта
    import json as _json
    for f in flags:
        if isinstance(f.get("detail"), str):
            try:
                f["detail"] = _json.loads(f["detail"])
            except Exception:
                pass
    return {"summary": summary_rows, "flags": flags}


# ── sources / jobs ────────────────────────────────────────────────────────────

@app.get("/api/sources")
def sources_status():
    from ..config import load_sources
    runs = q("""
        SELECT source, target_name, started_at, finished_at, status,
               items_seen, items_written, error, openclaw_job
          FROM extraction_run
         ORDER BY started_at DESC LIMIT 50
    """)
    # Список настроенных источников из sources.yaml — нужен на фронте даже
    # когда история запусков пуста (первый запуск с пустой БД).
    cfg = load_sources()
    configured = [
        {
            "name": k,
            "collector": v.get("collector", "http"),
            "targets": [t.get("name") for t in (v.get("targets") or [])],
        }
        for k, v in cfg.items()
    ]
    captcha = _load_captcha_pending()
    return {"runs": runs, "captcha_pending": captcha, "configured": configured}

def _load_captcha_pending() -> list:
    path = settings.workspace_dir / "captcha_pending.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []


class IngestRequest(BaseModel):
    source: str
    target: Optional[str] = None

@app.post("/api/ingest/run")
def ingest_run(req: IngestRequest, background_tasks: BackgroundTasks):
    if _CAPTCHA_LOCK:
        raise HTTPException(409, "Сейчас решается капча — дождитесь её завершения")
    background_tasks.add_task(_do_ingest, req.source, req.target)
    return {"status": "started", "source": req.source}

def _do_ingest(source: str, target: Optional[str]):
    from ..digest.scheduler import INGEST_MUTEX
    from ..orchestrator.runner import ingest
    if not INGEST_MUTEX.acquire(blocking=False):
        log.info("ingest %s: пропуск — сбор уже идёт (автосбор/другой запуск)", source)
        return
    try:
        ingest(source, target)
    except Exception:
        pass  # статус пишется в extraction_run
    finally:
        INGEST_MUTEX.release()


@app.post("/api/ingest/run-all")
def ingest_run_all(background_tasks: BackgroundTasks):
    """Запускает все настроенные источники последовательно в фоне.
    Используется кнопкой «Запустить весь сбор» на пустой БД.
    """
    if _CAPTCHA_LOCK:
        raise HTTPException(409, "Сейчас решается капча — дождитесь её завершения")
    from ..config import load_sources
    sources = list(load_sources().keys())
    background_tasks.add_task(_do_ingest_all, sources)
    return {"status": "started", "sources": sources}


def _do_ingest_all(sources: list[str]):
    from ..digest.scheduler import INGEST_MUTEX
    from ..orchestrator.runner import ingest
    if not INGEST_MUTEX.acquire(blocking=False):
        log.info("ingest_all: пропуск — сбор уже идёт (автосбор/другой запуск)")
        return
    try:
        for src in sources:
            try:
                ingest(src, None)
            except Exception:
                pass  # каждый источник пишет свой статус в extraction_run
    finally:
        INGEST_MUTEX.release()

@app.delete("/api/captcha/{idx}")
def dismiss_captcha(idx: int):
    path = settings.workspace_dir / "captcha_pending.json"
    items = _load_captcha_pending()
    if 0 <= idx < len(items):
        items.pop(idx)
        path.write_text(json.dumps(items, ensure_ascii=False))
    return {"ok": True}


_CAPTCHA_LOCK = False  # in-process flag — нельзя запустить ingest пока решается капча

@app.post("/api/captcha/solve/{idx}")
async def solve_captcha(idx: int, background_tasks: BackgroundTasks):
    """Открывает URL капчи в headed-браузере с тем же профилем.
    После решения автоматически перезапускает упавший target в фоне:
      • cookies уже сохранены в OPENCLAW-профиль
      • профиль освобождается перед повторным запуском (lock-flag сбрасывается)
    Endpoint блокируется до решения (макс. 3 минуты).
    """
    import asyncio, concurrent.futures, time as _t
    from ..collectors.browser import BrowserCollector
    global _CAPTCHA_LOCK

    items = _load_captcha_pending()
    if not (0 <= idx < len(items)):
        raise HTTPException(404, "Captcha entry not found")

    item = items[idx]
    url     = item.get("url")
    src     = item.get("source")
    tgt     = item.get("target")

    if _CAPTCHA_LOCK:
        raise HTTPException(409, "Уже решается другая капча — дождитесь")
    _CAPTCHA_LOCK = True
    try:
        browser = BrowserCollector(
            headless=False,
            profile_dir=settings.browser_profile,
            nav_timeout_s=180,
        )
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            solved = await loop.run_in_executor(pool, browser.open_for_captcha, url)
    finally:
        # Дать ОС время освободить файловые блокировки persistent-профиля
        _t.sleep(1.0)
        _CAPTCHA_LOCK = False

    resumed = False
    if solved:
        # Убираем из pending
        path = settings.workspace_dir / "captcha_pending.json"
        items_now = _load_captcha_pending()
        items_now = [i for i in items_now if i.get("url") != url]
        path.write_text(json.dumps(items_now, ensure_ascii=False))

        # Авто-возобновление упавшего target. Если target неизвестен —
        # перезапускаем весь источник (другие таргеты идемпотентны).
        if src:
            background_tasks.add_task(_do_ingest, src, tgt)
            resumed = True

    return {"solved": solved, "url": url, "resumed": resumed,
            "source": src, "target": tgt}


# ── Email alerts ──────────────────────────────────────────────────────────────

@app.get("/api/alerts/status")
def alerts_status():
    n = EmailNotifier()
    return {
        "configured": n.is_configured(),
        "smtp_host": n.smtp_host, "smtp_port": n.smtp_port,
        "from": n.from_email, "to": n.default_to, "cc": n.default_cc,
    }

@app.post("/api/alerts/test-login")
def alerts_test_login():
    """Проверка SMTP-логина без отправки писем."""
    n = EmailNotifier()
    if not (n.smtp_user and n.smtp_pwd):
        raise HTTPException(400, "SMTP_USER/SMTP_PWD не заданы")
    ok, err = n.test_login()
    return {"ok": ok, "error": err}

@app.post("/api/alerts/send-test")
def alerts_send_test():
    """Отправить тестовое письмо на ALERTS_TO."""
    n = EmailNotifier()
    if not n.is_configured():
        raise HTTPException(400, "SMTP не сконфигурирован — заполните .env")
    ok = n.send(
        subject="[bank_audit] тестовое уведомление",
        body="Это тестовое письмо от bank_audit_platform. SMTP настроен корректно.",
    )
    return {"ok": ok}

@app.post("/api/alerts/run-now")
def alerts_run_now():
    """Принудительный прогон проверки flag'ов и отправки письма."""
    n = EmailNotifier()
    return alerts_run_once(settings, n)


# ── RAG / knowledge layer ────────────────────────────────────────────────────

@app.post("/api/rag/rebuild-summaries")
def rag_rebuild_summaries(period: str = "all", background_tasks: BackgroundTasks = None):
    """Перестроить review_summary для всех банков.
    Запускается в фоне — на 100+ банков может занять 1-2 мин."""
    from ..rag.summarizer import rebuild_all
    if period not in ("all", "last_30d", "last_90d"):
        raise HTTPException(400, "period must be one of all|last_30d|last_90d")

    def _do():
        try:
            rebuild_all(period)
        except Exception as e:
            log.warning("rebuild_summaries failed: %s", e)

    if background_tasks:
        background_tasks.add_task(_do)
    else:
        _do()
    return {"started": True, "period": period}


@app.get("/api/rag/coverage")
def rag_coverage():
    """Сводка по knowledge layer: сколько документов/chunks/features per bank."""
    return q("""
        SELECT slug, name, documents, chunks, features,
               last_doc_fetch, last_feature_extract
          FROM v_bank_knowledge_coverage
         WHERE documents > 0 OR features > 0
         ORDER BY documents DESC NULLS LAST
         LIMIT 50
    """)


class IngestUrlRequest(BaseModel):
    url: str
    bank_slug: Optional[str] = None
    use_browser: bool = False


@app.post("/api/rag/ingest-url")
def rag_ingest_url(req: IngestUrlRequest):
    """Ручной ingest конкретного URL (для проверки парсера/индексера).
    Можно использовать для bootstrap'а: подсунуть PDF тарифа, получить chunks."""
    result = ingest_document_from_url(
        req.url, bank_slug_hint=req.bank_slug, prefer_browser=req.use_browser
    )
    return {
        "document_id":    result.document_id,
        "url":            result.url,
        "doc_type":       result.doc_type,
        "trust_score":    result.trust_score,
        "is_sponsored":   result.is_sponsored,
        "is_new":         result.is_new,
        "chunks_added":   result.chunks_added,
        "skipped_reason": result.skipped_reason,
    }


@app.post("/api/rag/bootstrap-bank/{bank_slug}")
def rag_bootstrap_bank(bank_slug: str, background_tasks: BackgroundTasks = None):
    """Discover sitemap + key_pages + сохранить bank_profile.
    Запускает фоном если background_tasks доступен."""
    if bank_slug not in TOP_BANK_SITES:
        raise HTTPException(404, f"bank_slug {bank_slug} not in TOP_BANK_SITES")

    def _do():
        try:
            profile = bootstrap_bank_profile(bank_slug)
            if "error" in profile:
                log.warning("bootstrap %s: %s", bank_slug, profile["error"])
                return
            with db.session() as s:
                row = s.execute(text("SELECT bank_id FROM bank WHERE slug=:s"),
                                {"s": bank_slug}).first()
                if not row:
                    log.warning("bootstrap %s: bank not in DB", bank_slug)
                    return
                bank_id = row[0]
                s.execute(text("""
                    INSERT INTO bank_profile(bank_id, official_url, sitemap_url,
                                              robots_url, key_pages,
                                              last_crawled_at, crawl_status)
                    VALUES (:b, :ou, :su, :ru, CAST(:kp AS jsonb), now(),
                            CASE WHEN :n_topics > 0 THEN 'partial' ELSE 'pending' END)
                    ON CONFLICT (bank_id) DO UPDATE
                      SET official_url = EXCLUDED.official_url,
                          sitemap_url  = EXCLUDED.sitemap_url,
                          robots_url   = EXCLUDED.robots_url,
                          key_pages    = EXCLUDED.key_pages,
                          last_crawled_at = now(),
                          crawl_status = EXCLUDED.crawl_status
                """), {
                    "b": bank_id,
                    "ou": profile.get("official_url"),
                    "su": profile.get("sitemap_url"),
                    "ru": profile.get("robots_url"),
                    "kp": json.dumps(profile.get("key_pages") or {}, ensure_ascii=False),
                    "n_topics": profile.get("n_topics", 0),
                })
            log.info("bootstrap %s: %s topics found", bank_slug, profile.get("n_topics"))
        except Exception as e:
            log.warning("bootstrap %s failed: %s", bank_slug, e)

    if background_tasks:
        background_tasks.add_task(_do)
        return {"started": True, "bank_slug": bank_slug}
    _do()
    return {"completed": True, "bank_slug": bank_slug}


@app.post("/api/rag/bootstrap-all")
def rag_bootstrap_all(background_tasks: BackgroundTasks):
    """Bootstrap для всех TOP_BANK_SITES (последовательно, в фоне)."""
    def _do():
        for slug in TOP_BANK_SITES:
            try:
                with db.session() as s:
                    row = s.execute(text("SELECT bank_id FROM bank WHERE slug=:s"),
                                    {"s": slug}).first()
                if not row:
                    continue
                profile = bootstrap_bank_profile(slug)
                if "error" not in profile:
                    bank_id = row[0]
                    with db.session() as s:
                        s.execute(text("""
                            INSERT INTO bank_profile(bank_id, official_url, sitemap_url,
                                                      robots_url, key_pages,
                                                      last_crawled_at, crawl_status)
                            VALUES (:b, :ou, :su, :ru, CAST(:kp AS jsonb), now(),
                                    CASE WHEN :n > 0 THEN 'partial' ELSE 'pending' END)
                            ON CONFLICT (bank_id) DO UPDATE
                              SET official_url = EXCLUDED.official_url,
                                  sitemap_url  = EXCLUDED.sitemap_url,
                                  robots_url   = EXCLUDED.robots_url,
                                  key_pages    = EXCLUDED.key_pages,
                                  last_crawled_at = now(),
                                  crawl_status = EXCLUDED.crawl_status
                        """), {"b": bank_id,
                               "ou": profile.get("official_url"),
                               "su": profile.get("sitemap_url"),
                               "ru": profile.get("robots_url"),
                               "kp": json.dumps(profile.get("key_pages") or {}, ensure_ascii=False),
                               "n": profile.get("n_topics", 0)})
                log.info("bootstrap-all %s: ok (%s topics)", slug, profile.get("n_topics"))
            except Exception as e:
                log.warning("bootstrap-all %s failed: %s", slug, e)
    background_tasks.add_task(_do)
    return {"started": True, "count": len(TOP_BANK_SITES)}


class SemanticSearchRequest(BaseModel):
    query: str
    top_k: int = 8
    bank_slugs: Optional[list[str]] = None
    doc_types: Optional[list[str]] = None
    trust_min: float = 0.5


@app.post("/api/rag/semantic-search")
def rag_semantic_search(req: SemanticSearchRequest):
    """Прямой semantic-search без LLM. Возвращает топ-N фрагментов с метаданными.
    Используется в Knowledge UI для быстрого превью."""
    from ..rag.retriever import semantic_search
    if not req.query or not req.query.strip():
        raise HTTPException(400, "query пустой")
    try:
        results = semantic_search(
            req.query, top_k=req.top_k,
            bank_slugs=req.bank_slugs, doc_types=req.doc_types,
            trust_min=req.trust_min, exclude_sponsored=True,
        )
    except Exception as e:
        raise HTTPException(500, f"semantic_search failed: {e}")
    return {
        "query":   req.query,
        "results": [
            {
                "text":          r["text"][:500],
                "headings_path": r.get("headings_path"),
                "bank_slug":     r.get("bank_slug"),
                "bank_name":     r.get("bank_name"),
                "url":           r.get("url"),
                "doc_type":      r.get("doc_type"),
                "trust_score":   float(r.get("trust_score") or 0),
                "source_kind":   r.get("source_kind"),
                "fetched_at":    r["fetched_at"].isoformat() if r.get("fetched_at") else None,
                "relevance":     round(float(r.get("relevance", 0)), 3),
            } for r in results
        ],
        "count": len(results),
    }


@app.post("/api/rag/crawl-bank/{bank_slug}")
def rag_crawl_bank(bank_slug: str, background_tasks: BackgroundTasks):
    """Crawl key_pages одного банка (ingest + chunk + embed). Запускается в фоне."""
    def _do():
        try:
            r = crawl_one_bank(bank_slug)
            log.info("crawl-bank %s done: %s", bank_slug, r.get("chunks_added"))
        except Exception as e:
            log.warning("crawl-bank %s failed: %s", bank_slug, e)
    background_tasks.add_task(_do)
    return {"started": True, "bank_slug": bank_slug}


@app.post("/api/rag/crawl-all")
def rag_crawl_all(background_tasks: BackgroundTasks):
    """Crawl всех банков с заполненным bank_profile. Долгая операция (10-30 мин)."""
    def _do():
        try:
            r = crawl_all_profiles()
            log.info("crawl-all done: %s banks, %s total chunks",
                     r.get("banks"), r.get("total_chunks_added"))
        except Exception as e:
            log.warning("crawl-all failed: %s", e)
    background_tasks.add_task(_do)
    return {"started": True}


@app.get("/api/rag/review-summary/{bank_slug}")
def rag_review_summary(bank_slug: str, period: str = "all"):
    """Возвращает агрегированный review_summary для банка."""
    rows = q("""
        SELECT b.slug, b.name, rs.period, rs.total_reviews, rs.avg_rating,
               rs.sentiment_pos, rs.sentiment_neg, rs.sentiment_neu,
               rs.top_complaints, rs.top_praise, rs.by_source, rs.generated_at
          FROM review_summary rs
          JOIN bank b USING(bank_id)
         WHERE b.slug = :s AND rs.period = :p
    """, {"s": bank_slug, "p": period})
    if not rows:
        raise HTTPException(404, f"summary not built for {bank_slug}/{period}")
    return rows[0]


# ── AI chat ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    history: list = []
    force_deep: Optional[bool] = None    # None=auto, True=force deep mode, False=force quick
    session_id: Optional[int] = None     # продолжение существующей сессии истории


async def _persisting_stream(inner, username: str, session_id: int, question: str):
    """Оборачивает stream_analysis: прозрачно проксирует SSE-события, попутно
    копит финальный ответ+источники и по завершении сохраняет сообщение ассистента
    и (для содержательных ответов) отчёт. Копим так же, как фронт: text-чанки +
    report_replace (перекрывает) + sources.
    """
    # Сразу отдаём фронту session_id, чтобы следующий вопрос продолжил эту сессию.
    yield json.dumps({"type": "session", "session_id": session_id}, ensure_ascii=False)
    parts: list[str] = []
    replaced: Optional[str] = None
    sources: list = []
    mode: Optional[str] = None
    persisted = False

    def _persist() -> int | None:
        """Сохранить ответ (и отчёт, если тянет). Возвращает report_id или None."""
        body = replaced if replaced is not None else "".join(parts)
        if not (body and body.strip()):
            return None
        try:
            banks = userdata.parse_query_signals(question).get("banks", [])
            is_report = (mode == "deep") or (len(body) > 800)
            report_id = None
            if is_report:
                report_id = userdata.save_report(
                    username, session_id, question, body,
                    payload={"sources": sources, "mode": mode}, banks=banks)
                # Само-дополняющийся профиль: каждый 3-й отчёт обновляем
                # LLM-нарратив интересов (в фоне, не блокируя ответ).
                try:
                    if userdata.count_reports(username) % 3 == 0:
                        from .profile_ai import generate_profile_note
                        asyncio.create_task(generate_profile_note(username))
                except Exception:
                    pass
            userdata.add_message(session_id, "assistant", body, {
                "sources": sources, "mode": mode, "report_id": report_id})
            return report_id
        except Exception:
            log.warning("[ai_analyze] persist failed", exc_info=True)
            return None

    try:
        async for ev in inner:
            try:
                data = json.loads(ev)
                t = data.get("type")
                if t == "text":
                    if data.get("chunk"):
                        parts.append(data["chunk"])
                    elif isinstance(data.get("text"), str):
                        replaced = data["text"]
                elif t == "report_replace" and isinstance(data.get("text"), str):
                    replaced = data["text"]
                elif t == "sources" and isinstance(data.get("sources"), list):
                    sources = data["sources"]
                elif t == "mode":
                    mode = data.get("value")
                elif t == "done" and not persisted:
                    # Персистим ДО done: клиент успевает получить report_id
                    # (кнопка «Поделиться» доступна сразу после прогона).
                    persisted = True
                    rid = _persist()
                    if rid:
                        yield json.dumps({"type": "report_saved", "report_id": rid},
                                         ensure_ascii=False)
            except Exception:
                pass
            yield ev
    finally:
        if not persisted:      # обрыв соединения/стрима без done — не теряем ответ
            _persist()


@app.post("/api/ai/analyze")
async def ai_analyze(req: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    # ── Demo hook: если DEMO_MODE=1 и вопрос совпадает с trigger_keywords ──
    # одного из demo/responses/*.json — стримим заготовленный ответ за ~25-30s.
    # Любые ДРУГИЕ вопросы идут в нормальный pipeline.
    if is_demo_mode_active():
        demo_resp = find_demo_response(req.question)
        if demo_resp is not None:
            return EventSourceResponse(
                stream_demo_response(req.question, demo_resp),
                media_type="text/event-stream",
                ping=10,
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                    "Content-Encoding": "identity",
                },
            )

    if not os.getenv("LLM_API_KEY"):
        raise HTTPException(503, "LLM_API_KEY не задан в .env")

    # Персонализация: заводим/продолжаем сессию истории, сохраняем вопрос,
    # обновляем профиль интересов, логируем событие (всё best-effort — не должно
    # уронить ответ, если БД недоступна).
    username = user.username
    session_id = req.session_id
    try:
        userdata.touch_user(username, user.name)
        session_id = userdata.get_or_create_session(username, req.session_id, req.question)
        userdata.add_message(session_id, "user", req.question,
                             {"force_deep": req.force_deep})
        signals = userdata.update_interests_from_query(username, req.question)
        userdata.log_event(username, "ai_query", {"question": req.question, **signals})
    except Exception:
        log.warning("[ai_analyze] pre-persist failed", exc_info=True)

    inner = stream_analysis(req.question, req.history, force_deep=req.force_deep)
    gen = (_persisting_stream(inner, username, session_id, req.question)
           if session_id else inner)

    # Deep-research pipeline идёт 90-300s. Между phase-событиями могут быть
    # длинные паузы (LLM-запросы по 30-60s). Без keep-alive проксики/браузер
    # рвут idle-соединение. ping=10 шлёт SSE-комментарий ':\n\n' каждые 10s —
    # это валидный SSE no-op, фронт игнорирует, прокси-таймауты не срабатывают.
    return EventSourceResponse(
        gen,
        media_type="text/event-stream",
        ping=10,
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Отключаем буферизацию у nginx/прокси (если в будущем встанут)
            "X-Accel-Buffering": "no",
            # Длинный response — гарантируем без сжатия, которое тоже буферизует
            "Content-Encoding": "identity",
        },
    )


# ── Clarify (модуль «asking») — уточняющая воронка ПЕРЕД research ─────────────
class ClarifyRequest(BaseModel):
    question: str
    history: list = []
    answers: Optional[list] = None    # None → генерим вопросы; задан → собираем enriched
    deep: bool = False

@app.post("/api/ai/clarify")
async def ai_clarify(req: ClarifyRequest):
    """Синхронный JSON (НЕ SSE). Два режима:
      answers is None → {complete, reason, questions} — нужна ли воронка и какие вопросы;
      answers задан    → {enriched_question, original} — обогащённый промпт для research."""
    # Demo-режим: воронку пропускаем — переписанный промпт сломал бы trigger_keywords.
    if is_demo_mode_active() and find_demo_response(req.question) is not None:
        return {"complete": True, "questions": [], "reason": "demo"}
    if req.answers is not None:
        enriched = await build_enriched_question(req.question, req.answers)
        return {"enriched_question": enriched, "original": req.question}
    return await generate_clarifications(req.question, req.history)


# ── PDF export ───────────────────────────────────────────────────────────────

class PdfExportRequest(BaseModel):
    question: str
    report_md: str
    sources: list[dict] = []
    meta: Optional[dict] = None
    # Verification + конфликты — отдельным полем чтобы рендерить как
    # styled-секцию в PDF (как в UI), а не сырым markdown'ом.
    verification: Optional[dict] = None
    # Charts specs (тот же формат что приходит через SSE event 'chart')
    # — будут отрендерены Chart.js'ом в Playwright Chromium и снапшотнуты
    # в PDF как самостоятельная секция перед источниками.
    charts: list[dict] = []
    # Богатые виджеты UI, которых раньше не было в PDF — рендерятся как
    # styled-секции (рейтинг-карточки, инсайты, пробелы, claim-check).
    ranking: Optional[dict] = None
    insights: list[dict] = []
    gaps: Optional[dict] = None
    claim_check: Optional[dict] = None

@app.post("/api/ai/export-pdf")
async def ai_export_pdf(req: PdfExportRequest):
    """Premium PDF export. Принимает report-markdown + sources + verification,
    возвращает PDF: обложка → тело → требуют проверки → источники.
    Рендеринг через Chromium (Playwright). ~3-5s на отчёт."""
    if not req.report_md or len(req.report_md) < 100:
        raise HTTPException(400, "Empty report content")
    from .pdf_export import export_report_to_pdf
    try:
        pdf_bytes = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: export_report_to_pdf(
                question=req.question, report_md=req.report_md,
                sources=req.sources or [], meta=req.meta or {},
                verification=req.verification,
                charts=req.charts or [],
                ranking=req.ranking, insights=req.insights or [],
                gaps=req.gaps, claim_check=req.claim_check),
        )
    except Exception as e:
        logging.getLogger(__name__).warning("PDF export failed: %s", e)
        raise HTTPException(500, f"PDF generation failed: {str(e)[:200]}")
    audit_id = (req.meta or {}).get("audit_id", "report")
    fname = f"auditlens_{audit_id}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ── health / readiness (для реверс-прокси и оркестратора контейнера) ─────────
# Регистрируются ДО catch-all spa_fallback (/{full_path:path}), иначе тот
# перехватил бы их и вернул 200+HTML (ложно-зелёный liveness).

@app.get("/healthz")
def healthz():
    """Liveness — процесс жив. БД НЕ трогаем: контейнер 'живой' даже если PG лежит."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness — готов обслуживать: проверяем коннект к БД (SELECT 1)."""
    try:
        with db.session() as s:
            s.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {e}")


# ── loophole module (mount router + static) ─────────────────────────────────
from ..loophole.web import router as loophole_router  # noqa: E402
app.include_router(loophole_router, prefix="/api/loophole")
LOOPHOLE_STATIC_DIR = Path(__file__).resolve().parent.parent / "loophole" / "static"


def _loophole_html_with_bust() -> str:
    """Cache-bust для loophole.jsx — иначе Babel/браузер держат старый чат-UI."""
    html_path = LOOPHOLE_STATIC_DIR / "loophole.html"
    html = html_path.read_text(encoding="utf-8")
    jsx_path = LOOPHOLE_STATIC_DIR / "loophole.jsx"
    if jsx_path.exists():
        v = int(jsx_path.stat().st_mtime)
        html = html.replace(
            'src="/static/loophole/loophole.jsx"',
            f'src="/static/loophole/loophole.jsx?v={v}"',
        )
    return html


@app.get("/static/loophole/loophole.html")
def loophole_page():
    return Response(
        content=_loophole_html_with_bust(),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Более длинный префикс — до общего /static, иначе Starlette отдаёт 404 на loophole.html.
app.mount("/static/loophole", StaticFiles(directory=LOOPHOLE_STATIC_DIR), name="loophole-static")

# ── static (SPA) ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _index_html_with_bust() -> str:
    """Подмешиваем cache-bust к src='/static/app.jsx' по mtime файла.
    Иначе браузер мог кэшировать старый JSX без PdfExportButton и других
    новых компонентов — пользователь видел «обновили на бэке, а UI старый».
    Bust-параметр на каждый ре-deploy меняется, браузер пере-фетчит."""
    idx = STATIC_DIR / "index.html"
    html = idx.read_text(encoding="utf-8")
    jsx_path = STATIC_DIR / "app.jsx"
    if jsx_path.exists():
        v = int(jsx_path.stat().st_mtime)
        html = html.replace('src="/static/app.jsx"',
                              f'src="/static/app.jsx?v={v}"')
    return html


@app.get("/")
def index():
    return Response(content=_index_html_with_bust(),
                    media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache, must-revalidate"})

@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    return Response(content=_index_html_with_bust(),
                    media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache, must-revalidate"})
