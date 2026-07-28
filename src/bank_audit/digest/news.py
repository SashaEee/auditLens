"""Сбор новостей для дайджеста — БЕЗ LLM (fetch → normalize → dedupe → окно 48ч).

Источники P0 (проверены с cloud.ru IP 2026-07-02):
  • RSS ЦБ (RssPress/RssNews) — регуляторика из первоисточника
  • RSS banki.ru / frankmedia.ru — банковская повестка
  • t.me/s/<канал> — публичные веб-превью Telegram, обычный HTTP GET без API:
    Банк России, Банкста (инциденты), Киберполиция МВД (схемы мошенничества),
    Frank Media
  • SearXNG (bing+dogpile живы) — точечные запросы по хищениям/предписаниям

Капчи НЕ обходим: упавший источник просто выпадает из корзины, его статус
честно пишется в sources[] (фронт показывает покрытие).

Отдельно: fetch_key_rate() — ключевая ставка через SOAP ЦБ (кэш 6 ч).
"""
from __future__ import annotations

import concurrent.futures as cf
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_TIMEOUT = float(os.getenv("DIGEST_FETCH_TIMEOUT_S", "10"))
MAX_ITEMS = int(os.getenv("DIGEST_NEWS_MAX_ITEMS", "40"))
_PER_SOURCE_CAP = 10          # чтобы cbr_news (100 items) не вытеснил остальных
_WINDOW_H = int(os.getenv("DIGEST_NEWS_WINDOW_H", "48"))

# tag — подсказка LLM для группировки (regulator/incident/scheme/market/search)
# tag — грубая категория (для группировки на UI). dimension — измерение аудита
# (compliance/conduct/ops/fraud/market) для матчинга новости на интересы пользователя
# в персональном дайджесте. Новые источники проверены на доступность из контура
# (httpx-проба 2026-07-21): ФАС/бизнес/взыскание через Telegram-web (t.me/s/, парсится
# существующим _parse_tg). Гос-RSS (fas.gov.ru/rospotrebnadzor) чистого фида не отдают.
SOURCES: list[dict] = [
    {"key": "cbr_press",     "kind": "rss", "url": "https://www.cbr.ru/rss/RssPress",     "tag": "regulator", "dimension": "compliance"},
    {"key": "cbr_news",      "kind": "rss", "url": "https://www.cbr.ru/rss/RssNews",      "tag": "regulator", "dimension": "compliance"},
    {"key": "banki_news",    "kind": "rss", "url": "https://www.banki.ru/xml/news.rss",   "tag": "market",    "dimension": "market"},
    {"key": "frankmedia",    "kind": "rss", "url": "https://frankmedia.ru/feed",          "tag": "market",    "dimension": "market"},
    {"key": "tg_cbr",        "kind": "tg",  "url": "https://t.me/s/centralbank_russia",   "tag": "regulator", "dimension": "compliance"},
    {"key": "tg_banksta",    "kind": "tg",  "url": "https://t.me/s/banksta",              "tag": "incident",  "dimension": "ops"},
    {"key": "tg_cyberpolice","kind": "tg",  "url": "https://t.me/s/cyberpolice_rus",      "tag": "scheme",    "dimension": "fraud"},
    {"key": "tg_frankmedia", "kind": "tg",  "url": "https://t.me/s/frank_media",          "tag": "market",    "dimension": "market"},
    # ── добавлено для персонализации (2026-07-21, проверено из контура) ──
    # Бизнес-повестка (LLM-секция news отбирает банк-релевантные под аудит).
    # ФАС/dolg_rf пробовали — @fasrussia общий (телеком/спорт, даты не парсятся),
    # @dolg_rf пуст → не добавлены. Гос-RSS чистого фида не отдают.
    {"key": "tg_kommersant", "kind": "tg",  "url": "https://t.me/s/kommersant",           "tag": "market",    "dimension": "market"},
    {"key": "tg_rbc",        "kind": "tg",  "url": "https://t.me/s/rbc_news",             "tag": "market",    "dimension": "market"},
]

# Точечные поисковые запросы (SearXNG). У выдачи нет дат → берём мало и метим.
SEARCH_QUERIES = [
    ("Сбербанк сбой OR инцидент", "incident"),
    ("банк мошенничество схема клиентов", "scheme"),
    ("ЦБ предписание OR штраф банку розница", "regulator"),
]


