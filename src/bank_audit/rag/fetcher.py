"""Smart fetcher: HTTP → Playwright → captcha auto-solver fallback chain.

Стратегия для каждого URL:
  1. Проверяем кэш (rag_cache namespace='fetch', TTL 6h по умолчанию)
  2. HTTP-fetch через httpx (быстро, дёшево)
     • Используем браузерные headers + http2=False + http/1.1
     • Если 200 + non-empty + нет captcha-маркеров → success
  3. Если HTTP не дал контент → Playwright fallback с OPENCLAW-профилем
     • Используется persistent context с warm cookies
     • Stealth-патчи активированы
  4. Если Playwright поймал капчу:
     • Сначала проверяем env TWOCAPTCHA_KEY → авто-решаем
     • Иначе пишем в captcha_pending → юзер решает раз в день
     • После решения cookies валидны 24h → следующие запросы пройдут через HTTP

Кэширует raw контент + content-type для индексирования.
"""
from __future__ import annotations
import hashlib, logging, os, time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import cache as rag_cache
from .trust import detect_invalid_content, detect_sponsored

log = logging.getLogger(__name__)

# CA bundle: системный + Russian Trusted Root CA (Минцифра РФ).
# Sberbank.ru, gosuslugi.ru и часть др. российских сайтов отдают сертификаты,
# подписанные русским гос. CA. Без него httpx/Playwright получают TLS handshake
# error → страницы возвращают заглушку «установите сертификат Минцифры».
# Bundle создаётся через `openssl x509 ... -inform DER + cat certifi.cacert + ru.pem`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_DEFAULT_CA_BUNDLE = os.path.join(_REPO_ROOT, "config", "ca_bundle_combined.pem")
CA_BUNDLE_PATH = os.getenv("CA_BUNDLE_PATH",
                            _DEFAULT_CA_BUNDLE
                            if os.path.exists(_DEFAULT_CA_BUNDLE) else None)
if CA_BUNDLE_PATH:
    log.info("fetcher: using custom CA bundle %s", CA_BUNDLE_PATH)
else:
    log.warning("fetcher: no CA bundle found at %s — Sberbank.ru may fail",
                _DEFAULT_CA_BUNDLE)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Минимальный размер валидного HTML — фильтр challenge-страниц
_MIN_VALID_BYTES = 2000


@dataclass
class FetchResult:
    url:           str
    final_url:     str                       # после redirects
    status:        int
    content:       bytes
    content_type:  str | None
    via:           str                       # 'cache' | 'http' | 'playwright' | 'playwright_solved'
    captcha:       bool = False              # True если требовалась решённая капча


def fetch(url: str, *, prefer_browser: bool = False,
          cache_ttl_seconds: int = 6 * 3600,
          force_refresh: bool = False,
          browser=None) -> FetchResult:
    """Главная точка входа. Возвращает FetchResult.

    prefer_browser=True — сразу через Playwright (для SPA вроде sravni.ru/banki.ru,
                          где HTTP отдаст пустой каркас)
    force_refresh — игнорировать кэш
    browser — опциональный BrowserCollector instance (если уже есть)
    """
    # 1. Cache lookup
    if not force_refresh:
        cached = rag_cache.get("fetch", url, prefer_browser)
        if cached:
            log.info("fetch %s: cache hit", url[:80])
            return FetchResult(
                url=url, final_url=cached.get("final_url", url),
                status=cached.get("status", 200),
                content=bytes.fromhex(cached["content_hex"]),
                content_type=cached.get("content_type"),
                via="cache",
            )

    # 2. Pure HTTP (если не prefer_browser)
    if not prefer_browser:
        result = _fetch_http(url)
        if result and _looks_valid(result.content, result.content_type):
            _cache_result(url, prefer_browser, result, cache_ttl_seconds)
            return result
        log.info("fetch %s: HTTP didn't yield valid content (%s bytes), trying browser",
                 url[:80], len(result.content) if result else 0)

    # 3. Playwright fallback (или primary path)
    result = _fetch_browser(url, browser=browser)
    if result and _looks_valid(result.content, result.content_type):
        _cache_result(url, prefer_browser, result, cache_ttl_seconds)
        return result

    # 4. Возвращаем что есть (может быть captcha=True)
    #    (2captcha-авторешение удалено: требовало Playwright + платный ключ,
    #     на сервере без браузера не работало)
    return result or FetchResult(url=url, final_url=url, status=0,
                                  content=b"", content_type=None, via="failed")


