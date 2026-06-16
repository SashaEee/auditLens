from __future__ import annotations
import json, os, asyncio, logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Query, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sse_starlette.sse import EventSourceResponse
from .. import db
from ..config import Settings
from ..ai.analyst import stream_analysis
from .demo_stream import is_demo_mode_active, find_demo_response, stream_demo_response
from ..notifier.email import EmailNotifier
from ..notifier.alerts import alerts_background_loop, run_once as alerts_run_once
from ..rag import cache as rag_cache
from ..rag.indexer import ingest_document_from_url
from ..rag.url_discovery import bootstrap_bank_profile, TOP_BANK_SITES
from ..rag.crawler import crawl_one_bank, crawl_all_profiles

STATIC_DIR = Path(__file__).parent / "static"
settings = Settings.load()
db.init(settings)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Фоновые циклы:
    #  • alerts_background_loop — раз в 30 мин quality_flag → email
    # (cookie-warming убран: требовал Playwright, на сервере циклически падал)
    tasks = [
        asyncio.create_task(alerts_background_loop()),
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── helpers ──────────────────────────────────────────────────────────────────

def q(sql: str, params: dict = {}):
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params).mappings().all()]

def scalar(sql: str, params: dict = {}):
    with db.session() as s:
        return s.execute(text(sql), params).scalar_one_or_none()


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

@app.get("/api/reviews/sentiment")
def reviews_sentiment():
    return q("SELECT * FROM v_review_sentiment_share ORDER BY total DESC")

@app.get("/api/reviews/list")
def reviews_list(bank_slug: Optional[str] = None, limit: int = 50):
    extra = "AND b.slug = :s" if bank_slug else ""
    return q(f"""
        SELECT r.review_id, b.name bank_name, b.slug bank_slug, b.is_sber,
               r.source, r.rating, r.title, left(r.text,400) text_short,
               r.posted_at, rs.label sentiment, r.status
          FROM review r JOIN bank b USING(bank_id)
          LEFT JOIN review_sentiment rs USING(review_id)
         WHERE 1=1 {extra}
         ORDER BY r.posted_at DESC NULLS LAST LIMIT :l
    """, {**({"s": bank_slug} if bank_slug else {}), "l": limit})


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
    from ..orchestrator.runner import ingest
    try:
        ingest(source, target)
    except Exception:
        pass  # статус пишется в extraction_run


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
    from ..orchestrator.runner import ingest
    for src in sources:
        try:
            ingest(src, None)
        except Exception:
            pass  # каждый источник пишет свой статус в extraction_run

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

@app.post("/api/ai/analyze")
async def ai_analyze(req: ChatRequest):
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
    # Deep-research pipeline идёт 90-300s. Между phase-событиями могут быть
    # длинные паузы (LLM-запросы по 30-60s). Без keep-alive проксики/браузер
    # рвут idle-соединение. ping=10 шлёт SSE-комментарий ':\n\n' каждые 10s —
    # это валидный SSE no-op, фронт игнорирует, прокси-таймауты не срабатывают.
    return EventSourceResponse(
        stream_analysis(req.question, req.history, force_deep=req.force_deep),
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
