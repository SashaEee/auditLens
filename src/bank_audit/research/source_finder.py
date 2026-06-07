"""Source Finder — для каждой Entity находит 2-3 «gold sources».

Gold source — это документ, реально описывающий продукт у банка.
Признаки качества:
  • bank_official (trust ≥ 0.9)
  • длинный текст (>2000 chars, продуктовая страница, а не превью)
  • плотность тарифных маркеров (₽, %, срок, тариф, документ)
  • продуктовые URL-паттерны (/cards/, /credits/, /personal/...)
  • не promo (URL не содержит /promo/, /akcii/, /news/)

Поиск работает в 3 параллельных полосах:
  A) pgvector + HyDE по уже проиндексированной БД
  B) targeted web-search: site:{bank.domain} {product}
  C) известные URL-templates (для случая когда поисковики банят)

Результаты merge + dedup + rank → top-3 на entity.
"""
from __future__ import annotations
import asyncio, logging, os, re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import text as _t

from .. import db
from ..rag import embedder
from ..rag.indexer import ingest_document_from_url
from ..rag.web_search import (search as web_search,
                                get_direct_product_urls)
from .entity_extractor import Entity

log = logging.getLogger(__name__)


@dataclass
class GoldSource:
    """Document, обогащённый метрикой 'gold-ness'."""
    url: str
    title: str
    bank_slug: str | None
    domain: str
    trust_score: float
    text: str                  # полный текст (≥80 chars)
    headings_path: str | None = None
    document_id: int | None = None
    # Метрики качества
    length: int = 0
    tariff_density: float = 0.0   # доля «продуктовых» маркеров (₽,%,срок,тариф)
    has_promo_url: bool = False
    is_product_url: bool = False
    gold_score: float = 0.0       # финальный rank

    def to_dict(self) -> dict:
        return {
            "url": self.url, "title": self.title,
            "bank_slug": self.bank_slug, "domain": self.domain,
            "trust_score": self.trust_score, "gold_score": self.gold_score,
            "length": self.length, "tariff_density": self.tariff_density,
            "document_id": self.document_id,
        }


# ── Метрики качества "gold-ness" ────────────────────────────────────────
_TARIFF_MARKERS_RE = re.compile(
    r"(\d+\s*(?:%|₽|руб|тыс|млн|млрд|лет|дней|мес|годов|раб\.?дн)"
    r"|тариф|комисси|ставк|лимит|документ|требован|условия|выпуск|обслужив"
    r"|расч[её]т|открыт|подключ)",
    re.IGNORECASE,
)
_PRODUCT_URL_RE = re.compile(
    r"/(card|карт|credit|kredit|deposit|vklad|mortgage|ipoteka|"
    r"acquiring|rko|investments|business|personal|private|tariff|tarif|"
    r"product|usluga|offer)/",
    re.IGNORECASE,
)
_PROMO_URL_RE = re.compile(
    r"/(promo|akci[ai]|sale|event|spasibo|citydrive|bonus-|giveaway|"
    r"news|press|blog|article|stories|quiz)",
    re.IGNORECASE,
)


def _compute_gold_score(src: GoldSource) -> float:
    """Композитный rank: trust × length-density × tariff-density × URL-fit."""
    # Trust [0..1]
    trust = max(0.0, min(1.0, src.trust_score))
    # Length-bonus: 0 для <500, 1 для ≥4000 chars
    length_b = max(0.0, min(1.0, (src.length - 500) / 3500))
    # Tariff-density [0..1] (capped)
    tariff = min(1.0, src.tariff_density * 4.0)   # density 0.25 → score 1
    # URL bonus / penalty
    url_b = 0.0
    if src.is_product_url: url_b += 0.3
    if src.has_promo_url:   url_b -= 0.5
    # Финальная формула: trust взвешен сильнее всего
    score = (0.45 * trust) + (0.20 * length_b) + (0.25 * tariff) + url_b
    return max(0.0, min(1.0, score))


