"""Web research tools — обёртки над web_search + fetcher + passive indexing.

Это «руки» автономных агентов. Каждый вызов:
  1. ищет/читает в web
  2. возвращает текст LLM
  3. пассивно индексирует найденное в БД (document) + регистрирует в SourceRegistry

БД = кэш: завтра тот же запрос найдёт этот документ через semantic_search.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

from .source_registry_helper import register_source

log = logging.getLogger(__name__)


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _trust_for(domain: str, url: str) -> float:
    """Эвристика доверия по домену/URL (для SourceRegistry)."""
    d = domain.lower()
    url_l = url.lower()
    # Регуляторные
    reg = ("cbr.ru", "pravo.gov.ru", "consultant.ru", "garant.ru", "fas.gov.ru",
           "nalog.gov.ru", "minfin.gov.ru", "kremlin.ru", "government.ru",
           "notariat.ru", "sfr.gov.ru", "mil.ru", "asv.org.ru", "sbp.nspk.ru")
    if any(r == d or d.endswith("." + r) for r in reg):
        return 0.98
    if d.endswith(".gov.ru") or d.endswith(".mil.ru"):
        return 0.95
    if url_l.endswith(".pdf"):
        return 0.9
    # Офиц. сайты банков (по домену 2 уровня)
    bank_domains = ("sberbank.ru", "vtb.ru", "alfabank.ru", "tbank.ru", "tinkoff.ru",
                    "sovcombank.ru", "gazprombank.ru", "rshb.ru", "domrfbank.ru",
                    "open.ru", "raiffeisen.ru", "pochtabank.ru", "mkb.ru",
                    "psbank.ru", "rosbank.ru", "mtsbank.ru", "ozon.ru")
    if any(bd == d for bd in bank_domains):
        return 0.92
    # Агрегаторы (высокая, но не первоисточник)
    agg = ("banki.ru", "sravni.ru", "bankiros.ru", "sravni.com")
    if any(a == d for a in agg):
        return 0.7
    if any(a == d for a in ("vc.ru", "forbes.ru", "rbc.ru", "tass.ru", "vedomosti.ru")):
        return 0.6
    # Отзовики/пользовательский контент
    if any(d.endswith(x) for x in ("irecommend.ru", "otzovik.com", "vk.com")):
        return 0.5
    return 0.55


# ════════════════════════════════════════════════════════════════════════
# TOOL: web_search
# ════════════════════════════════════════════════════════════════════════


def tool_web_search(args: dict, bundle) -> str:
    """Поиск в web через multi-backend (SearXNG/Brave/ddgs/ddg/yandex).

    Возвращает список {title, url, snippet, domain}. НЕ скачивает содержимое —
    только метаданные SERP. Для чтения страницы вызови read_url.
    """
    from ....rag.web_search import search as _ws
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query пустой"}, ensure_ascii=False)
    max_results = int(args.get("max_results", 8))
    site_filter = args.get("site_filter")  # ["sberbank.ru", "banki.ru", ...]

    try:
        results = _ws(query, max_results=max_results,
                       site_filter=site_filter) or []
    except Exception as e:
        log.warning("web_search %r failed: %s", query[:60], e)
        return json.dumps({"error": f"search failed: {e}"}, ensure_ascii=False)

    out = []
    for r in results:
        url = r.get("url") or ""
        if not url.startswith("http"):
            continue
        dom = r.get("domain") or _domain(url)
        out.append({
            "title": (r.get("title") or "")[:200],
            "url": url,
            "snippet": (r.get("snippet") or "")[:500],
            "domain": dom,
            "trust": round(_trust_for(dom, url), 2),
        })
    return json.dumps({"query": query, "results": out, "count": len(out)},
                      ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════
# TOOL: read_url — скачать страницу/PDF, вернуть текст + пассивный индекс
# ════════════════════════════════════════════════════════════════════════


def tool_read_url(args: dict, bundle) -> str:
    """Скачать URL (HTML/PDF), распарсить, вернуть релевантный текст.

    Side effect: документ пассивно индексируется в БД → будущие запросы найдут
    его через semantic_search. Также регистрируется в SourceRegistry bundle.

    Возвращает {title, text, domain, source_n, trust}. text укорочен до
    budget_chars для промпта.
    """
    url = (args.get("url") or "").strip()
    if not url:
        return json.dumps({"error": "url пустой"}, ensure_ascii=False)
    query_hint = (args.get("query") or "").strip()  # для релевантной выборки
    # Раньше 6000 симв/страница: индексатор парсил страницу целиком, а модель
    # видела лишь ~1500 токенов (фрагмент по ключевику) — тариф-PDF/длинная
    # страница отзывов теряли основное содержимое. Поднято (контекст модели
    # огромный, страница уже скачана — отдавать больше почти бесплатно).
    budget = int(args.get("budget_chars",
                          int(os.getenv("V2_READ_BUDGET_CHARS", "12000"))))
    bank_slug_hint = args.get("bank_slug")

    dom = _domain(url)
    trust = _trust_for(dom, url)
    kind = _kind_for(dom, url)

    # Пассивная индексация (best-effort, не блокирует ответ)
    text = ""
    title = ""
    try:
        from ..passive_indexer import index_and_get_text
        idx = index_and_get_text(url, bank_slug_hint=bank_slug_hint,
                                  query_hint=query_hint, budget=budget)
        text = idx.get("text", "")
        title = idx.get("title", "")
    except Exception as e:
        log.info("passive index failed for %s: %s — raw fetch", url[:80], e)
        # Fallback: прямой fetch без индексации
        try:
            text = _raw_fetch_text(url, budget)
        except Exception as e2:
            return json.dumps({"error": f"fetch failed: {e2}"},
                              ensure_ascii=False)

    if not text:
        return json.dumps({"error": "пустой текст (404/captcha/SPA)", "url": url},
                          ensure_ascii=False)

    # Регистрируем источник в bundle
    src_n = register_source(bundle, url=url, title=title, domain=dom,
                              trust=trust, kind=kind, excerpt=text[:600])

    return json.dumps({
        "url": url, "title": title, "domain": dom,
        "text": text[:budget], "trust": round(trust, 2),
        "source_n": src_n,
    }, ensure_ascii=False)


def _kind_for(domain: str, url: str) -> str:
    d = domain.lower()
    reg = ("cbr.ru", "pravo.gov.ru", "consultant.ru", "garant.ru", "fas.gov.ru",
           "nalog.gov.ru", "minfin.gov.ru", "kremlin.ru", "government.ru",
           "notariat.ru", "sfr.gov.ru", "mil.ru", "asv.org.ru", "sbp.nspk.ru")
    if any(r == d or d.endswith("." + r) or d.endswith(".gov.ru") for r in reg):
        return "regulatory"
    bank_domains = ("sberbank.ru", "vtb.ru", "alfabank.ru", "tbank.ru", "tinkoff.ru",
                    "sovcombank.ru", "gazprombank.ru", "rshb.ru", "domrfbank.ru",
                    "open.ru", "raiffeisen.ru", "pochtabank.ru", "mkb.ru",
                    "psbank.ru", "rosbank.ru", "mtsbank.ru")
    if any(bd == d for bd in bank_domains):
        return "bank_official"
    if any(a == d for a in ("banki.ru", "sravni.ru", "bankiros.ru")):
        return "aggregator"
    if any(d.endswith(x) for x in ("irecommend.ru", "otzovik.com")):
        return "review"
    if any(a == d for a in ("vc.ru", "forbes.ru", "rbc.ru", "tass.ru")):
        return "news"
    return "web"


def _raw_fetch_text(url: str, budget: int) -> str:
    """Простой fallback-fetch без индексации (когда indexer не справился)."""
    from ....rag import fetcher
    from ..passive_indexer import _should_render
    fr = fetcher.fetch(url, prefer_browser=_should_render(url))
    if not fr.content:
        return ""
    from ....rag.parsers import parse_auto
    parsed = parse_auto(fr.content, url=fr.final_url, content_type=fr.content_type)
    text = parsed.text or ""
    return text[:budget]


# ════════════════════════════════════════════════════════════════════════
# TOOL: semantic_search — pgvector по уже проиндексированному (кэш БД)
# ════════════════════════════════════════════════════════════════════════


def tool_semantic_search(args: dict, bundle) -> str:
    """Семантический поиск по уже проиндексированным документам в БД.

    Быстро и бесплатно. Используй ПЕРВЫМ — если данные уже есть, не надо
    лезть в web. Если результатов мало (<3) → web_search/read_url.
    """
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query пустой"}, ensure_ascii=False)
    bank_slugs = args.get("bank_slugs")
    if isinstance(bank_slugs, str):
        bank_slugs = [bank_slugs]
    doc_types = args.get("doc_types")
    trust_min = float(args.get("trust_min", 0.5))
    top_k = int(args.get("top_k", 6))

    try:
        from ....rag.retriever import semantic_search as _ss
        results = _ss(query, top_k=top_k, bank_slugs=bank_slugs,
                       doc_types=doc_types, trust_min=trust_min,
                       exclude_sponsored=True)
    except Exception as e:
        return json.dumps({"error": f"semantic_search failed: {e}"},
                          ensure_ascii=False)

    out = []
    for r in results:
        url = r.get("url") or ""
        dom = _domain(url)
        # Регистрируем источник
        src_n = register_source(bundle, url=url,
                                  title=r.get("title", "") or url[:80],
                                  domain=dom,
                                  trust=float(r.get("trust_score") or 0.6),
                                  kind=_kind_for(dom, url),
                                  excerpt=(r.get("text") or "")[:600])
        out.append({
            "text": (r.get("text") or "")[:1500],
            "headings_path": r.get("headings_path"),
            "bank_slug": r.get("bank_slug"),
            "url": url,
            "source_n": src_n,
            "trust": round(float(r.get("trust_score") or 0.6), 2),
            "relevance": round(r.get("relevance", 0), 3),
        })
    return json.dumps({"query": query, "results": out, "count": len(out)},
                      ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════
# TOOL: run_sql — read-only SQL по предзаданным view/таблицам
# ════════════════════════════════════════════════════════════════════════


def tool_run_sql(args: dict, bundle) -> str:
    """Read-only SELECT по предзаданным представлениям/таблицам.

    Доступно: v_offer_current, v_sber_vs_market, v_review_topics,
    v_review_sentiment_share, v_bank_coverage, bank, review, review_topic,
    review_sentiment, product_offer, product_terms, quality_flag,
    change_history.

    Запрещено: всё кроме SELECT/WITH. LIMIT обязателен.
    """
    from ....ai.analyst import _run_sql_safe
    sql = (args.get("sql") or "").strip()
    if not sql:
        return json.dumps({"error": "sql пустой"}, ensure_ascii=False)
    return _run_sql_safe(sql)