# ── низкоуровневые фетчи ──────────────────────────────────────────────────────

def _get(url: str) -> httpx.Response:
    return httpx.get(url, timeout=_TIMEOUT, follow_redirects=True,
                     headers={"User-Agent": _UA})


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", s or "")).replace("\xa0", " ").strip()


def _parse_dt(raw_dt: str) -> datetime | None:
    """Всегда aware-datetime (naive → UTC): смесь naive/aware в сортировке
    и оконном фильтре даёт TypeError."""
    if not raw_dt:
        return None
    raw_dt = raw_dt.strip()
    ts = None
    try:
        ts = parsedate_to_datetime(raw_dt)
    except Exception:  # noqa: BLE001
        try:
            ts = datetime.fromisoformat(raw_dt)
        except Exception:  # noqa: BLE001
            return None
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)

# ── картинки для плиток «Для вас» (enclosure/media:* в RSS, фото в t.me/s) ────
_MEDIA_NS = "{http://search.yahoo.com/mrss/}"
_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.IGNORECASE)
# фото/видео-превью TG: class идёт до style, внутри атрибутов '>' не бывает
_TG_IMG_RE = re.compile(
    r"tgme_widget_message_(?:photo_wrap|video_thumb)[^>]*"
    r"background-image:url\('([^']+)'\)")


def _rss_image(it: ET.Element) -> str | None:
    """Картинка item'а: <enclosure type=image> либо media:content/thumbnail."""
    enc = it.find("enclosure")
    if enc is not None:
        url = (enc.get("url") or "").strip()
        typ = (enc.get("type") or "").lower()
        if url.startswith("http") and ("image" in typ or _IMG_EXT_RE.search(url)):
            return url
    for tag in ("content", "thumbnail"):
        for m in it.iter(_MEDIA_NS + tag):
            url = (m.get("url") or "").strip()
            typ = (m.get("type") or "").lower()
            if url.startswith("http") and ("video" not in typ):
                return url
    return None


_IMG_SRC_RE = re.compile(r'<img[^>]+src="(https?://[^"]+)"', re.IGNORECASE)


def _img_from_html(raw: str) -> str | None:
    """<img src> внутри description/content:encoded (frankmedia кладёт так)."""
    m = _IMG_SRC_RE.search(html.unescape(raw or ""))
    return m.group(1) if m else None


def _rss_image_re(block: str) -> str | None:
    """То же для regex-fallback (битый XML banki.ru)."""
    m = re.search(r'<(enclosure|media:content|media:thumbnail)[^>]*url="([^"]+)"',
                  block, re.IGNORECASE)
    if m:
        url = html.unescape(m.group(2)).strip()
        tag_txt = m.group(0).lower()
        if url.startswith("http") and "video" not in tag_txt and (
                "image" in tag_txt or "media:" in m.group(1).lower()
                or _IMG_EXT_RE.search(url)):
            return url
    return _img_from_html(block)


