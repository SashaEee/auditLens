"""Web search — multi-backend chain с fallback'ами.

Цепочка (в порядке приоритета):
  1. SearXNG (self-hosted, безлимитный) — env SEARXNG_URL
  2. Brave Search API (2k/мес free)    — env BRAVE_SEARCH_API_KEY
  3. DuckDuckGo HTML SERP               — нет ключа, но банят
  4. Yandex HTML SERP                   — нет ключа, тоже банят

Каждый backend возвращает [{title, url, snippet, domain}, ...].
search() пробует backend'ы по порядку: первый давший непустой результат — используется.

Кэш на 1 час по (query, site_filter, region).
"""
from __future__ import annotations
import logging, os, re, time
from typing import Iterable
from urllib.parse import quote_plus, urlparse, parse_qs

import httpx
from selectolax.parser import HTMLParser

from . import cache as rag_cache
from .trust import KNOWN_BANK_DOMAINS

log = logging.getLogger(__name__)

DDG_HTML = "https://html.duckduckgo.com/html/"

# Backend configuration — читаем env при каждом вызове, а не при import,
# чтобы dotenv успел подхватить .env (он грузится в config.py).
def _searxng_url() -> str | None:
    return os.getenv("SEARXNG_URL") or None
def _brave_key() -> str | None:
    return os.getenv("BRAVE_SEARCH_API_KEY") or None
BRAVE_API_ENDPOINT   = "https://api.search.brave.com/res/v1/web/search"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}


def _decode_ddg_url(href: str) -> str:
    """DDG обёртывает URL в /l/?uddg=ENCODED — раскодируем."""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or "/l/?uddg=" in href:
        try:
            qs = parse_qs(urlparse(href).query)
            uddg = qs.get("uddg")
            if uddg:
                from urllib.parse import unquote
                return unquote(uddg[0])
        except Exception:
            pass
    return href


def _post_filter_by_sites(results: list[dict],
                            site_filter: list[str] | None) -> list[dict]:
    """Фильтрует results по whitelist доменов (с поддоменными совпадениями)."""
    if not site_filter or not results:
        return results
    sf = set(site_filter)
    return [r for r in results
            if any(d == r["domain"] or r["domain"].endswith("." + d) for d in sf)]


def search(
    query: str,
    *,
    max_results: int = 8,
    site_filter: list[str] | None = None,    # ['cbr.ru', 'sberbank.ru'] — узкий список
    region: str = "ru-ru",
    cache_ttl_seconds: int = 3600,
) -> list[dict]:
    """Multi-backend web search:
      1. SearXNG (если SEARXNG_URL задан) — приоритет, безлимит
      2. Brave Search API (если BRAVE_SEARCH_API_KEY задан) — 2k/мес free
      3. DuckDuckGo HTML
      4. Yandex HTML
    Первый непустой результат используется. Возвращает [{title, url, snippet, domain}].
    """
    if not query or not query.strip():
        return []

    cache_key = ("web_search", query, tuple(sorted(site_filter or [])), region)
    cached = rag_cache.get("web_search", *cache_key[1:])
    if cached:
        return cached[:max_results]

    backends = []
    if _searxng_url():
        backends.append(("searxng", _search_searxng))
    if _brave_key():
        backends.append(("brave", _search_brave))
    backends.append(("ddg", _search_ddg))
    backends.append(("yandex", _search_yandex))

    results: list[dict] = []
    for name, fn in backends:
        try:
            r = fn(query, max_results=max_results,
                   site_filter=site_filter, region=region)
        except TypeError:
            # backend не принимает region (yandex)
            try:
                r = fn(query, max_results=max_results, site_filter=site_filter)
            except Exception as e:
                log.info("%s search failed: %s", name, type(e).__name__)
                r = []
        except Exception as e:
            log.info("%s search failed: %s", name, type(e).__name__)
            r = []
        # Post-filter если backend не обработал site_filter сам
        if site_filter:
            r = _post_filter_by_sites(r, site_filter)
        if r:
            log.warning("[web_search] backend=%s q=%s → %d",
                     name, query[:50], len(r))
            results = r
            break

    if results:
        rag_cache.put("web_search", results, cache_ttl_seconds, *cache_key[1:])
    return results[:max_results]


