"""Корпоративный шлюз поиска: Яндекс как основной поисковик и сохранённые копии страниц.

Почему Яндекс основным. Замер 24.09.2026 на 87 реальных подзапросах инструмента
(слепая оценка выдачи): по релевантности он не хуже лучшего из доступных
вариантов, но отвечает за 0,6 с против 1,4 с у шлюза на резидентских прокси,
не отдаёт пустых выдач, у 99% результатов есть дата, а запрос уходит в
российский поисковик через корпоративный контур, а не через сторонние прокси.

Почему копии страниц. Сайты банков закрыты антиботом: sberbank.ru на простой
запрос отдаёт заглушку в 91 символ, браузер ждёт по 20 с и тоже часто
проигрывает. В сохранённой копии Яндекса эта же страница лежит целиком и
отдаётся за секунду-две (замер: 9 из 10 страниц Сбера). Копия — это страница в
момент обхода роботом; её дату мы сохраняем, чтобы отчёт честно её показал.

Три правила, ради которых модуль вообще отдельный:
  • лимит шлюза — несколько запросов в секунду на ОБЩИЙ ключ, а глубокий отчёт
    выпускает до 16 подзапросов разом. Поэтому свой ограничитель на процесс:
    запрос, которому не хватило слота за разумное время, уходит на запасной
    поиск, а не висит и не получает 429;
  • сбой шлюза не должен выглядеть как «ничего не нашлось». Каждый вызов
    возвращает статус (ok / empty / limited / down / error), вызывающий решает,
    идти ли на запасной путь, а телеметрия видит, как часто это случается;
  • TLS проверяется нашим корнем (Russian Trusted Root CA из config/). Отключать
    проверку сертификата нельзя.

Адрес и ключ — только из окружения (SEARCH_GATEWAY_URL, SEARCH_GATEWAY_API_KEY).
"""
from __future__ import annotations

import html
import logging
import os
import re
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

OK, EMPTY, LIMITED, DOWN, ERROR = "ok", "empty", "limited", "down", "error"

_YANDEX_PATH = "/yandex/web-search"
# Лимит Яндекса на длину текста запроса.
_MAX_QUERY = 400
_MAX_SITES = 10
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ── Настройки ────────────────────────────────────────────────────────────────
# Читаем окружение при каждом вызове: dotenv подгружается позже импорта.
def _base() -> str | None:
    return (os.getenv("SEARCH_GATEWAY_URL") or "").rstrip("/") or None


def _key() -> str | None:
    return os.getenv("SEARCH_GATEWAY_API_KEY") or None


def enabled() -> bool:
    return bool(_base() and _key())


def primary() -> str:
    """Какой поиск основной: yandex (по умолчанию) или fleet."""
    return (os.getenv("SEARCH_PRIMARY") or "yandex").strip().lower()


def yandex_first() -> bool:
    return enabled() and primary() != "fleet"


def _rps() -> float:
    try:
        return max(0.5, float(os.getenv("SEARCH_GATEWAY_RPS", "4")))
    except ValueError:
        return 4.0


def _read_timeout() -> float:
    try:
        return float(os.getenv("SEARCH_GATEWAY_TIMEOUT", "12"))
    except ValueError:
        return 12.0


# ── Ограничитель частоты ─────────────────────────────────────────────────────
class _Bucket:
    """Ведро токенов на процесс. Шлюз отвечает 429 при превышении лимита на
    ключ; дешевле не доводить до отказа, чем разбирать последствия.

    После 429 ведро закрывается на Retry-After целиком: лимит общий, и
    соседние потоки упрутся в него так же.
    """

    def __init__(self, rate_fn=_rps) -> None:
        self._rate_fn = rate_fn
        self._lock = threading.Lock()
        self._tokens: float | None = None
        self._t = time.monotonic()
        self._blocked_until = 0.0

    def acquire(self, max_wait: float) -> bool:
        deadline = time.monotonic() + max(0.0, max_wait)
        while True:
            with self._lock:
                rate = self._rate_fn()
                burst = max(1.0, rate)
                now = time.monotonic()
                if self._tokens is None:
                    self._tokens = burst
                self._tokens = min(burst, self._tokens + (now - self._t) * rate)
                self._t = now
                blocked = self._blocked_until - now
                if blocked <= 0 and self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                need = max(blocked, (1.0 - self._tokens) / rate)
            if time.monotonic() + need > deadline:
                return False
            time.sleep(min(need, 0.25) + 0.005)

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)
            self._tokens = 0.0