def _enrich_source(src: GoldSource, topic_keywords: list[str] | None = None) -> GoldSource:
    """Вычисляет метрики качества. topic_keywords — для пост-фильтра
    релевантности по теме (доверенность/эквайринг/ипотека и т.п.)."""
    txt = src.text or ""
    src.length = len(txt)
    if src.length > 0:
        marker_hits = len(_TARIFF_MARKERS_RE.findall(txt[:8000]))
        src.tariff_density = marker_hits / max(1, src.length / 1000)
    url_low = (src.url or "").lower()
    src.has_promo_url = bool(_PROMO_URL_RE.search(url_low))
    src.is_product_url = bool(_PRODUCT_URL_RE.search(url_low))

    # Topic relevance — критично! Без этого pgvector возвращает любые
    # тарифные документы банка (карты/вклады/RKO), даже когда тема —
    # доверенность. Если topic не упоминается в тексте хотя бы 2 раза —
    # источник off-topic. Снижаем gold_score.
    src.gold_score = _compute_gold_score(src)
    if topic_keywords:
        low = txt.lower()
        hits = sum(low.count(kw.lower()) for kw in topic_keywords if kw and len(kw) >= 4)
        if hits == 0:
            src.gold_score *= 0.1   # off-topic — почти исключаем
        elif hits == 1:
            src.gold_score *= 0.5   # слабая релевантность
        # ≥2 хитов — оставляем gold_score как есть
    return src


# ── Lane A: pgvector + HyDE ──────────────────────────────────────────────
async def _generate_hyde(client: AsyncOpenAI, entity: Entity,
                          model: str) -> str:
    """Генерирует гипотетический ответ для embedding-поиска.
    Главное: должен содержать конкретные слова продукта (доверенность,
    эквайринг, ипотека) — иначе HyDE-embedding ловит «любой тарифный
    документ» и pgvector возвращает off-topic chunks."""
    prompt = (
        f"Напиши КОРОТКИЙ (3-5 строк) фрагмент тарифного документа банка "
        f"{entity.bank_name} КОНКРЕТНО про продукт «{entity.product}». "
        f"ОБЯЗАТЕЛЬНО упомяни сам продукт по имени несколько раз. "
        f"Включи цифры (ставка/лимит/срок/комиссия) и требуемые документы. "
        f"Стилем тарифной страницы. БЕЗ маркетинга и акций. ТОЛЬКО факты."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model, max_tokens=300, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            ), timeout=15)
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.info("HyDE generation failed for %s: %s", entity.bank_slug, e)
        # Fallback: используем сам product как query
        return f"{entity.product} {entity.bank_name} тарифы условия"


def _search_db_chunks(query_vec: list[float], entity: Entity,
                       top_k: int = 12,
                       topic_keywords: list[str] | None = None) -> list[GoldSource]:
    """pgvector search в БД, ограниченный по bank_slug."""
    try:
        with db.session() as s:
            rows = s.execute(_t("""
                SELECT d.document_id, d.url, d.title, d.trust_score,
                       d.fetched_at, dc.headings_path,
                       b.slug AS bank_slug,
                       st.domain AS source_domain,
                       string_agg(dc.text, E'\\n\\n' ORDER BY dc.idx) AS full_text,
                       MIN(dc.embedding <=> CAST(:qvec AS vector)) AS distance
                  FROM document_chunk dc
                  JOIN document d ON d.document_id = dc.document_id
                  LEFT JOIN bank b ON b.bank_id = d.bank_id
                  LEFT JOIN source_trust st ON st.source_id = d.source_id
                 WHERE b.slug = :slug
                   AND d.trust_score >= 0.5
                   AND d.is_sponsored = FALSE
                 GROUP BY d.document_id, d.url, d.title, d.trust_score,
                          d.fetched_at, dc.headings_path, b.slug, st.domain
                 ORDER BY MIN(dc.embedding <=> CAST(:qvec AS vector))
                 LIMIT :lim
            """), {"qvec": str(query_vec), "slug": entity.bank_slug,
                    "lim": top_k}).mappings().all()
        out = []
        for r in rows:
            src = GoldSource(
                url=r["url"], title=r["title"] or "",
                bank_slug=r["bank_slug"],
                domain=r["source_domain"] or "",
                trust_score=float(r["trust_score"] or 0),
                text=r["full_text"] or "",
                headings_path=r["headings_path"],
                document_id=r["document_id"],
            )
            out.append(_enrich_source(src, topic_keywords=topic_keywords))
        return out
    except Exception as e:
        log.warning("_search_db_chunks failed for %s: %s", entity.bank_slug, e)
        return []