# ── Backend 1: SearXNG ────────────────────────────────────────────────────
def _search_searxng(query: str, *, max_results: int = 8,
                     site_filter: list[str] | None = None,
                     region: str = "ru-ru") -> list[dict]:
    """SearXNG self-hosted JSON API. Агрегирует Google/Bing/Brave/Qwant/Yandex.
    SEARXNG_URL должен указывать на инстанс с включённым JSON output:
      `formats: [html, json]` в settings.yml."""
    base = _searxng_url()
    if not base:
        return []
    full_query = query
    if site_filter:
        sites = " OR ".join(f"site:{d}" for d in site_filter[:25])
        full_query = f"({query}) ({sites})"
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5, read=20,
                                                  write=5, pool=5)) as c:
            resp = c.get(f"{base.rstrip('/')}/search",
                         params={"q": full_query, "format": "json",
                                 "language": "ru", "safesearch": "0"})
        if resp.status_code != 200:
            log.warning("searxng %s: HTTP %s", query[:50], resp.status_code)
            return []
        data = resp.json()
    except Exception as e:
        log.info("searxng %s: %s", query[:50], type(e).__name__)
        return []

    out: list[dict] = []
    for r in (data.get("results") or [])[:max_results * 2]:
        url = r.get("url") or ""
        if not url.startswith("http"):
            continue
        try:
            domain = (urlparse(url).hostname or "").replace("www.", "")
        except Exception:
            domain = ""
        out.append({
            "title":   (r.get("title") or "")[:200],
            "url":     url,
            "snippet": (r.get("content") or "")[:400],
            "domain":  domain,
        })
    return out[:max_results]


# ── Backend 2: Brave Search API ──────────────────────────────────────────
def _search_brave(query: str, *, max_results: int = 8,
                   site_filter: list[str] | None = None,
                   region: str = "ru-ru") -> list[dict]:
    """Brave Search API. Бесплатный тариф 2k/мес.
    Регистрация: https://api.search.brave.com/app/keys"""
    api_key = _brave_key()
    if not api_key:
        return []
    full_query = query
    if site_filter:
        sites = " OR ".join(f"site:{d}" for d in site_filter[:10])
        full_query = f"({query}) ({sites})"
    # Brave region codes: 'ru-RU', 'us-EN', etc
    brave_country = region.split("-")[0].upper() if region else "RU"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=5, read=15,
                                                  write=5, pool=5)) as c:
            resp = c.get(BRAVE_API_ENDPOINT,
                         headers=headers,
                         params={"q": full_query, "country": brave_country,
                                 "search_lang": "ru",
                                 "count": min(max_results * 2, 20)})
        if resp.status_code == 429:
            log.warning("brave search rate-limited")
            return []
        if resp.status_code != 200:
            log.warning("brave %s: HTTP %s", query[:50], resp.status_code)
            return []
        data = resp.json()
    except Exception as e:
        log.info("brave %s: %s", query[:50], type(e).__name__)
        return []

    out: list[dict] = []
    web = data.get("web") or {}
    for r in (web.get("results") or [])[:max_results * 2]:
        url = r.get("url") or ""
        if not url.startswith("http"):
            continue
        try:
            domain = (urlparse(url).hostname or "").replace("www.", "")
        except Exception:
            domain = ""
        out.append({
            "title":   (r.get("title") or "")[:200],
            "url":     url,
            "snippet": (r.get("description") or "")[:400],
            "domain":  domain,
        })
    return out[:max_results]