class _Breaker:
    """Размыкатель: при отказах, которые повтором не лечатся, перестаём
    ходить в шлюз на время и сразу отправляем запросы на запасной поиск."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._open_until = 0.0
        self._fails = 0
        self.reason = ""

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def trip(self, seconds: float, reason: str) -> None:
        with self._lock:
            was_open = time.monotonic() < self._open_until
            self._open_until = max(self._open_until, time.monotonic() + seconds)
            self.reason = reason
        if not was_open:
            log.error("%s: отключён на %d мин — %s; запросы идут на запасной поиск",
                      self.name, int(seconds // 60) or 1, reason)

    def failure(self, reason: str, *, threshold: int = 3, seconds: float = 120) -> None:
        with self._lock:
            self._fails += 1
            hit = self._fails >= threshold
            if hit:
                self._fails = 0
        if hit:
            self.trip(seconds, f"{threshold} сбоя подряд: {reason}")

    def success(self) -> None:
        with self._lock:
            self._fails = 0


_bucket = _Bucket()
_breaker = _Breaker("шлюз поиска")
# Копии отдаёт отдельный хост; его сбои не должны выключать поиск.
_copy_breaker = _Breaker("копии страниц")

_client: httpx.Client | None = None
_copy_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _ssl_context() -> ssl.SSLContext:
    """Контекст с нашим бандлом (certifi + Russian Trusted Root CA)."""
    from .fetcher import CA_BUNDLE_PATH
    if CA_BUNDLE_PATH and os.path.exists(CA_BUNDLE_PATH):
        return ssl.create_default_context(cafile=CA_BUNDLE_PATH)
    return ssl.create_default_context()


def _http() -> httpx.Client:
    """Один клиент на процесс: keep-alive вместо TLS-рукопожатия на каждый
    запрос. trust_env=False: шлюз во внутреннем контуре, прокси ему не нужен."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                verify=_ssl_context(), trust_env=False,
                timeout=httpx.Timeout(connect=5, read=_read_timeout(), write=5, pool=5),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8))
        return _client


def _copy_http() -> httpx.Client:
    """Клиент для копий страниц: другой хост (кэш Яндекса), редиректы, UA браузера."""
    global _copy_client
    with _client_lock:
        if _copy_client is None:
            _copy_client = httpx.Client(
                verify=_ssl_context(), follow_redirects=True,
                timeout=httpx.Timeout(connect=5, read=20, write=5, pool=5),
                headers={"User-Agent": _UA, "Accept-Language": "ru-RU,ru;q=0.9"})
        return _copy_client


# ── Телеметрия ───────────────────────────────────────────────────────────────
def emit(kind: str, backend: str, status: str, *, dur_ms: int, n: int = 0,
         http_status: int | None = None, **extra) -> None:
    """Событие в «Пульс». Текст запроса не пишем. Ничего не ломает и не ждёт:
    запись идёт в отдельном потоке."""
    payload = {"status": status, "n": n, **extra}

    def _write() -> None:
        try:
            from ..web import telemetry
            telemetry.log_event(None, kind, page=backend, dur_ms=dur_ms,
                                status=http_status, payload=payload)
        except Exception:  # noqa: BLE001 — телеметрия никогда не ломает поиск
            pass
    threading.Thread(target=_write, daemon=True).start()


# ── Вызов шлюза ──────────────────────────────────────────────────────────────
@dataclass
class _Reply:
    status: str
    data: dict | None = None
    http_status: int | None = None
    detail: str = ""