# ── Lane B + C: live web-search + direct URLs ───────────────────────────
def _search_web_for_entity(entity: Entity, max_per_query: int = 5) -> list[str]:
    """Параллельные web-search запросы, возвращает уникальные URL'ы.

    Стратегия (приоритет сверху вниз):
    1. Bank-сайт с продуктом + основные synonyms (3-5 запросов)
    2. АГРЕГАТОРЫ: banki.ru/sravni.ru — у них специальные обзорные страницы
       по продуктам и сравнения. Goldmine для сравнительных вопросов.
    3. Generic поиск с фильтром «не бизнес» для личных продуктов.
    """
    queries = []
    product = entity.product
    # Top-2 наиболее распространённых synonyms (помимо основного product)
    extra_terms = [s for s in (entity.product_synonyms or [])
                   if s and s.lower() != product.lower() and len(s) >= 4][:2]

    # 1. Bank-сайт с разными вариантами product
    if entity.bank_domain:
        queries.append(f"site:{entity.bank_domain} {product}")
        for syn in extra_terms:
            queries.append(f"site:{entity.bank_domain} {syn}")
        queries.append(f"site:{entity.bank_domain} {product} тарифы")

    # 2. Агрегаторы — обзорные страницы, рейтинги
    queries.append(f"site:banki.ru {product} {entity.bank_name}")
    queries.append(f"site:sravni.ru {product} {entity.bank_name}")
    # Обзорная статья «топ N» — обычно с конкретикой
    queries.append(f"{product} обзор условия {entity.bank_name}")

    # 3. Negative-keyword filter: если product указывает на личный сегмент,
    # исключаем бизнес/корпоратив, и наоборот
    p_low = product.lower()
    is_personal = any(k in p_low for k in ["пенсион", "ветеран", "для физ", "детск", "молод"])
    is_business = any(k in p_low for k in ["ип", "бизнес", "юр", "эквайринг", "рко"])
    if is_personal:
        queries.append(f"{entity.bank_name} {product} -бизнес -ИП -корпоратив")
    elif is_business:
        queries.append(f"{entity.bank_name} {product} ИП тарифы")

    urls: list[str] = []
    seen: set[str] = set()
    for q in queries:
        try:
            results = web_search(q, max_results=max_per_query) or []
        except Exception as e:
            log.info("web_search failed for %r: %s", q, e)
            continue
        for r in results:
            u = r.get("url")
            if u and u not in seen:
                seen.add(u); urls.append(u)
    log.warning("[source_finder] %s: %s web URLs after %s queries",
                 entity.bank_slug, len(urls), len(queries))
    return urls


def _direct_urls_for_entity(entity: Entity) -> list[str]:
    """Известные landing-pages: backup когда поисковики банят."""
    if not entity.bank_domain:
        return []
    try:
        items = get_direct_product_urls(
            entity.bank_domain, entity.product,
            synonyms=entity.product_synonyms,
            audience_filter=entity.audience,
            bank_slug=entity.bank_slug,
        )
        return [it["url"] for it in items if it.get("url")]
    except Exception as e:
        log.info("get_direct_product_urls failed: %s", e)
        return []


def _ingest_urls_parallel(urls: list[str], slug_hint: str,
                          max_workers: int = 4) -> int:
    """Ingest URL'ов параллельно. Возвращает кол-во успешно проиндексированных."""
    if not urls:
        return 0
    def _do(u: str):
        try:
            return ingest_document_from_url(u, bank_slug_hint=slug_hint,
                                              prefer_browser=False)
        except Exception as e:
            log.info("ingest failed for %s: %s", u, e)
            return None
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(_do, urls[:12]))   # cap для безопасности
    return sum(1 for r in results if r and getattr(r, "document_id", None))