def _search_ddg(query: str, *, max_results: int, site_filter: list[str] | None,
                region: str) -> list[dict]:
    """DuckDuckGo HTML SERP."""

    full_query = query
    if site_filter:
        # Несколько сайтов через site:X OR site:Y. Лимит чтобы query не был слишком длинный
        top_sites = site_filter[:25]
        sites = " OR ".join(f"site:{d}" for d in top_sites)
        full_query = f"({full_query}) ({sites})"

    try:
        with httpx.Client(headers=_HEADERS, follow_redirects=True,
                          timeout=httpx.Timeout(connect=8, read=18, write=8, pool=8)) as c:
            resp = c.post(DDG_HTML, data={"q": full_query, "kl": region})
        if resp.status_code != 200:
            log.warning("ddg search %s: HTTP %s", query[:60], resp.status_code)
            return []
    except Exception as e:
        log.warning("ddg search %s: %s", query[:60], type(e).__name__)
        return []

    tree = HTMLParser(resp.text)
    results = []
    seen_urls = set()
    # Структура: <div class="result"> ...
    for r in tree.css(".result, .web-result"):
        a = r.css_first("a.result__a, .result__title a")
        if not a:
            continue
        href = a.attributes.get("href") or ""
        href = _decode_ddg_url(href)
        if not href.startswith("http"):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        title = (a.text() or "").strip()
        snippet_node = r.css_first(".result__snippet")
        snippet = (snippet_node.text() or "").strip() if snippet_node else ""
        domain = ""
        try:
            domain = urlparse(href).hostname or ""
            domain = domain.replace("www.", "")
        except Exception:
            pass
        results.append({
            "title":   title[:200],
            "url":     href,
            "snippet": snippet[:400],
            "domain":  domain,
        })
        if len(results) >= max_results * 2:           # запас перед фильтрами
            break

    # Если есть site_filter — DDG в теории уже отфильтровал, но проверим
    if site_filter:
        sf = set(site_filter)
        results = [r for r in results
                   if any(s == r["domain"] or r["domain"].endswith("." + s) for s in sf)]

    return results[:max_results]


# Известные slug'и не-банков для entity discovery
KNOWN_SERVICES_DOMAINS = {
    "domclick":  "domclick.ru",
    "домклик":   "domclick.ru",
    "cian":      "cian.ru",
    "циан":      "cian.ru",
    "avito":     "avito.ru",
    "авито":     "avito.ru",
    "domrf":     "дом.рф",
    "дом.рф":    "дом.рф",
    "дом рф":    "дом.рф",
}


def _search_yandex(query: str, *, max_results: int = 8,
                    site_filter: list[str] | None = None,
                    region: str = "ru-ru") -> list[dict]:
    """Yandex SERP HTML scraping fallback. Менее надёжен (могут банить),
    но иногда даёт лучшую RU-релевантность чем DDG."""
    full_query = query
    if site_filter:
        sites = " | ".join(f"site:{d}" for d in site_filter[:15])
        full_query = f"{query} ({sites})"

    try:
        with httpx.Client(headers=_HEADERS, follow_redirects=True,
                          timeout=httpx.Timeout(connect=8, read=18, write=8, pool=8)) as c:
            # Yandex search XML-like serp HTML version
            resp = c.get("https://yandex.ru/search/",
                         params={"text": full_query, "lr": 213})
        if resp.status_code != 200:
            return []
    except Exception as e:
        log.info("yandex search fallback failed: %s", type(e).__name__)
        return []

    tree = HTMLParser(resp.text)
    results: list[dict] = []
    seen = set()
    # Yandex SERP структура: .OrganicTitle a + .OrganicTextContentSpan для snippet
    for a in tree.css(".OrganicTitle a, h2 a, a.Link.OrganicTitle-Link"):
        href = a.attributes.get("href") or ""
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        title = (a.text() or "").strip()
        if not title or len(title) < 5:
            continue
        try:
            domain = (urlparse(href).hostname or "").replace("www.", "")
        except Exception:
            domain = ""
        results.append({"title": title[:200], "url": href, "snippet": "", "domain": domain})
        if len(results) >= max_results * 2:
            break

    if site_filter:
        sf = set(site_filter)
        results = [r for r in results
                   if any(d == r["domain"] or r["domain"].endswith("." + d) for d in sf)]
    return results[:max_results]