def _looks_valid(content: bytes, content_type: str | None) -> bool:
    """Эвристика: контент годен для индексирования."""
    if not content or len(content) < _MIN_VALID_BYTES:
        # Маленькие файлы могут быть валидны (PDF metadata, JSON-API), не отбрасываем
        if content_type and ("pdf" in content_type or "json" in content_type
                              or "spreadsheet" in content_type or "presentation" in content_type):
            return len(content) > 100
        if not content:
            return False
        # маленький HTML — скорее всего challenge
    text_preview = content[:8192].decode("utf-8", errors="ignore")
    invalid, _ = detect_invalid_content(text_preview)
    return not invalid


def _fetch_http(url: str) -> FetchResult | None:
    try:
        # CA bundle: подключаем русский корневой CA для sberbank.ru и др.
        verify_arg = CA_BUNDLE_PATH if CA_BUNDLE_PATH else True
        with httpx.Client(http2=False, headers=DEFAULT_HEADERS,
                          follow_redirects=True,
                          verify=verify_arg,
                          timeout=httpx.Timeout(connect=10, read=30,
                                                write=10, pool=10)) as client:
            resp = client.get(url)
        return FetchResult(
            url=url, final_url=str(resp.url), status=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("content-type"),
            via="http",
        )
    except Exception as e:
        log.info("HTTP fetch %s failed: %s", url[:80], type(e).__name__)
        return None


def _fetch_browser(url: str, browser=None) -> FetchResult | None:
    """Через BrowserCollector (Playwright). Возвращает FetchResult или None.
    Если browser не передан — создаём временный.

    ВАЖНО (перф deep-research): временный браузер для read_url создаётся с
    КОРОТКИМ nav-таймаутом (FETCH_BROWSER_NAV_S, дефолт 22с вместо скрапинговых
    45с). read_url — чтение текста ОДНОЙ страницы, а не выкачивание листинга;
    при широком отчёте десятки медленных браузерных чтений по 60с+ складывались
    в десятки минут. Меньше nav-таймаут → зависший сайт отваливается быстрее."""
    from ..collectors.browser import BrowserCollector, CaptchaRequired
    own_browser = browser
    if own_browser is None:
        own_browser = BrowserCollector(
            nav_timeout_s=float(os.getenv("FETCH_BROWSER_NAV_S", "22")))

    try:
        status, content = own_browser.fetch_html(url)
        return FetchResult(
            url=url, final_url=url, status=status,
            content=content, content_type="text/html",
            via="playwright",
        )
    except CaptchaRequired:
        log.info("browser fetch %s: CaptchaRequired", url[:80])
        return FetchResult(url=url, final_url=url, status=0,
                           content=b"", content_type=None,
                           via="playwright", captcha=True)
    except Exception as e:
        log.info("browser fetch %s failed: %s", url[:80], type(e).__name__)
        return None


def _cache_result(url: str, prefer_browser: bool,
                  result: FetchResult, ttl: int) -> None:
    if not result.content or len(result.content) > 2_000_000:
        # Слишком большой документ не кэшируем (PDF может быть >2MB)
        return
    rag_cache.put(
        "fetch",
        {
            "final_url": result.final_url,
            "status": result.status,
            "content_hex": result.content.hex(),
            "content_type": result.content_type,
        },
        ttl,
        url, prefer_browser,
    )