def _xml_field(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    val = m.group(1)
    cm = _CDATA_RE.search(val)
    return (cm.group(1) if cm else val).strip()


def _parse_rss_fallback(xml_text: str, src: dict) -> list[dict]:
    """Regex-парсер item-блоков — для фидов с невалидным XML (banki.ru вставляет
    сырые <script>/&). Терпит мусор между тегами."""
    items = []
    for block in re.findall(r"<item[ >](.*?)</item>", xml_text, re.DOTALL | re.IGNORECASE):
        title = _strip_html(_xml_field(block, "title"))
        link = _strip_html(_xml_field(block, "link"))
        if not title or not link.startswith("http"):
            continue
        items.append({"title": title[:220], "url": link,
                      "ts": _parse_dt(_xml_field(block, "pubDate")),
                      "snippet": _strip_html(_xml_field(block, "description"))[:300],
                      "source": src["key"], "tag": src["tag"],
                      "dimension": src.get("dimension"),
                      "image": _rss_image_re(block)})
    return items


def _parse_rss(xml_text: str, src: dict) -> list[dict]:
    """Мини-парсер RSS 2.0 (stdlib) + regex-fallback на невалидный XML."""
    # вычищаем управляющие символы, которые роняют ElementTree
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _parse_rss_fallback(xml_text, src)
    items = []
    for it in root.iter("item"):
        title = _strip_html((it.findtext("title") or ""))
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        raw_dt = it.findtext("pubDate") or it.findtext(
            "{http://purl.org/dc/elements/1.1/}date") or ""
        desc_raw = ((it.findtext("description") or "") + " " +
                    (it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""))
        snippet = _strip_html(it.findtext("description") or "")[:300]
        items.append({"title": title[:220], "url": link, "ts": _parse_dt(raw_dt),
                      "snippet": snippet, "source": src["key"], "tag": src["tag"],
                      "dimension": src.get("dimension"),
                      "image": _rss_image(it) or _img_from_html(desc_raw)})
    return items


def _parse_tg(html_text: str, src: dict) -> list[dict]:
    """Публичное веб-превью t.me/s/<канал>: режем на блоки сообщений и парсим
    каждый отдельно (одним regex по всей странице опциональная группа текста
    всегда матчилась пустой)."""
    items = []
    for block in html_text.split("tgme_widget_message_wrap")[1:]:
        post_m = re.search(r'data-post="([^"]+)"', block)
        time_m = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        # у поста-ответа ПЕРВЫЙ message_text — это цитата чужого поста
        # (reply-превью); собственный текст идёт последним → берём последний
        text_ms = re.findall(r'tgme_widget_message_text[^>]*>(.*?)</div>',
                             block, re.DOTALL)
        if not post_m or not time_m:
            continue
        body_html = text_ms[-1] if text_ms else ""
        text = _strip_html(body_html.replace("<br/>", "\n").replace("<br>", "\n"))
        if not text or len(text) < 25:      # сервисные/медиа-посты без текста
            continue
        ts = _parse_dt(time_m.group(1))
        first_line = text.split("\n", 1)[0].strip()
        title = (first_line if len(first_line) >= 15 else text)[:180]
        img_m = _TG_IMG_RE.search(block)
        items.append({"title": title, "url": f"https://t.me/{post_m.group(1)}",
                      "ts": ts, "snippet": text[:400],
                      "source": src["key"], "tag": src["tag"],
                      "dimension": src.get("dimension"),
                      "image": html.unescape(img_m.group(1)) if img_m else None})
    return items


def _fetch_source(src: dict) -> tuple[list[dict], dict]:
    """Возвращает (items, status). Любой сбой → пустой список + честный статус."""
    status = {"name": src["key"], "ok": False, "items": 0}
    try:
        r = _get(src["url"])
        if r.status_code != 200:
            status["skipped_reason"] = f"http {r.status_code}"
            return [], status
        items = (_parse_rss(r.text, src) if src["kind"] == "rss"
                 else _parse_tg(r.text, src))
        items.sort(key=lambda x: x["ts"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        items = items[:_PER_SOURCE_CAP]
        status.update(ok=True, items=len(items))
        return items, status
    except Exception as e:  # noqa: BLE001 — деградация источника, не секции
        status["skipped_reason"] = f"{type(e).__name__}: {str(e)[:120]}"
        return [], status


def _fetch_search() -> tuple[list[dict], dict]:
    """SearXNG-запросы (best-effort). Выдача без дат — помечаем tag=search-*."""
    status = {"name": "web_search", "ok": False, "items": 0}
    if os.getenv("DIGEST_SEARCH", "1") == "0":
        status["skipped_reason"] = "disabled"
        return [], status
    items = []
    try:
        from ..rag.web_search import search
        dim = {"incident": "ops", "scheme": "fraud", "regulator": "compliance"}
        for query, tag in SEARCH_QUERIES:
            for r in search(query, max_results=4, cache_ttl_seconds=6 * 3600):
                items.append({"title": (r.get("title") or "")[:220],
                              "url": r.get("url") or "", "ts": None,
                              "snippet": (r.get("snippet") or "")[:300],
                              "source": "web_search", "tag": tag,
                              "dimension": dim.get(tag, "market"), "image": None})
        status.update(ok=True, items=len(items))
    except Exception as e:  # noqa: BLE001
        status["skipped_reason"] = f"{type(e).__name__}: {str(e)[:120]}"
    return items, status


# ── нормализация / дедуп ──────────────────────────────────────────────────────

_NORM_RE = re.compile(r"[^а-яa-z0-9ё]+")
# ведущие эмодзи/пиктограммы TG-постов — не наш тон (аудиторский инструмент)
_EMOJI_RE = re.compile("^[\\s\\u2190-\\u2BFF\\u2600-\\u27BF\\u2B00-\\u2BFF"
                       "\\uFE0F\\u200D\\U0001F000-\\U0001FAFF]+")


def _norm_title(t: str) -> str:
    return _NORM_RE.sub(" ", (t or "").lower()).strip()[:120]


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        host = ""
        try:
            host = urlparse(it["url"]).netloc.lower()
        except Exception:  # noqa: BLE001
            pass
        key = (_norm_title(it["title"]), host)
        # дубль заголовка с ДРУГОГО хоста тоже режем (перепечатки агентств)
        key_soft = _norm_title(it["title"])
        if key in seen or (len(key_soft) > 30 and key_soft in seen):
            continue
        seen.add(key)
        if len(key_soft) > 30:
            seen.add(key_soft)
        out.append(it)
    return out


def fetch_all() -> tuple[list[dict], list[dict]]:
    """Параллельный сбор всех источников. Возвращает (items, sources_status).
    items: свежие (окно _WINDOW_H), дедуплицированные, топ-MAX_ITEMS."""
    tasks = [*SOURCES]
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_fetch_source, tasks))
    items = [it for r, _ in results for it in r]
    statuses = [st for _, st in results]
    s_items, s_status = _fetch_search()
    items += s_items
    statuses.append(s_status)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_WINDOW_H)
    fresh = []
    for it in items:
        ts = it.get("ts")
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            it["ts"] = ts
        fresh.append(it)

    fresh = _dedupe(fresh)
    fresh.sort(key=lambda x: x["ts"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    fresh = fresh[:MAX_ITEMS]
    for it in fresh:                       # ts → isoformat для jsonb
        it["title"] = _EMOJI_RE.sub("", it["title"]).strip() or it["title"]
        it["ts"] = it["ts"].isoformat() if it.get("ts") else None
        try:
            it["domain"] = urlparse(it["url"]).netloc.replace("www.", "")
        except Exception:  # noqa: BLE001
            it["domain"] = ""
    return fresh, statuses


# ── ключевая ставка (SOAP ЦБ, проверен реальный POST) ─────────────────────────

_KEYRATE_URL = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"
_KEYRATE_ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <KeyRate xmlns="http://web.cbr.ru/">
      <fromDate>{frm}</fromDate>
      <ToDate>{to}</ToDate>
    </KeyRate>
  </soap:Body>
</soap:Envelope>"""

_KR_ROW_RE = re.compile(r"<DT>([^<]+)</DT>\s*<Rate>([^<]+)</Rate>", re.IGNORECASE)


def fetch_key_rate(months: int = 6) -> dict | None:
    """История ключевой ставки за N месяцев: {current, points:[{date,rate}]}.
    Кэш 6 ч (rag_cache) — ставка меняется в дни решений СД ЦБ."""
    from ..rag import cache as rag_cache
    cached = rag_cache.get("digest_keyrate", months)
    if cached:
        return cached
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    to = now.strftime("%Y-%m-%d")
    r = httpx.post(_KEYRATE_URL,
                   content=_KEYRATE_ENVELOPE.format(frm=frm, to=to).encode(),
                   timeout=_TIMEOUT,
                   headers={"Content-Type": "text/xml; charset=utf-8",
                            "SOAPAction": '"http://web.cbr.ru/KeyRate"',
                            "User-Agent": _UA})
    if r.status_code != 200:
        return None
    points = []
    for dt_raw, rate_raw in _KR_ROW_RE.findall(r.text):
        try:
            points.append({"date": dt_raw.strip()[:10],
                           "rate": float(rate_raw.strip().replace(",", "."))})
        except ValueError:
            continue
    if not points:
        return None
    points.sort(key=lambda p: p["date"])
    out = {"current": points[-1]["rate"], "as_of": points[-1]["date"], "points": points}
    try:
        rag_cache.put("digest_keyrate", out, 6 * 3600, months)
    except Exception:  # noqa: BLE001
        pass
    return out