# ── Direct URL templates ─────────────────────────────────────────────────
# Когда DDG/Yandex банят — используем прямые URL'ы. Эти страницы стабильны
# у топ-банков, на них почти всегда есть тарифы/правила/документы.
BANK_PRODUCT_URL_TEMPLATES: dict[str, list[str]] = {
    # Generic templates — пробуем для ЛЮБОГО банка, paths общие у большинства
    "_generic": [
        "https://www.{domain}/tariffs/",
        "https://www.{domain}/tarify/",
        "https://www.{domain}/documents/",
        "https://www.{domain}/dokumenty/",
        "https://www.{domain}/legal/",
        "https://www.{domain}/usloviya/",
        "https://www.{domain}/conditions/",
    ],
    # Bank-specific URL hints (наиболее частые landing pages)
    "sberbank.ru": [
        "https://www.sberbank.ru/ru/legal/about_pristavu/perevod_dengi/dover",
        "https://www.sberbank.ru/ru/person/contributions/dover_documents",
        "https://www.sberbank.ru/ru/legal",
    ],
    "vtb.ru": [
        "https://www.vtb.ru/legal/",
    ],
    "alfabank.ru": [
        "https://alfabank.ru/help/",
        "https://alfabank.ru/get-money/credit-cards/tariffs/",
    ],
    "tinkoff.ru": [
        "https://www.tinkoff.ru/about/documents/",
        "https://www.tbank.ru/about/documents/",
    ],
    "tbank.ru": [
        "https://www.tbank.ru/about/documents/",
    ],
    "sovcombank.ru": [
        "https://sovcombank.ru/about/documents",
        "https://sovcombank.ru/individual/credit-cards/halva/dokumenty",
    ],
    "gazprombank.ru": [
        "https://www.gazprombank.ru/about/disclosure/",
        "https://www.gazprombank.ru/personal/everyday/documents/",
    ],
    "rshb.ru": [
        "https://www.rshb.ru/legal/",
    ],
    "domrf.ru": [
        "https://domrfbank.ru/about/documents/",
    ],
}


# ── Audience-specific landing pages ─────────────────────────────────────
# Когда у audience есть собственный продуктовый раздел на сайте банка
# (карта ветерана СВО / военнослужащих / пенсионеров) — даём прямые URL'ы.
# Ключ — кортеж из триггеров (audience_filter or synonym lower-substring).
# Значение — список URL-шаблонов на конкретный продукт. Перебирается ВСЕМ
# ban kом если хотя бы один триггер встречается в audience_filter ИЛИ в
# topic / topic_synonyms. URL может вернуть 404 — тогда fetcher просто
# отбросит, ingest продолжится со следующего.
AUDIENCE_URL_TEMPLATES: dict[str, dict[str, list[str]]] = {
    # Карта участника СВО / ветерана / военнослужащего
    "veteran_svo": {
        "_triggers": ["сво", "ветеран", "военнослуж", "участник", "защитник",
                       "спецоперац", "льготн"],
        "sberbank.ru": [
            "https://www.sberbank.ru/ru/person/cards/debit/sbercard_veteran",
            "https://www.sberbank.com/ru/person/promo/sbercard_veteran",
            "https://www.sberbank.ru/ru/person/special/veterans",
            "https://www.sberbank.ru/ru/person/special/uchastnikam-svo",
        ],
        "vtb.ru": [
            "https://www.vtb.ru/personal/karty/karta-zaschitnika-otechestva/",
            "https://www.vtb.ru/personal/karty/debet/karta-veterana/",
            "https://www.vtb.ru/o-banke/uchastnikam-svo/",
        ],
        "psbank.ru": [
            "https://www.psbank.ru/personal/cards/voennaya",
            "https://www.psbank.ru/personal/cards/military",
            "https://www.psbank.ru/personal/Cards/Voennaya",
            "https://www.psbank.ru/svo",
            "https://www.psbank.ru/personal/special/svo",
        ],
        "gazprombank.ru": [
            "https://www.gazprombank.ru/personal/cards/karta-veterana/",
            "https://www.gazprombank.ru/personal/cards/veteran/",
            "https://www.gazprombank.ru/personal/special/veteranam-svo/",
            "https://www.gazprombank.ru/personal/cards/voennoslugaschim/",
        ],
    },
}