# ── Главная функция ────────────────────────────────────────────────────
async def find_gold_sources(client: AsyncOpenAI, entity: Entity,
                              top_n: int = 3,
                              model: str | None = None) -> list[GoldSource]:
    """Для entity возвращает top-N gold sources.

    Стратегия:
      1. pgvector + HyDE для существующей БД (быстро)
      2. Если <top_n high-score sources → live web search + ingest + повторный SQL
      3. Если всё ещё мало → direct URL templates как last resort
    """
    model = model or os.getenv("LLM_MODEL_FAST") or os.getenv("LLM_MODEL_NAME",
                                                                "gpt-4o-mini")

    # Topic-keywords для пост-фильтра (главное против off-topic из БД)
    topic_kws = list(set([entity.product.lower()] +
                          [s.lower() for s in (entity.product_synonyms or []) if len(s) >= 4]))
    # Если product это «доверенность на распоряжение счётом» — добавим короткий корень
    main_word = entity.product.lower().split()[0] if entity.product else ""
    if main_word and len(main_word) >= 5 and main_word not in topic_kws:
        topic_kws.append(main_word[:6])   # доверенность → доверен

    # Lane A: HyDE → embedding → pgvector
    hyde_text = await _generate_hyde(client, entity, model)
    try:
        qvec = await asyncio.get_event_loop().run_in_executor(
            None, embedder.embed_one, hyde_text)
    except Exception as e:
        log.warning("HyDE embed failed: %s", e)
        qvec = None
    db_sources: list[GoldSource] = []
    if qvec:
        db_sources = await asyncio.get_event_loop().run_in_executor(
            None, _search_db_chunks, qvec, entity, 12, topic_kws)
    log.info("[source_finder] %s: %s DB sources from HyDE (after topic-filter)",
             entity.bank_slug, len(db_sources))

    # On-topic: gold_score >= 0.3 (мягкий порог — лучше слабый источник
    # чем 0). Главный фильтр в _enrich_source уже отфильтровал по topic-keywords.
    on_topic = [s for s in db_sources if s.gold_score >= 0.3]

    # КРИТИЧНО: если on-topic < top_n → web search ОБЯЗАТЕЛЬНО.
    if len(on_topic) < top_n:
        log.warning("[source_finder] %s: only %s on-topic in DB → live web search",
                    entity.bank_slug, len(on_topic))
        web_urls = await asyncio.get_event_loop().run_in_executor(
            None, _search_web_for_entity, entity)
        direct_urls = await asyncio.get_event_loop().run_in_executor(
            None, _direct_urls_for_entity, entity)
        all_urls = list(dict.fromkeys(web_urls + direct_urls))   # dedup keeping order
        if all_urls:
            ingested = await asyncio.get_event_loop().run_in_executor(
                None, _ingest_urls_parallel, all_urls, entity.bank_slug)
            log.warning("[source_finder] %s: ingested %s/%s URLs",
                        entity.bank_slug, ingested, len(all_urls))
            # Повторный SQL после ingest
            if qvec:
                db_sources = await asyncio.get_event_loop().run_in_executor(
                    None, _search_db_chunks, qvec, entity, 12, topic_kws)

    # Ranking + dedup by URL
    seen_urls = set()
    unique: list[GoldSource] = []
    for s in sorted(db_sources, key=lambda x: -x.gold_score):
        if s.url in seen_urls: continue
        seen_urls.add(s.url)
        unique.append(s)

    unique = _filter_by_product(unique, entity)
    top = unique[:top_n]
    log.warning("[source_finder] %s gold sources for %s: %s",
                len(top), entity.bank_slug,
                [(s.url[-50:], round(s.gold_score, 2)) for s in top])
    return top


# ════════════════════════════════════════════════════════════════════════
# EXTENDED FINDER — Phase 2
# ════════════════════════════════════════════════════════════════════════


