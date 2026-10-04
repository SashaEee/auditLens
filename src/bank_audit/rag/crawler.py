"""Crawler: индексация key_pages топ-банков в фоновом режиме.

Стратегия:
  • Берём bank_profile.key_pages (заполнено bootstrap'ом)
  • Для каждого банка → ingest каждого URL из key_pages (max N топиков)
  • Распределяем нагрузку: задержка между банками (rate-limit) + jitter
  • Идемпотентно: уже-проиндексированный документ не индексируется повторно
    (UNIQUE на content_sha256)
  • robots.txt не проверяется (обход только ключевых страниц продуктов из
    профиля банка, с паузами и бюджетом)

Запуск:
  • POST /api/rag/crawl-banks                — все банки с заполненным profile
  • POST /api/rag/crawl-bank/{slug}          — один банк
  • Ночной обход по расписанию: crawl_nightly (digest/scheduler.kb_crawl_loop)
"""
from __future__ import annotations
import asyncio, logging, random, time
from datetime import datetime, timedelta
from typing import Callable, Iterable
from sqlalchemy import text

from .. import db
from . import indexer

log = logging.getLogger(__name__)

# Лимиты, чтобы не упасть в bot-detect
MAX_URLS_PER_BANK = 8                         # топ-N топиков
INTER_URL_DELAY_S = (3, 8)                    # jitter между URLs в одном банке
INTER_BANK_DELAY_S = (10, 25)                 # jitter между банками
# порядок тем обхода — как на карте покрытия (web/app.KNOWLEDGE_TOPIC_ORDER)
_TOPIC_PRIORITY = ("deposits", "credits", "mortgage", "cards", "cards_credit", "cards_debit",
                   "auto", "tariffs", "fees", "transfers", "transfers_intl", "rko",
                   "business", "investments", "documents", "document", "premium",
                   "mobile_app", "support", "about")


def crawl_one_bank(bank_slug: str, max_urls: int = MAX_URLS_PER_BANK, *,
                   rotation: int = 0, record_kind: str | None = None,
                   should_stop: Callable[[], bool] | None = None) -> dict:
    """Crawl одного банка по key_pages из bank_profile.

    rotation — какой адрес темы брать (раньше всегда первый: одни и те же ≤8
    страниц). record_kind — писать журнал происхождения (kind='crawl'): по нему
    «Техническое состояние индекса» показывает обход, а карта — сбои (ДАН-02).
    should_stop — проверка перед каждым адресом (окно ночного сбора, дедлайн)."""
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

    # Темы — в порядке важности для аудита, а не в порядке ключей jsonb (Postgres
    # отдаёт их по длине: rko, auto, fees, about… — вклады и ипотека за бюджет
    # не попадали никогда). Окно тем сдвигается с rotation, адрес внутри темы
    # меняется раз в полный круг тем: за несколько проходов читается всё.
    topics = sorted((t for t, v in key_pages.items() if v),
                    key=lambda t: (_TOPIC_PRIORITY.index(t) if t in _TOPIC_PRIORITY
                                   else len(_TOPIC_PRIORITY), t))
    urls_to_crawl: list[tuple[str, str]] = []   # (topic, url)
    if topics:
        per = max(1, max_urls)
        cycles = -(-len(topics) // per)
        start = (rotation * per) % len(topics)
        for topic in (topics[start:] + topics[:start])[:per]:
            url_list = key_pages[topic]
            if isinstance(url_list, list):
                urls_to_crawl.append((topic, url_list[(rotation // cycles) % len(url_list)]))
            elif isinstance(url_list, str):
                urls_to_crawl.append((topic, url_list))

    if not urls_to_crawl:
        return {"bank_slug": bank_slug, "error": "no urls in key_pages"}

    log.info("crawl_one_bank %s: starting, %s URLs", bank_slug, len(urls_to_crawl))
    results = []
    run_id = f"crawl:{bank_slug}:{datetime.now().strftime('%Y%m%d')}"
    for i, (topic, url) in enumerate(urls_to_crawl):
        if should_stop and should_stop():
            log.info("crawl_one_bank %s: остановлен (окно сбора или дедлайн)", bank_slug)
            break
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
            if record_kind:
                from .ingest_queue import record_origin
                record_origin(url, r.document_id, r.skipped_reason,
                              {"kind": record_kind, "run_id": run_id},
                              "browser" if prefer_browser else "http")
        except Exception as e:
            results.append({"topic": topic, "url": url, "error": str(e)[:200]})
            log.warning("  %s [%s] failed: %s", bank_slug, topic, e)
            if record_kind:
                from .ingest_queue import record_origin
                # внутренняя ошибка (эмбеддер, БД) — не сбой сайта банка
                record_origin(url, None, "error", {"kind": record_kind, "run_id": run_id},
                              None)

    n_new_chunks = sum(r.get("chunks", 0) for r in results)
    return {
        "bank_slug": bank_slug,
        "urls_attempted": len(results),
        "new": sum(1 for r in results if r.get("is_new") and r.get("chunks")),
        "failed": sum(1 for r in results if r.get("error") or r.get("skipped") in
                      ("captcha", "fetch_failed", "empty_after_parse", "antibot_stub")),
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


# ── Ночной обход (аудит 03.10, ДАН-02) ────────────────────────────────────────
# Обход сайтов не вызывался по расписанию ни разу: crawl_* дёргались только
# ручками API, и архив по банкам замер с конца августа.

def due_banks(now: datetime, last: dict, banks: list[str], every_d: int,
              sber_every_d: int) -> list[str]:
    """Кому пора: Сбер — раз в sber_every_d дней, остальные — раз в every_d;
    Сбер первым, затем самые давние. Допуск 2 ч — чтобы ночь не «съезжала»."""
    per = lambda s: sber_every_d if s == "sberbank" else every_d  # noqa: E731
    slack = timedelta(hours=2)
    due = [s for s in banks if last.get(s) is None
           or now - last[s] >= timedelta(days=per(s)) - slack]
    floor = datetime.min.replace(tzinfo=now.tzinfo)
    return sorted(due, key=lambda s: (s != "sberbank", last.get(s) or floor))


def last_crawl_by_bank() -> dict:
    with db.session() as s:
        rows = s.execute(text("""
            SELECT split_part(run_id, ':', 2) AS slug, max(created_at)
              FROM document_origin
             WHERE kind = 'crawl' AND run_id LIKE 'crawl:%'
             GROUP BY 1""")).all()
    return {r[0]: r[1] for r in rows}


def crawl_nightly(banks: list[str], *, every_d: int, sber_every_d: int, max_urls: int,
                  should_stop: Callable[[], bool]) -> dict:
    """Обход банков, у которых подошёл срок, в пределах бюджета адресов."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    left, out = max_urls, []
    for slug in due_banks(now, last_crawl_by_bank(), banks, every_d, sber_every_d):
        if left <= 0 or should_stop():
            break
        rot = now.timetuple().tm_yday // max(1, sber_every_d if slug == "sberbank" else every_d)
        r = crawl_one_bank(slug, max_urls=min(MAX_URLS_PER_BANK, left), rotation=rot,
                           record_kind="crawl", should_stop=should_stop)
        left -= r.get("urls_attempted", 0) or 0
        out.append(r)
        time.sleep(random.uniform(*INTER_BANK_DELAY_S))
    return {"banks": [r.get("bank_slug") for r in out], "urls": max_urls - left,
            "new": sum(r.get("new", 0) for r in out),
            "failed": sum(r.get("failed", 0) for r in out),
            "errors": [r.get("error") for r in out if r.get("error")]}