def _matches_audience(audience_key: str, topic: str | None,
                       synonyms: list[str] | None,
                       audience_filter: str | None) -> bool:
    """True если хотя бы один триггер этой audience встречается в инпутах."""
    triggers = AUDIENCE_URL_TEMPLATES.get(audience_key, {}).get("_triggers", [])
    if not triggers:
        return False
    haystack_parts = [topic or "", audience_filter or ""]
    haystack_parts.extend(synonyms or [])
    haystack = " ".join(p.lower() for p in haystack_parts)
    return any(t in haystack for t in triggers)


def get_direct_product_urls(domain: str, topic: str,
                             synonyms: list[str] | None = None,
                             audience_filter: str | None = None,
                             product_url_paths: list[str] | None = None,
                             bank_slug: str | None = None,
                             bank_specific_paths: dict[str, list[str]] | None = None,
                             ) -> list[dict]:
    """Возвращает прямые URL'ы для ingest когда DDG/Yandex недоступны.

    Порядок (важен — fetcher берёт первые N URL'ов):
      1. bank_specific_paths[slug] — exception-карта от resolver-LLM
         (например {"sberbank": ["domclick.ru/ipoteka"]})
      2. AUDIENCE_URL_TEMPLATES — хардкод-фолбек для известных audience
         (карта ветерана СВО) — ОСТАЁТСЯ для надёжности
      3. resolver.product_url_paths — LLM-discovered paths под этот продукт
         (universal, работает для ЛЮБОГО topic'а — вклад/ипотека/эквайринг/...)
      4. BANK_PRODUCT_URL_TEMPLATES — hardcoded bank-specific landing pages
      5. _generic templates — universal fallback (/tariffs/, /documents/, ...)

    Все параметры опциональны (zero-breaking change). Без них работает как раньше.
    """
    out: list[dict] = []
    seen = set()

    def _add(url: str, title_tag: str):
        if url and url not in seen:
            out.append({"url": url, "title": f"{domain} ({title_tag})",
                        "snippet": topic, "domain": domain})
            seen.add(url)

    # 1. Bank-specific exceptions (resolver-LLM знает «у Сбера ипотека на domclick»)
    if bank_slug and bank_specific_paths:
        for p in (bank_specific_paths.get(bank_slug) or []):
            if p.startswith("http"):
                _add(p, "direct bank-specific")
                continue
            stripped = p.lstrip("/")
            first_seg = stripped.split("/")[0]
            # Если path начинается с домена (содержит «.»), это другой сайт банка
            # (sberbank → domclick.ru/ipoteka). Префиксить доменом банка нельзя.
            if "." in first_seg:
                _add(f"https://{stripped}", "direct bank-specific")
            else:
                _add(f"https://{domain}/{stripped}", "direct bank-specific")

    # 2. Hardcoded audience templates (надёжный фолбек для известных кейсов)
    for aud_key in AUDIENCE_URL_TEMPLATES:
        if not _matches_audience(aud_key, topic, synonyms, audience_filter):
            continue
        for u in AUDIENCE_URL_TEMPLATES[aud_key].get(domain, []):
            _add(u, f"direct audience:{aud_key}")

    # 3. LLM-discovered product_url_paths (universal — главный универсальный путь)
    bare_domain = domain[4:] if domain.startswith("www.") else domain
    for p in (product_url_paths or []):
        if p.startswith("http"):
            _add(p, "direct llm-path")
        else:
            path = "/" + p.lstrip("/")
            # Пробуем без www. и с www. — банки бывают по-разному настроены
            _add(f"https://{bare_domain}{path}", "direct llm-path")
            _add(f"https://www.{bare_domain}{path}", "direct llm-path-www")

    # 4. Hardcoded bank-specific
    for u in BANK_PRODUCT_URL_TEMPLATES.get(domain, []):
        _add(u, "direct")

    # 5. Generic templates
    for tpl in BANK_PRODUCT_URL_TEMPLATES["_generic"]:
        _add(tpl.format(domain=domain), "direct generic")
    return out