def _post(path: str, body: dict, *, max_wait: float) -> _Reply:
    if not enabled():
        return _Reply(DOWN, detail="не настроен")
    if _breaker.is_open():
        return _Reply(DOWN, detail=_breaker.reason)
    for attempt in (1, 2):
        if not _bucket.acquire(max_wait):
            return _Reply(LIMITED, detail="нет свободного слота")
        try:
            r = _http().post(_base() + path, json=body, headers={"X-Api-Key": _key()})
        except httpx.ConnectError as e:
            if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
                # Неверный бандл не лечится повтором.
                _breaker.trip(600, f"TLS: {e}")
            else:
                _breaker.failure(f"соединение: {type(e).__name__}")
            return _Reply(DOWN, detail=type(e).__name__)
        except httpx.TimeoutException as e:
            _breaker.failure("таймаут")
            return _Reply(DOWN, detail=type(e).__name__)
        except httpx.HTTPError as e:
            _breaker.failure(type(e).__name__)
            return _Reply(DOWN, detail=type(e).__name__)
        code = r.status_code
        if code == 200:
            _breaker.success()
            try:
                return _Reply(OK, r.json(), code)
            except ValueError:
                return _Reply(ERROR, None, code, "ответ не JSON")
        if code == 429:
            try:
                wait = float(r.headers.get("retry-after") or 1)
            except ValueError:
                wait = 1.0
            _bucket.pause(min(wait, 10.0))
            if attempt == 1 and wait <= 2:
                continue            # одно ожидание по Retry-After, дальше — запасной поиск
            return _Reply(LIMITED, None, code, "429")
        if code in (401, 403):
            _breaker.trip(900, f"HTTP {code}: ключ не принят или нет прав")
            return _Reply(DOWN, None, code, f"HTTP {code}")
        if code == 402:
            _breaker.trip(1800, "HTTP 402: у поставщика закончились кредиты")
            return _Reply(DOWN, None, code, "HTTP 402")
        if code >= 500:
            _breaker.failure(f"HTTP {code}")
            return _Reply(DOWN, None, code, f"HTTP {code}")
        return _Reply(ERROR, None, code, f"HTTP {code}: {r.text[:200]}")
    return _Reply(LIMITED, detail="429")


# ── Запрос в синтаксисе Яндекса ──────────────────────────────────────────────
_SITE_RE = re.compile(r"\bsite:(\S+)", re.IGNORECASE)
_OR_RE = re.compile(r"\s+OR\s+")


def _host(d: str) -> str:
    h = (d or "").strip().strip("()\"'").lower()
    h = re.sub(r"^https?://", "", h).split("/")[0]
    return h.removeprefix("www.")


