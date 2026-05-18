"""Crawler: индексация key_pages топ-банков в фоновом режиме.

Стратегия:
  • Берём bank_profile.key_pages (заполнено bootstrap'ом)
  • Для каждого банка → ingest каждого URL из key_pages (max N топиков)
  • Распределяем нагрузку: задержка между банками (rate-limit) + jitter
  • Идемпотентно: уже-проиндексированный документ не индексируется повторно
    (UNIQUE на content_sha256)
  • Robots.txt уважается через `_should_crawl`

Запуск:
  • POST /api/rag/crawl-banks                — все банки с заполненным profile
  • POST /api/rag/crawl-bank/{slug}          — один банк
  • Cron weekly через background_loop (опционально)
"""
from __future__ import annotations
import asyncio, logging, random, time
from typing import Iterable
from sqlalchemy import text

from .. import db
from . import indexer

log = logging.getLogger(__name__)

# Лимиты, чтобы не упасть в bot-detect
MAX_URLS_PER_BANK = 8                         # топ-N топиков
INTER_URL_DELAY_S = (3, 8)                    # jitter между URLs в одном банке
INTER_BANK_DELAY_S = (10, 25)                 # jitter между банками


def crawl_one_bank(bank_slug: str, max_urls: int = MAX_URLS_PER_BANK) -> dict:
    """Crawl одного банка по key_pages из bank_profile."""
    with db.session() as s:
        row = s.execute(text("""
            SELECT bp.key_pages, b.bank_id
              FROM bank_profile bp
              JOIN bank b USING(bank_id)
             WHERE b.slug = :s
        """), {"s": bank_slug}).first()
    if not row:
        return {"bank_slug": bank_slug, "error": "no bank_profile"}

    key_pages, bank_id = row[0], row[1]
    if not key_pages or not isinstance(key_pages, dict):
        return {"bank_slug": bank_slug, "error": "no key_pages"}

    # Собираем список URL — берём первый URL для каждого топика
    urls_to_crawl: list[tuple[str, str]] = []   # (topic, url)
    for topic, url_list in key_pages.items():
        if not url_list:
            continue
        if isinstance(url_list, list):
            urls_to_crawl.append((topic, url_list[0]))
        elif isinstance(url_list, str):
            urls_to_crawl.append((topic, url_list))
        if len(urls_to_crawl) >= max_urls:
            break

    if not urls_to_crawl:
        return {"bank_slug": bank_slug, "error": "no urls in key_pages"}

    log.info("crawl_one_bank %s: starting, %s URLs", bank_slug, len(urls_to_crawl))
    results = []
    for i, (topic, url) in enumerate(urls_to_crawl):
        if i > 0:
            time.sleep(random.uniform(*INTER_URL_DELAY_S))
        try:
            # SPA-сайты крупных банков → prefer_browser=True
            # Документы (PDF/XLSX) → HTTP fine
            doc_ext = url.lower().rsplit(".", 1)[-1] if "." in url.rsplit("/", 1)[-1] else ""
            prefer_browser = doc_ext not in ("pdf", "xlsx", "xls", "pptx", "docx")
            r = indexer.ingest_document_from_url(
                url, bank_slug_hint=bank_slug, prefer_browser=prefer_browser,
            )
            results.append({
                "topic": topic, "url": url,
                "doc_id": r.document_id, "chunks": r.chunks_added,
                "doc_type": r.doc_type, "trust": r.trust_score,
                "is_new": r.is_new, "skipped": r.skipped_reason,
            })
            log.info("  %s [%s] → %s chunks (skipped: %s)",
                     bank_slug, topic, r.chunks_added, r.skipped_reason)
        except Exception as e:
            results.append({"topic": topic, "url": url, "error": str(e)[:200]})
            log.warning("  %s [%s] failed: %s", bank_slug, topic, e)

    n_new_chunks = sum(r.get("chunks", 0) for r in results)
    return {
        "bank_slug": bank_slug,
        "urls_attempted": len(urls_to_crawl),
        "chunks_added":   n_new_chunks,
        "results":        results,
    }


def crawl_all_profiles(bank_slugs: Iterable[str] | None = None) -> dict:
    """Crawl всех банков с заполненным bank_profile.
    bank_slugs — опциональный фильтр."""
    with db.session() as s:
        wh = "WHERE bp.key_pages IS NOT NULL AND bp.key_pages::text != '{}'"
        params: dict = {}
        if bank_slugs:
            wh += " AND b.slug = ANY(:slugs)"
            params["slugs"] = list(bank_slugs)
        rows = s.execute(text(f"""
            SELECT b.slug FROM bank_profile bp
              JOIN bank b USING(bank_id)
             {wh}
        """), params).all()
    slugs = [r[0] for r in rows]

    log.info("crawl_all_profiles: %s банков", len(slugs))
    summary = []
    for i, slug in enumerate(slugs):
        if i > 0:
            time.sleep(random.uniform(*INTER_BANK_DELAY_S))
        try:
            r = crawl_one_bank(slug)
            summary.append({"slug": slug, "chunks_added": r.get("chunks_added", 0),
                            "urls_attempted": r.get("urls_attempted", 0)})
        except Exception as e:
            summary.append({"slug": slug, "error": str(e)[:200]})
            log.warning("crawl %s failed: %s", slug, e)

    total_chunks = sum(s.get("chunks_added", 0) for s in summary)
    return {"banks": len(slugs), "total_chunks_added": total_chunks,
            "details": summary}


async def crawl_background_loop(initial_delay_s: int = 600,
                                 interval_h: int = 24 * 7):
    """Cron-style: раз в неделю crawl всех профилей. Запуск с задержкой
    initial_delay_s после старта сервера (чтобы embedder загрузился)."""
    await asyncio.sleep(initial_delay_s)
    while True:
        try:
            log.info("crawl_background_loop: starting weekly crawl")
            await asyncio.get_event_loop().run_in_executor(None, crawl_all_profiles, None)
        except Exception as e:
            log.warning("crawl_background_loop failed: %s", e)
        await asyncio.sleep(interval_h * 3600)