async def find_gold_sources_extended(client: AsyncOpenAI,
                                       entity: Entity,
                                       core_schema=None,
                                       top_n: int = 10,
                                       model: str | None = None) -> list[GoldSource]:
    """РАСШИРЕННАЯ версия для Phase 2.

    Отличия от find_gold_sources:
      • Использует query_planner — 8-12 разных queries вместо одного set
      • Поддерживает PDF (через pdf_extractor)
      • Возвращает top_n=10 (vs 3) — больше материала для extract
      • Dedup по content-fingerprint (region-similar pages)

    Backward compat: если что-то падает — fallback на find_gold_sources.
    """
    from .query_planner import plan_queries
    from .pdf_extractor import extract_pdf_text, is_pdf_url

    model = model or os.getenv("LLM_MODEL_FAST") or \
              os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")

    # 1) Plan queries (LLM-driven multi-query)
    try:
        planned = await plan_queries(client, entity, core_schema or [],
                                       model=model, n_queries=12)
    except Exception as e:
        log.warning("[extended_finder] query_planner failed: %s — fallback to base", e)
        return await find_gold_sources(client, entity, top_n=min(top_n, 5),
                                          model=model)

    # 2) Pgvector first (быстрая лень-проверка БД)
    topic_kws = list(set([entity.product.lower()] +
                          [s.lower() for s in (entity.product_synonyms or []) if len(s) >= 4]))
    main_word = entity.product.lower().split()[0] if entity.product else ""
    if main_word and len(main_word) >= 5 and main_word not in topic_kws:
        topic_kws.append(main_word[:6])

    loop = asyncio.get_event_loop()
    hyde_text = await _generate_hyde(client, entity, model)
    try:
        qvec = await loop.run_in_executor(None, embedder.embed_one, hyde_text)
    except Exception as e:
        log.warning("[extended_finder] HyDE embed failed: %s", e)
        qvec = None

    db_sources: list[GoldSource] = []
    if qvec:
        db_sources = await loop.run_in_executor(
            None, _search_db_chunks, qvec, entity, 12, topic_kws)
    on_topic_db = [s for s in db_sources if s.gold_score >= 0.3]
    log.warning("[extended_finder] %s: %s on-topic from DB",
                 entity.bank_slug, len(on_topic_db))

    # 3) Web search — ВСЕГДА выполняется для Phase 2 (больше источников)
    def _search_one(q_text: str) -> list[str]:
        try:
            results = web_search(q_text, max_results=5) or []
            return [r.get("url", "") for r in results if r.get("url")]
        except Exception as e:
            log.info("web_search failed for %r: %s", q_text, e)
            return []

    # Параллельный поиск по всем queries
    search_tasks = [loop.run_in_executor(None, _search_one, p.text)
                     for p in planned]
    search_lists = await asyncio.gather(*search_tasks, return_exceptions=False)

    web_urls: list[str] = []
    seen_urls: set[str] = set()
    for q, urls in zip(planned, search_lists):
        for u in urls:
            if u and u not in seen_urls:
                seen_urls.add(u)
                web_urls.append(u)

    log.warning("[extended_finder] %s: %s unique URLs from %s queries",
                 entity.bank_slug, len(web_urls), len(planned))

    # 4) Split URLs: PDF separately handled, остальные через ingest
    pdf_urls = [u for u in web_urls if is_pdf_url(u)][:5]   # max 5 PDF
    html_urls = [u for u in web_urls if not is_pdf_url(u)][:15]   # max 15 HTML

    # 5) PDF extraction (parallel)
    pdf_sources: list[GoldSource] = []
    if pdf_urls:
        sem = asyncio.Semaphore(3)

        async def _one_pdf(url: str) -> GoldSource | None:
            async with sem:
                try:
                    text = await extract_pdf_text(url)
                except Exception as e:
                    log.info("[extended_finder] PDF extract failed %s: %s", url, e)
                    return None
                if not text or len(text) < 300:
                    return None
                from urllib.parse import urlparse
                host = urlparse(url).netloc.lower().removeprefix("www.")
                # PDF официального банка — высокий trust
                trust = 0.98 if entity.bank_domain and entity.bank_domain in host else 0.7
                return GoldSource(
                    url=url, title=url.split("/")[-1][:120],
                    bank_slug=entity.bank_slug, domain=host,
                    trust_score=trust, text=text,
                    length=len(text), gold_score=trust * 0.9,
                    document_id=None,
                )
        pdf_results = await asyncio.gather(*[_one_pdf(u) for u in pdf_urls],
                                              return_exceptions=False)
        pdf_sources = [s for s in pdf_results if s]
        log.warning("[extended_finder] %s: %s PDF sources extracted",
                     entity.bank_slug, len(pdf_sources))

    # 6) Ingest HTML URLs — добавит их в БД, чтобы DB-поиск нашёл
    if html_urls:
        ingested = await loop.run_in_executor(
            None, _ingest_urls_parallel, html_urls, entity.bank_slug)
        log.warning("[extended_finder] %s: ingested %s/%s HTML URLs",
                     entity.bank_slug, ingested, len(html_urls))
        # Re-search БД с новым контентом
        if qvec and ingested > 0:
            db_sources = await loop.run_in_executor(
                None, _search_db_chunks, qvec, entity, 16, topic_kws)

    # 7) Combine: DB sources + PDF sources, dedup by URL и content-fingerprint
    all_sources: list[GoldSource] = []
    seen_urls = set()
    seen_fingerprints = set()

    for s in pdf_sources + sorted(db_sources, key=lambda x: -x.gold_score):
        if s.url in seen_urls:
            continue
        # Content-fingerprint dedup (одинаковая страница в разных регионах)
        fp = _content_fingerprint(s.text)
        if fp and fp in seen_fingerprints:
            log.info("[extended_finder] dedup by content: %s (similar to existing)",
                      s.url[-50:])
            continue
        seen_urls.add(s.url)
        if fp:
            seen_fingerprints.add(fp)
        all_sources.append(s)

    # Продуктовая дисциплина: убрать источники про чужой продукт
    all_sources = _filter_by_product(all_sources, entity)

    top = all_sources[:top_n]
    log.warning("[extended_finder] %s: %s sources final (%s PDF, %s HTML/DB)",
                 entity.bank_slug, len(top),
                 sum(1 for s in top if is_pdf_url(s.url)),
                 sum(1 for s in top if not is_pdf_url(s.url)))
    return top