def get_direct_review_urls(bank_slug: str, topic: str) -> list[dict]:
    """Прямые URL'ы агрегаторов отзывов: banki.ru + sravni.ru.

    Раньше использовали только banki.ru. Sravni.ru добавлен как второй
    независимый источник — у него тоже есть JSON-LD Review разметка, наш
    parser её извлекает. Это даёт ~2× больше отзывов для тематического
    фильтра в get_topical_reviews. Sravni-slug мапим через sravni-overrides
    из config (для нестандартных slug'ов вроде «promsvyazbank» вместо «psb»).
    """
    if not bank_slug:
        return []
    # Sravni-slug overrides: где наш slug !=  sravni-slug
    sravni_overrides = {
        "tinkoff": "tinkoff", "alfabank": "alfa-bank", "sovcombank": "sovkombank",
        "rshb": "rosselhozbank", "psb": "promsvyazbank", "pochtabank": "pochta-bank",
        "raiffeisen": "raiffeisenbank", "rsb": "russkiystandart",
        "ozonbank": "ozon-bank", "homecredit": "home-credit-bank",
        "rencredit": "renessans-credit", "uralsib": "uralsib",
        "akbars": "ak-bars", "mtsbank": "mts-bank",
        "yandexbank": "yandex-bank", "domrf": "dom-rf",
    }
    sravni_slug = sravni_overrides.get(bank_slug, bank_slug)
    return [
        {"url": f"https://www.banki.ru/services/responses/bank/{bank_slug}/?type=all",
         "title": f"banki.ru отзывы: {bank_slug}",
         "snippet": "", "domain": "banki.ru"},
        {"url": f"https://www.banki.ru/services/responses/bank/{bank_slug}/?type=otz",
         "title": f"banki.ru отзывы (с оценкой): {bank_slug}",
         "snippet": "", "domain": "banki.ru"},
        {"url": f"https://www.sravni.ru/bank/{sravni_slug}/otzyvy/",
         "title": f"sravni.ru отзывы: {sravni_slug}",
         "snippet": "", "domain": "sravni.ru"},
    ]


def search_pdfs_on_domain(domain: str, topic: str,
                           *, max_results: int = 4) -> list[dict]:
    """Ищет PDF/тарифные документы на домене конкретного банка.
    Используется для product-вопросов (доверенности, тарифы, условия).

    Делает 3 параллельные DDG-запроса:
      • site:{domain} {topic} filetype:pdf
      • site:{domain} условия {topic}
      • site:{domain} тарифы {topic}

    Возвращает агрегированный top-N результатов с приоритетом PDF.
    """
    if not domain or not topic:
        return []
    queries = [
        f'site:{domain} "{topic}" filetype:pdf',
        f'site:{domain} условия {topic}',
        f'site:{domain} тарифы {topic}',
        f'site:{domain} {topic} документ',
    ]
    seen_urls: set[str] = set()
    out: list[dict] = []
    for q in queries:
        try:
            rs = search(q, max_results=max_results, region="ru-ru",
                       cache_ttl_seconds=1800)
        except Exception as e:
            log.info("pdf search %s: %s", q[:60], type(e).__name__)
            rs = []
        for r in rs:
            u = r.get("url") or ""
            if not u or u in seen_urls:
                continue
            # Приоритет — PDF и страницы с «тариф/условия/правила»
            ul = u.lower()
            score = 0
            if ul.endswith(".pdf"): score += 10
            for kw in ("/tariff", "/тариф", "/условия", "/uslovia",
                       "/document", "/dokument", "/rules", "/правила"):
                if kw in ul: score += 3
            r2 = dict(r)
            r2["_score"] = score
            out.append(r2)
            seen_urls.add(u)
    out.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return out[:max_results * 2]


