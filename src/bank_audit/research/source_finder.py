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
    """Параллельные web-search запросы, возвращает уникальные URL'ы."""
    queries = []
    if entity.bank_domain:
        queries.append(f"site:{entity.bank_domain} {entity.product}")
        queries.append(f"site:{entity.bank_domain} {entity.product} тарифы")
        queries.append(f"site:{entity.bank_domain} {entity.product} filetype:pdf")
    queries.append(f"{entity.bank_name} {entity.product} условия тарифы")
    queries.append(f"{entity.bank_name} {entity.product} site:banki.ru")

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

    # On-topic: gold_score >= 0.5 И значит topic есть в тексте (см. _enrich_source)
    on_topic = [s for s in db_sources if s.gold_score >= 0.5]

    # КРИТИЧНО: если on-topic < top_n → web search ОБЯЗАТЕЛЬНО.
    # Раньше web search запускался по low confidence, теперь — по topic-coverage.
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

    top = unique[:top_n]
    log.warning("[source_finder] %s gold sources for %s: %s",
                len(top), entity.bank_slug,
                [(s.url[-50:], round(s.gold_score, 2)) for s in top])
    return top