# Сигнатуры продуктовых категорий (по URL + заголовку) — для отсева источников
# про ДРУГОЙ продукт (в авто-отчёт попадали вклады/ипотека/карты из общей БД).
_PRODUCT_CATEGORIES = {
    "auto":      ("автокред", "авток", "avtokredit", "autocredit", "auto-loan",
                   "auto_loan", "car-loan", "/auto/"),
    "deposit":   ("вклад", "депозит", "накопит", "сберсчёт", "deposit", "vklad",
                   "nakopit", "savings"),
    "mortgage":  ("ипотек", "ipotek", "ipoteka", "mortgage", "hypothec", "жилищн"),
    "card":      ("дебетов", "кредитн карт", "кредитка", "cashback", "кэшбэк",
                   "creditcard", "credit-card", "debit", "/cards/", "/karty/"),
    "consumer":  ("потребит", "наличными", "cash-loan", "kredit-nalich", "потребкредит"),
    "business":  ("эквайринг", "acquiring", "/rko", "расчётно-кассов", "малый бизнес"),
    "insurance": ("осаго", "каско", "kasko", "osago", "страхование жизни"),
    "investment":("брокер", "иис", "пиф", "broker", "invest"),
}


def _detect_categories(blob: str) -> set[str]:
    blob = (blob or "").lower()
    return {cat for cat, kws in _PRODUCT_CATEGORIES.items()
            if any(k in blob for k in kws)}


def _filter_by_product(sources: list[GoldSource], entity: Entity) -> list[GoldSource]:
    """Отсев источников, которые ЯВНО про другую продуктовую категорию.

    Детектит категорию запрошенного продукта; убирает источники, чья категория
    непуста и НЕ пересекается с запрошенной (вклад/ипотека/карта в авто-отчёте).
    Источники без явной категории НЕ трогаем (не over-фильтруем)."""
    req_blob = (entity.product + " " + " ".join(entity.product_synonyms or "")).lower()
    req_cats = _detect_categories(req_blob)
    if not req_cats:
        return sources                      # не можем определить — оставляем всё
    kept, dropped = [], []
    for s in sources:
        cats = _detect_categories(f"{s.url} {s.title or ''}")
        if cats and not (cats & req_cats):
            dropped.append(s)
            continue
        kept.append(s)
    if dropped:
        log.warning("[source_finder] %s: отсеяно %d источников не по теме (%s): %s",
                     entity.bank_slug, len(dropped), "/".join(sorted(req_cats)),
                     [d.url[-40:] for d in dropped[:5]])
    # safety: если фильтр выкосил всё — возвращаем исходное
    return kept or sources


def _content_fingerprint(text: str, sample_len: int = 200) -> str:
    """Simple content fingerprint: первые non-trivial слова → joined.

    Используется для dedup региональных копий типа vtb.ru/.../ekaterinburg/,
    .../nizhnij-novgorod/ — у них одинаковый body.
    """
    if not text or len(text) < 100:
        return ""
    # Берём первые 500 chars (это header+intro обычно), удаляем geo-маркеры
    sample = text[:1000].lower()
    # Удалим часто меняющиеся geo-слова между копиями страниц
    import re as _re
    sample = _re.sub(r"\b(в|из|для)\s+[А-ЯA-Z][а-яa-z\-]+", " ", sample)
    # Извлечь токены 4+ символов
    words = _re.findall(r"[а-яёa-z0-9]{4,}", sample)
    # Берём первые 30 — этого достаточно для отличить разные страницы
    sig = " ".join(words[:30])
    return sig[:sample_len]