# Whitelist отзывных площадок — для тематического поиска отзывов
REVIEW_AGGREGATOR_DOMAINS = (
    "banki.ru",
    "sravni.ru",
    "otzovik.com",
    "irecommend.ru",
    "vc.ru",
    "pikabu.ru",
)


def search_topical_reviews(topic: str, bank_name: str,
                            *, max_results: int = 6) -> list[dict]:
    """Ищет отзывы по конкретной услуге у конкретного банка на агрегаторах.

    Используется когда planner детектит «продуктовый» вопрос с триггерами
    «плюсы/минусы/отзывы». Не заменяет get_review_themes (общая статистика
    из БД), а дополняет — даёт ТЕМАТИЧЕСКИЕ отзывы по конкретной услуге.

    Возвращает [{title, url, snippet, domain}, ...]
    """
    if not topic or not bank_name:
        return []
    queries = [
        f'отзывы {topic} {bank_name}',
        f'{topic} {bank_name} жалобы',
        f'{bank_name} {topic} опыт клиентов',
    ]
    seen_urls: set[str] = set()
    out: list[dict] = []
    for q in queries:
        try:
            rs = search(q, max_results=max_results,
                        site_filter=list(REVIEW_AGGREGATOR_DOMAINS),
                        region="ru-ru", cache_ttl_seconds=1800)
        except Exception as e:
            log.info("topical_reviews %s: %s", q[:60], type(e).__name__)
            rs = []
        for r in rs:
            u = r.get("url") or ""
            if not u or u in seen_urls:
                continue
            out.append(r)
            seen_urls.add(u)
        if len(out) >= max_results * 2:
            break
    return out[:max_results * 2]


# Триггеры «продуктовый/процедурный вопрос» — для активации deep-pdf+reviews
PRODUCT_TOPIC_TRIGGERS = {
    # каждый ключ = topic-keyword для поиска, значения = триггеры в вопросе
    "доверенность":  ["доверенност"],
    "тариф":         ["тариф", "комисси", "стоимост"],
    "перевод":       ["перевод"],
    "ипотека":       ["ипотек", "жилищн"],
    "кредит":        ["кредит наличн", "потребкредит", "потребительск"],
    "вклад":         ["вклад", "депозит"],
    "карта":         ["дебетов", "кредитн карт", "карта", "карт ", " карты"],
    "счёт":          ["расчётн счёт", "открытие счёта", "р/с"],
    "сбп":           ["сбп", "система быстрых платежей"],
    "эквайринг":     ["эквайринг", "терминал"],
    "автокредит":    ["автокредит"],
    "рефинансирование":["рефинансир"],
    "счёт эскроу":   ["эскроу"],
    "брокер":        ["брокер", "иис"],
}


def detect_product_topic(question: str) -> str | None:
    """Возвращает topic-keyword для product-search или None.
    Берёт ПЕРВЫЙ совпадающий триггер."""
    if not question:
        return None
    low = question.lower()
    for topic, triggers in PRODUCT_TOPIC_TRIGGERS.items():
        if any(t in low for t in triggers):
            return topic
    return None


def detect_entities(question: str) -> list[dict]:
    """Извлекает упомянутые в вопросе сущности (банки и сервисы) по словарю.
    Возвращает [{slug, name, domain}, ...]"""
    if not question:
        return []
    low = question.lower()
    found: dict[str, dict] = {}
    # Банки из KNOWN_BANK_DOMAINS
    for domain, slug in KNOWN_BANK_DOMAINS.items():
        # Если упоминание slug'а или фрагмента domain'а — добавляем
        d_short = domain.split(".")[0]
        if slug in low or d_short in low:
            found.setdefault(slug, {"slug": slug, "name": slug, "domain": domain})
    # Сервисы (Домклик / ЦИАН / Авито / ДОМ.РФ)
    for trigger, domain in KNOWN_SERVICES_DOMAINS.items():
        if trigger in low:
            slug = domain.replace(".ru", "").replace("дом.рф", "domrf")
            found.setdefault(slug, {"slug": slug, "name": trigger.title(), "domain": domain})
    return list(found.values())