def build_query(query: str, sites: list[str] | None = None,
                fresh_hours: int | None = None) -> str:
    """Текст запроса для Яндекса.

    • site: из текста и из списка собираются в один фильтр: один домен —
      «site:d», несколько — «(site:a | site:b)»;
    • гугловский OR Яндекс не понимает: заменяем на «|». Без этого два из трёх
      запросов «Обзора» уходили мимо;
    • свежесть — оператор date:>ГГГГММДД. Сортировка по времени на замере дала
      свежий мусор, поэтому сортировку не трогаем, только отсекаем старое;
    • непарные скобки и кавычки из текста агента убираем: это синтаксис.
    """
    found = _SITE_RE.findall(query or "")
    q = _SITE_RE.sub(" ", query or "")
    q = _OR_RE.sub(" | ", q)
    if q.count("(") != q.count(")"):
        q = q.replace("(", " ").replace(")", " ")
    if q.count('"') % 2:
        q = q.replace('"', " ")
    q = re.sub(r"\(\s*\)", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    doms: list[str] = []
    for d in list(found) + list(sites or []):
        h = _host(d)
        if h and h not in doms:
            doms.append(h)
    doms = doms[:_MAX_SITES]
    tail = []
    if len(doms) == 1:
        tail.append(f"site:{doms[0]}")
    elif doms:
        tail.append("(" + " | ".join(f"site:{d}" for d in doms) + ")")
    if fresh_hours:
        since = datetime.now(timezone.utc) - timedelta(hours=int(fresh_hours))
        tail.append(f"date:>{since.strftime('%Y%m%d')}")
    suffix = (" " + " ".join(tail)) if tail else ""
    room = _MAX_QUERY - len(suffix)
    if len(q) > room:
        q = q[:room].rsplit(" ", 1)[0]
    return (q + suffix).strip()


# ── Разбор ответа ────────────────────────────────────────────────────────────
def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _iso_date(modtime: str | None) -> str | None:
    """modtime Яндекса «20260921T033736» → «2026-09-21». Это дата изменения
    страницы, а не публикации: годится как подсказка, не как факт."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})", modtime or "")
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def parse_xml(raw: str) -> tuple[str, list[dict], str]:
    """(статус, результаты, пояснение). Ошибка 15 — «ничего не найдено»:
    это честная пустота, а не сбой."""
    if not raw:
        return ERROR, [], "пустой ответ"
    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except ET.ParseError as e:
        return ERROR, [], f"XML: {e}"
    err = root.find(".//response/error")
    if err is not None:
        code = err.get("code") or ""
        return (EMPTY if code == "15" else ERROR), [], f"код {code}: {_text(err)[:120]}"
    out: list[dict] = []
    for doc in root.iter("doc"):
        url = html.unescape((doc.findtext("url") or "").strip())
        if not url.startswith("http"):
            continue
        passages = [_text(p) for p in doc.iter("passage")]
        out.append({
            "title": _text(doc.find("title"))[:200],
            "url": url,
            "snippet": " … ".join(p for p in passages if p)[:400],
            "domain": _host(doc.findtext("domain") or urlparse(url).netloc),
            "date": _iso_date(doc.findtext("modtime")),
            "cache_url": html.unescape((doc.findtext("saved-copy-url") or "").strip()) or None,
        })
    return (OK if out else EMPTY), out, ""


# Метаданные последних результатов: скрапер берёт по адресу ссылку на копию
# и дату, не повторяя поиск. Ограничено по размеру, общее на процесс.
_META: "OrderedDict[str, dict]" = OrderedDict()
_META_LOCK = threading.Lock()
_META_MAX = 5000


def _remember(items: list[dict]) -> None:
    with _META_LOCK:
        for it in items:
            _META[it["url"]] = {"date": it.get("date"), "cache_url": it.get("cache_url")}
            _META.move_to_end(it["url"])
        while len(_META) > _META_MAX:
            _META.popitem(last=False)


def meta_for(url: str) -> dict | None:
    with _META_LOCK:
        return dict(_META[url]) if url in _META else None


@dataclass
class SearchResult:
    status: str
    items: list[dict] = field(default_factory=list)
    detail: str = ""


def yandex_search(query: str, *, sites: list[str] | None = None,
                  fresh_hours: int | None = None, max_results: int = 10,
                  max_wait: float = 8.0, caller: str = "") -> SearchResult:
    """Поиск Яндекса через шлюз. Никогда не кидает исключений."""
    t0 = time.monotonic()
    text = build_query(query, sites, fresh_hours)
    body = {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": text},
            "responseFormat": "FORMAT_XML", "maxPassages": "2"}
    rep = _post(_YANDEX_PATH, body, max_wait=max_wait)
    if rep.status != OK:
        res = SearchResult(rep.status, [], rep.detail)
    else:
        status, items, detail = parse_xml((rep.data or {}).get("raw_text") or "")
        if status == ERROR:
            log.warning("шлюз поиска: %s", detail)
        _remember(items)
        res = SearchResult(status, items[:max_results], detail)
    emit("web_search", "yandex", res.status, dur_ms=int((time.monotonic() - t0) * 1000),
         n=len(res.items), http_status=rep.http_status, caller=caller,
         sites=bool(sites or _SITE_RE.search(query or "")), fresh=bool(fresh_hours))
    return res


# ── Сохранённые копии страниц ────────────────────────────────────────────────
@dataclass
class CachedCopy:
    content: bytes
    copy_date: str | None       # когда робот Яндекса сохранил страницу
    cache_url: str


_copy_bucket = _Bucket(lambda: max(0.5, float(os.getenv("SEARCH_COPY_RPS", "3") or 3)))


def _copy_date(cache_url: str) -> str | None:
    """Параметр la в ссылке на копию — момент сохранения (unix-время)."""
    try:
        la = parse_qs(urlparse(cache_url).query).get("la", [None])[0]
        return datetime.fromtimestamp(int(la), tz=timezone.utc).date().isoformat() if la else None
    except (TypeError, ValueError, OverflowError):
        return None


def _strip_header(content: bytes) -> bytes:
    """Вырезает плашку Яндекса «сохранённая копия» — иначе её текст попадёт в
    страницу как содержимое сайта."""
    try:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(content.decode("utf-8", "ignore"))
        for node in tree.css("#yandex-cache-hdr"):
            node.decompose()
        return (tree.html or "").encode("utf-8")
    except Exception:  # noqa: BLE001 — без очистки лучше, чем без копии
        return content


def read_cached_copy(url: str, *, max_wait: float = 6.0) -> CachedCopy | None:
    """Сохранённая копия страницы из индекса Яндекса или None.

    Ссылку на копию берём из недавнего поиска; если адрес пришёл не из
    Яндекса, спрашиваем её оператором url: (один запрос поиска).
    """
    if not enabled() or _copy_breaker.is_open():
        return None
    t0 = time.monotonic()
    meta = meta_for(url) or {}
    cache_url = meta.get("cache_url")
    if not cache_url:
        rep = _post(_YANDEX_PATH, {"query": {"searchType": "SEARCH_TYPE_RU",
                                             "queryText": f"url:{url}"[:_MAX_QUERY]},
                                   "responseFormat": "FORMAT_XML"}, max_wait=max_wait)
        if rep.status == OK:
            _status, items, _ = parse_xml((rep.data or {}).get("raw_text") or "")
            _remember(items)
            cache_url = next((it.get("cache_url") for it in items if it.get("cache_url")), None)
    if not cache_url:
        emit("web_read", "yandex_copy", EMPTY, dur_ms=int((time.monotonic() - t0) * 1000))
        return None
    if not _copy_bucket.acquire(max_wait):
        emit("web_read", "yandex_copy", LIMITED, dur_ms=int((time.monotonic() - t0) * 1000))
        return None
    try:
        r = _copy_http().get(cache_url)
    except httpx.HTTPError as e:
        _copy_breaker.failure(type(e).__name__, threshold=5, seconds=300)
        emit("web_read", "yandex_copy", DOWN, dur_ms=int((time.monotonic() - t0) * 1000))
        return None
    final = str(r.url)
    # Капча вместо копии: частим — пауза, а не десятки пустых попыток.
    if "captcha" in final.lower() or b"showcaptcha" in r.content[:20000]:
        _copy_breaker.trip(600, "Яндекс показал капчу на копиях страниц")
        emit("web_read", "yandex_copy", LIMITED, dur_ms=int((time.monotonic() - t0) * 1000),
             http_status=r.status_code)
        return None
    if r.status_code != 200 or len(r.content) < 500:
        emit("web_read", "yandex_copy", ERROR, dur_ms=int((time.monotonic() - t0) * 1000),
             http_status=r.status_code)
        return None
    _copy_breaker.success()
    emit("web_read", "yandex_copy", OK, dur_ms=int((time.monotonic() - t0) * 1000),
         http_status=200, n=len(r.content))
    return CachedCopy(_strip_header(r.content), _copy_date(cache_url), cache_url)


def status() -> dict:
    """Для диагностики и «Пульса»: настроен ли, основной ли, не отключён ли."""
    return {"enabled": enabled(), "primary": primary() if enabled() else "fleet",
            "breaker_open": _breaker.is_open(), "breaker_reason": _breaker.reason,
            "copies_breaker_open": _copy_breaker.is_open(), "rps": _rps()}
