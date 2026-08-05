"""Сбор новостей для дайджеста — БЕЗ LLM-отбора (он в writer.news).

Конвейер пула (переработан 05.08.2026 по итогам аудита — судья давал 82
процента мусора в пуле, половину мусора в публикации):
  fetch → окно свежести (48ч / 96ч регуляторика) → дедуп заголовков →
  смысловой дедуп (эмбеддинги, рерайты агентств) → межднёвная память
  (digest_news_seen: публиковавшееся и трижды отвергнутое не возвращается) →
  сборка по квотам: рег-квота с приоритетом РЕШЕНИЙ над текучкой (per-item),
  потолок общих лент (cls=gen) и недатированных.

Источники: RSS ЦБ, banki.ru, frankmedia, Ведомости «Финансы»/«Экономика»,
Ъ «Экономика», t.me/s/<канал> (ЦБ, Банкста, Киберполиция, Frank Media, РБК),
РИА (общий, под квотой), SearXNG. Капчи НЕ обходим: упавший источник выпадает
из корзины, статус честно пишется в sources[] (фронт показывает покрытие).

Отдельно: fetch_key_rate() — ключевая ставка через SOAP ЦБ (кэш 1 ч).
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
# Окно для регуляторных источников (ЦБ, ФАС). 96 ч = пятничное решение доживает
# до утра понедельника: ЦБ объявляет ставку в пятницу ~13:30, а работает аудитор
# в будни. Шире общего окна намеренно — таких новостей мало, и они не вытесняют
# ленту (на источник всё равно действует _PER_SOURCE_CAP).
_WINDOW_REG_H = int(os.getenv("DIGEST_NEWS_WINDOW_REG_H", "96"))
# Сколько мест в пуле держим за регуляторикой, чтобы её не вытеснил свежий шум.
# 10 из 40 — четверть: заметно, но лента остаётся живой.
_REG_QUOTA = int(os.getenv("DIGEST_NEWS_REG_QUOTA", "10"))
# Потолок мест для общих лент (cls=gen). Без него общая повестка душила
# банковскую чистой свежестью (РИА обновляется каждые минуты) — замер
# 05.08.2026: 57 процентов пула, judge-оценка мусора в пуле 82 процента.
_GEN_CAP = int(os.getenv("DIGEST_NEWS_GEN_CAP", "8"))
# Недатированных в пуле НЕ БЫВАЕТ (05.08.2026): выдача поиска без ts обходила
# окно свежести — пресс-релиз Сбера 2011 года о сбое процессинга ушёл в выпуск
# и в ЗАГОЛОВОК дня, а страница мониторинга от 24.03.2026 выдала мартовскую
# статистику за сегодняшнюю. Теперь недатированное датируем (_resolve_undated),
# недатируемое выбрасываем: «не знаем когда» для новостной ленты = «не новость».
# Смысловой дедуп пула (эмбеддинги): рерайты агентств («гендиректор пострадал» /
# «водитель директора…») дедуп заголовков не ловит — 05.08.2026 одно событие
# вошло в пул тремя формулировками.
_SEMDEDUP = os.getenv("DIGEST_NEWS_SEMDEDUP", "1") == "1"
_SEMDEDUP_T = float(os.getenv("DIGEST_NEWS_SEMDEDUP_T", "0.88"))
# Межднёвная память digest_news_seen (миграция 032).
_SEEN = os.getenv("DIGEST_NEWS_SEEN", "1") == "1"

# tag — грубая категория (для группировки на UI). dimension — измерение аудита
# (compliance/conduct/ops/fraud/market) для матчинга новости на интересы пользователя
# в персональном дайджесте. cls — класс источника для квоты пула:
#   bank — банковская/регуляторная повестка (наполняет пул в первую очередь);
#   gen  — общие ленты «обо всём» (жёсткий потолок _GEN_CAP мест).
# Без классовой квоты общие ленты душили банковские по свежести: замер 05.08.2026
# на живом пуле — РИА 10 + РБК 7 + Ъ 6 = 57 процентов пула, banki.ru выжил с
# одной позицией, киберполиция (схемы мошенничества) — с нулём.
SOURCES: list[dict] = [
    # Признак «это РЕШЕНИЕ ЦБ, а не текучка» считается ПО КАЖДОЙ НОВОСТИ
    # (_DECISION_RE/_ROUTINE_RE в fetch_all), а не флагом источника: флаг на
    # cbr_press поднимал в топ рег-квоты и «Редкие монеты» с юбилеями —
    # пресс-релизы без решений (пойман на живом пуле 05.08.2026).
    {"key": "cbr_press",     "kind": "rss", "url": "https://www.cbr.ru/rss/RssPress",     "tag": "regulator", "dimension": "compliance", "cls": "bank"},
    {"key": "cbr_news",      "kind": "rss", "url": "https://www.cbr.ru/rss/RssNews",      "tag": "regulator", "dimension": "compliance", "cls": "bank"},
    {"key": "banki_news",    "kind": "rss", "url": "https://www.banki.ru/xml/news.rss",   "tag": "market",    "dimension": "market",     "cls": "bank"},
    {"key": "frankmedia",    "kind": "rss", "url": "https://frankmedia.ru/feed",          "tag": "market",    "dimension": "market",     "cls": "bank"},
    {"key": "tg_cbr",        "kind": "tg",  "url": "https://t.me/s/centralbank_russia",   "tag": "regulator", "dimension": "compliance", "cls": "bank"},
    {"key": "tg_banksta",    "kind": "tg",  "url": "https://t.me/s/banksta",              "tag": "incident",  "dimension": "ops",        "cls": "bank"},
    {"key": "tg_cyberpolice","kind": "tg",  "url": "https://t.me/s/cyberpolice_rus",      "tag": "scheme",    "dimension": "fraud",      "cls": "bank"},
    {"key": "tg_frankmedia", "kind": "tg",  "url": "https://t.me/s/frank_media",          "tag": "market",    "dimension": "market",     "cls": "bank"},
    # ── деловые СМИ: секционные фиды вместо общих лент (проверены 05.08.2026) ──
    # Ведомости отдают тематические рубрики — «Финансы» идёт классом bank
    # (банки/рынки по определению рубрики), «Экономика» — классом gen (макро).
    # t.me/s/kommersant (лента обо всём: назначения, взрывы, геополитика)
    # заменён на RSS рубрики «Экономика» Ъ.
    {"key": "vedomosti_fin", "kind": "rss", "url": "https://www.vedomosti.ru/rss/rubric/finance.xml",   "tag": "market", "dimension": "market", "cls": "bank"},
    {"key": "vedomosti_econ","kind": "rss", "url": "https://www.vedomosti.ru/rss/rubric/economics.xml", "tag": "market", "dimension": "market", "cls": "gen"},
    {"key": "kommersant_econ","kind":"rss", "url": "https://www.kommersant.ru/RSS/section-economics.xml","tag": "market", "dimension": "market", "cls": "gen"},
    # Общие ленты: секционных фидов у РБК/РИА нет (проверено 05.08.2026, 404) —
    # держим под классовой квотой ради скорости важных сообщений (решения ЦБ,
    # предупреждения о мошенничестве госагентство часто даёт первым).
    {"key": "tg_rbc",        "kind": "tg",  "url": "https://t.me/s/rbc_news",             "tag": "market", "dimension": "market", "cls": "gen"},
    {"key": "ria_novosti",   "kind": "rss", "url": "https://ria.ru/export/rss2/archive/index.xml", "tag": "market", "dimension": "market", "cls": "gen"},
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
                      "cls": src.get("cls", "bank"),
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
                      "cls": src.get("cls", "bank"),
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
                      "cls": src.get("cls", "bank"),
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
                              "source": "web_search", "tag": tag, "cls": "bank",
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


# ── датировка недатированных (выдача поиска) ─────────────────────────────────
# Порядок дешёвый → дорогой: дата в пути URL → явная полная дата в заголовке/
# сниппете → метаданные самой страницы (article:published_time, datePublished,
# <time datetime>). Голые упоминания года в тексте НЕ считаем датой публикации
# («в 2024 году ЦБ ввёл…» — обычная фраза в свежей статье). Антибот-заглушки
# (sberbank.ru по HTTP) метаданных не отдают → страница остаётся недатируемой
# и выбрасывается — ровно то, что нужно.

_URL_DATE_RE = re.compile(r"/((?:19|20)\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:/|\b)")
_URL_YEAR_RE = re.compile(r"/((?:19|20)\d{2})(?:/|\b)")
_RU_MONTH_N = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
               "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
               "ноября": 11, "декабря": 12}
_TXT_DATE_RE = re.compile(r"\b(\d{1,2})[.](\d{1,2})[.]((?:19|20)\d{2})\b")
_TXT_RU_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|"
    r"сентября|октября|ноября|декабря)\s+((?:19|20)\d{2})")
_META_DATE_RES = (
    re.compile(r'"datePublished"\s*:\s*"([^"]{8,40})"'),
    re.compile(r'<meta[^>]{0,200}?(?:article:published_time|datePublished|'
               r'pubdate)[^>]{0,200}?content="([^"]{8,40})"', re.I),
    re.compile(r'<meta[^>]{0,200}?content="([^"]{8,40})"[^>]{0,200}?'
               r'(?:article:published_time|datePublished|pubdate)', re.I),
    re.compile(r'<time[^>]{0,120}?datetime="([^"]{8,40})"', re.I),
)


def _page_date(url: str) -> datetime | None:
    """Дата публикации из метаданных страницы (только структурные источники)."""
    try:
        r = _get(url)
        if r.status_code != 200:
            return None
        # ВЕСЬ документ, не первые N КБ: у forbes.ru datePublished лежит за
        # пределами первых 60 КБ — обрезка стоила фантомного «сбоя дня» из
        # апрельской статьи (05.08.2026)
        head = r.text[:500000]
        for rx in _META_DATE_RES:
            m = rx.search(head)
            if m:
                ts = _parse_dt(m.group(1))
                if ts:
                    return ts
    except Exception:  # noqa: BLE001
        pass
    return None


def _resolve_undated(items: list[dict]) -> list[dict]:
    """Датирует элементы без ts; недатируемые выбрасывает (см. коммент выше)."""
    undated = [i for i in items if not i.get("ts")]
    if not undated:
        return items

    def _try(it: dict) -> None:
        u = it.get("url") or ""
        m = _URL_DATE_RE.search(u)
        if m:
            try:
                it["ts"] = datetime(int(m[1]), int(m[2]), int(m[3]),
                                    tzinfo=timezone.utc)
                return
            except ValueError:
                pass
        m = _URL_YEAR_RE.search(u)
        if m and int(m[1]) < datetime.now(timezone.utc).year:
            # старый год в пути — точный день не важен, в окно всё равно не пройдёт
            it["ts"] = datetime(int(m[1]), 12, 31, tzinfo=timezone.utc)
            return
        txt = f'{it.get("title") or ""} {it.get("snippet") or ""}'
        m = _TXT_DATE_RE.search(txt)
        if m:
            try:
                it["ts"] = datetime(int(m[3]), int(m[2]), int(m[1]),
                                    tzinfo=timezone.utc)
                return
            except ValueError:
                pass
        m = _TXT_RU_DATE_RE.search(txt)
        if m:
            try:
                it["ts"] = datetime(int(m[3]), _RU_MONTH_N[m[2]], int(m[1]),
                                    tzinfo=timezone.utc)
                return
            except (ValueError, KeyError):
                pass
        it["ts"] = _page_date(u)

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(_try, undated))
    kept = [i for i in items if i.get("ts")]
    if len(kept) < len(items):
        log.info("выброшено недатируемых: %d (из них поиск: %d)",
                 len(items) - len(kept),
                 sum(1 for i in items
                     if not i.get("ts") and i.get("source") == "web_search"))
    return kept


# ── решение vs текучка (по каждой новости, не по источнику) ──────────────────
# Раньше флаг decisions стоял на ИСТОЧНИКЕ cbr_press — и юбилейные монеты из
# пресс-релизов сортировались в рег-квоте выше реальных новостей (замер 05.08.2026).
_DECISION_RE = re.compile(
    r"ключев\w+ ставк|санкци|предписани|аннулир\w+ лиценз|отозв\w+ лиценз|"
    r"отзыв\w* лиценз|штраф|мер\w+ воздействия|решени\w+ совета директоров|"
    r"ограничени\w+ (на |для )?банк|внепланов\w+ проверк", re.I)
_ROUTINE_RE = re.compile(
    r"\bruonia\b|\bрепо\b|валютн\w+ своп|депозиты банков|усреднени|"
    r"обязательн\w+ резерв|монет\w|юбилейн|100-лети|выставк|"
    r"инсайдерская информация банка россии", re.I)

# Авторитет источника: при смысловом дубле остаётся самый авторитетный
# представитель (первоисточник/профильное СМИ выигрывает у перепечатки).
_AUTHORITY = {"cbr_press": 0, "cbr_news": 0, "tg_cbr": 1,
              "banki_news": 2, "frankmedia": 2, "vedomosti_fin": 2,
              "tg_frankmedia": 3, "tg_banksta": 3, "tg_cyberpolice": 3,
              "kommersant_econ": 4, "vedomosti_econ": 4,
              "tg_rbc": 5, "ria_novosti": 5, "web_search": 6}


def _sem_dedupe(items: list[dict]) -> list[dict]:
    """Смысловой дедуп: косинус эмбеддингов (заголовок+сниппет) ≥ порога = одно
    событие. У выжившего копится echo — сколько источников продублировали
    (сигнал значимости для отбора). Best-effort: эмбеддер упал → пул как есть."""
    if not _SEMDEDUP or len(items) < 2:
        return items
    try:
        from ..rag import embedder
        texts = [f'{i.get("title") or ""}. {(i.get("snippet") or "")[:200]}'
                 for i in items]
        vecs = embedder.embed_batch(texts)

        def _k(n: int):
            ts = items[n].get("ts")
            return (_AUTHORITY.get(items[n]["source"], 9),
                    -(ts.timestamp() if ts else 0.0))
        kept: list[int] = []
        drop: set[int] = set()
        for n in sorted(range(len(items)), key=_k):
            twin = next((j for j in kept
                         if embedder.cosine_similarity(vecs[n], vecs[j]) >= _SEMDEDUP_T),
                        None)
            if twin is None:
                kept.append(n)
            else:
                drop.add(n)
                items[twin]["echo"] = int(items[twin].get("echo") or 1) + 1
        return [it for n, it in enumerate(items) if n not in drop]
    except Exception as e:  # noqa: BLE001 — дедуп не должен ронять сбор
        log.warning("смысловой дедуп пропущен: %s", e)
        return items


# ── межднёвная память (digest_news_seen, миграция 032) ───────────────────────

def _url_hash(u: str) -> str:
    import hashlib
    return hashlib.sha256((u or "").encode()).hexdigest()[:32]


def _filter_seen(items: list[dict]) -> list[dict]:
    """Убирает из пула то, что уже публиковалось в ПРОШЛЫЕ дни (по url; для
    рубричных заголовков ЦБ — одно название каждый день под новыми url — по
    title_norm с выдержкой 5 дней, кроме настоящих решений), и то, что три
    разных дня попадало в пул, но ни разу не отбиралось. Сегодняшние публикации
    не глушим: force-refresh пересобирает тот же выпуск и не должен терять
    собственные утренние новости. Best-effort: БД недоступна → пул как есть."""
    if not _SEEN or not items:
        return items
    try:
        from sqlalchemy import bindparam, text as _t
        from .. import db
        hs = [_url_hash(i.get("url") or "") for i in items]
        tn = [_norm_title(i.get("title") or "") for i in items]
        q = _t("""
            SELECT url_hash, title_norm, times_pool, picked,
                   (picked_at AT TIME ZONE 'Europe/Moscow')::date AS picked_day
              FROM digest_news_seen
             WHERE url_hash IN :hs OR title_norm IN :tn
        """).bindparams(bindparam("hs", expanding=True),
                        bindparam("tn", expanding=True))
        with db.session() as s:
            rows = s.execute(q, {"hs": hs, "tn": [x for x in tn if x] or ["-"]
                                 }).mappings().all()
        today = datetime.now(timezone(timedelta(hours=3))).date()
        by_hash = {r["url_hash"]: r for r in rows}
        picked_titles = {r["title_norm"] for r in rows
                         if r["picked"] and r["title_norm"] and r["picked_day"]
                         and r["picked_day"] < today
                         and (today - r["picked_day"]).days <= 5}
        out = []
        for n, it in enumerate(items):
            r = by_hash.get(hs[n])
            if r is not None:
                if r["picked"] and r["picked_day"] and r["picked_day"] < today:
                    continue
                if not r["picked"] and int(r["times_pool"] or 0) >= 3:
                    continue
            if (tn[n] and tn[n] in picked_titles
                    and not _DECISION_RE.search(it.get("title") or "")):
                continue
            out.append(it)
        if len(out) < len(items):
            log.info("память новостей: отфильтровано %d повторов", len(items) - len(out))
        return out
    except Exception as e:  # noqa: BLE001
        log.info("память новостей недоступна (%s) — пул без межднёвного фильтра", e)
        return items


def _record_pool(items: list[dict]) -> None:
    """Регистрирует состав пула. times_pool растёт по ДНЯМ, не по прогонам
    (lazy/force собирают пул несколько раз в сутки). Заодно чистит старьё."""
    if not _SEEN or not items:
        return
    try:
        from sqlalchemy import text as _t
        from .. import db
        with db.session() as s:
            for it in items:
                s.execute(_t("""
                    INSERT INTO digest_news_seen (url_hash, url, title, title_norm, source)
                    VALUES (:h, :u, :t, :tn, :src)
                    ON CONFLICT (url_hash) DO UPDATE SET
                        times_pool = digest_news_seen.times_pool
                                     + CASE WHEN digest_news_seen.last_seen < current_date
                                            THEN 1 ELSE 0 END,
                        last_seen = current_date
                """), {"h": _url_hash(it.get("url") or ""),
                       "u": (it.get("url") or "")[:800],
                       "t": (it.get("title") or "")[:300],
                       "tn": _norm_title(it.get("title") or ""),
                       "src": it.get("source")})
            s.execute(_t("DELETE FROM digest_news_seen WHERE last_seen < current_date - 60"))
    except Exception as e:  # noqa: BLE001
        log.info("память новостей: запись пропущена (%s)", e)


def mark_published(urls: list[str]) -> None:
    """Отметка «вышло в выпуске» — зовёт writer.news после успешного отбора.
    Опубликованное в прошлые дни _filter_seen больше в пул не пускает."""
    if not urls:
        return
    try:
        from sqlalchemy import bindparam, text as _t
        from .. import db
        q = _t("""UPDATE digest_news_seen SET picked = TRUE, picked_at = now()
                   WHERE url_hash IN :hs""").bindparams(
            bindparam("hs", expanding=True))
        with db.session() as s:
            s.execute(q, {"hs": [_url_hash(u) for u in urls]})
    except Exception as e:  # noqa: BLE001
        log.warning("память новостей: отметка публикации не записана: %s", e)


def fetch_all() -> tuple[list[dict], list[dict]]:
    """Параллельный сбор всех источников. Возвращает (items, sources_status).
    Конвейер пула: окно свежести → дедуп заголовков → смысловой дедуп →
    межднёвная память → сборка по квотам (регуляторика с приоритетом решений,
    потолок общих лент и недатированных)."""
    tasks = [*SOURCES]
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_fetch_source, tasks))
    items = [it for r, _ in results for it in r]
    statuses = [st for _, st in results]
    s_items, s_status = _fetch_search()
    items += s_items
    statuses.append(s_status)
    items = _resolve_undated(items)   # недатированное датируем или выбрасываем

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=_WINDOW_H)
    cutoff_reg = now_utc - timedelta(hours=_WINDOW_REG_H)
    fresh = []
    for it in items:
        ts = it.get("ts")
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            # Регуляторике окно шире. Совет директоров ЦБ заседает по пятницам
            # около 13:30, и при общем окне 48 ч решение по ключевой ставке к
            # утру понедельника уже старше окна — выпадало ровно в тот день,
            # когда новая ставка вступает в силу (проверено 27.07.2026: 67.8 ч).
            if ts < (cutoff_reg if it.get("tag") == "regulator" else cutoff):
                continue
            it["ts"] = ts
        fresh.append(it)

    fresh = _dedupe(fresh)
    fresh = _sem_dedupe(fresh)     # рерайты агентств: одно событие N формулировок
    fresh = _filter_seen(fresh)    # публиковалось раньше / трижды отвергнуто
    _newest = lambda x: x["ts"] or datetime.min.replace(tzinfo=timezone.utc)  # noqa: E731

    # решение vs текучка — по каждой новости (для приоритета в рег-квоте)
    for it in fresh:
        blob = (it.get("title") or "") + " " + (it.get("snippet") or "")[:200]
        it["decision"] = bool(_DECISION_RE.search(blob))
        it["routine"] = (not it["decision"]
                         and bool(_ROUTINE_RE.search(it.get("title") or "")))

    # ── сборка пула по квотам (не чистой свежестью) ──
    # Регуляторика: решения → не-текучка → текучка, внутри — по свежести.
    # Квота нужна, чтобы пятничное решение ЦБ доживало до понедельника
    # (проверено 27.07.2026: 68 ч при окне 96 ч — чистая свежесть его резала).
    reg = sorted((i for i in fresh if i.get("tag") == "regulator"),
                 key=lambda i: (i["decision"], not i["routine"], _newest(i)),
                 reverse=True)[:_REG_QUOTA]
    taken = {id(i) for i in reg}          # по тождеству: одинаковые словари ≠ один элемент
    rest = sorted((i for i in fresh if id(i) not in taken), key=_newest, reverse=True)

    def _interleave(pool: list[dict], per_src_cap: int | None = None) -> list[dict]:
        """Round-robin по источникам (внутри источника — по свежести): без него
        частопишущие душили остальных чистой свежестью — замер 05.08.2026:
        banksta забрал 10 мест класса bank, РИА — 7 из 8 мест класса gen."""
        by_src: dict[str, list[dict]] = {}
        for it in pool:
            by_src.setdefault(it["source"], []).append(it)
        out, rnd = [], 0
        while True:
            layer = [lst[rnd] for lst in by_src.values() if rnd < len(lst)]
            if not layer or (per_src_cap is not None and rnd >= per_src_cap):
                break
            layer.sort(key=_newest, reverse=True)
            out.extend(layer)
            rnd += 1
        return out

    # текучка/юбилеи ЦБ (routine) за общие места не конкурируют: их место —
    # только хвост рег-квоты (иначе «Редкие монеты» возвращались через класс bank)
    bank = _interleave([i for i in rest
                        if i.get("cls") != "gen" and not i.get("routine")])
    gen = _interleave([i for i in rest if i.get("cls") == "gen"], per_src_cap=3)
    room = max(0, MAX_ITEMS - len(reg))
    take_bank = bank[:max(0, room - min(_GEN_CAP, room))]
    # общих — не больше потолка ДАЖЕ при недоборе банковских: пул лучше короче,
    # чем добитый общеполитическим шумом (ровно с него начался мусор в выпусках)
    take_gen = gen[:min(_GEN_CAP, room - len(take_bank))]
    fresh = sorted(reg + take_bank + take_gen, key=_newest, reverse=True)

    _record_pool(fresh)
    for it in fresh:                       # ts → isoformat для jsonb
        it["title"] = _EMOJI_RE.sub("", it["title"]).strip() or it["title"]
        it["ts"] = it["ts"].isoformat() if it.get("ts") else None
        try:
            it["domain"] = urlparse(it["url"]).netloc.replace("www.", "")
        except Exception:  # noqa: BLE001
            it["domain"] = ""
    pool_by_src: dict[str, int] = {}
    for it in fresh:
        pool_by_src[it["source"]] = pool_by_src.get(it["source"], 0) + 1
    for st in statuses:                    # прозрачность: сколько дожило до пула
        st["in_pool"] = pool_by_src.get(st["name"], 0)
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


# Ставка меняется 8 раз в год, но именно в эти дни она и нужна. Час — компромисс:
# ЦБ не нагружаем (24 запроса в сутки), а отставание экрана не превышает часа.
# Раньше здесь было 6 часов, и 27.07.2026 это стоило суток неверной ставки на
# главной: в 07:00 ЦБ ещё отдавал вчерашнее значение, а перечитать было некому.
_KEYRATE_TTL_S = int(os.getenv("KEYRATE_TTL_S", "3600"))


def _store_key_rate(points: list[dict]) -> None:
    """Сохраняет точки в cbr_key_rate. Ошибка не должна ломать выдачу ставки."""
    if not points:
        return
    try:
        from .. import db
        from sqlalchemy import text as _t
        with db.session() as s:
            for p in points:
                s.execute(_t("""
                    INSERT INTO cbr_key_rate(rate_date, rate) VALUES (:d, :r)
                    ON CONFLICT (rate_date) DO UPDATE
                       SET rate = EXCLUDED.rate, fetched_at = now()
                """), {"d": p["date"], "r": p["rate"]})
    except Exception as e:  # noqa: BLE001
        log.info("не сохранил историю ставки: %s", e)


def key_rate_from_db(months: int = 6) -> dict | None:
    """Последнее известное значение из БД — на случай, когда ЦБ недоступен.

    Раньше при недоступности SOAP функция возвращала None, и экран оставался
    вовсе без ставки. Показать последнее известное значение с честной датой
    полезнее, чем пустое место.
    """
    try:
        from .. import db
        from sqlalchemy import text as _t
        with db.session() as s:
            rows = s.execute(_t("""
                SELECT rate_date::text d, rate::float r FROM cbr_key_rate
                 WHERE rate_date <= current_date
                   AND rate_date > current_date - make_interval(months => :m)
                 ORDER BY rate_date
            """), {"m": months}).all()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    points = [{"date": d, "rate": r} for d, r in rows]
    return {"current": points[-1]["rate"], "as_of": points[-1]["date"],
            "points": points, "from_db": True}


def fetch_key_rate(months: int = 6) -> dict | None:
    """История ключевой ставки за N месяцев: {current, points:[{date,rate}]}.

    Порядок: свежий кэш → SOAP ЦБ (с записью истории в БД) → последнее
    известное из БД. Последняя ступень важна: без неё недоступность ЦБ
    оставляла экран вообще без ставки.
    """
    from ..rag import cache as rag_cache
    cached = rag_cache.get("digest_keyrate", months)
    if cached:
        return cached
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    to = now.strftime("%Y-%m-%d")
    try:
        r = httpx.post(_KEYRATE_URL,
                       content=_KEYRATE_ENVELOPE.format(frm=frm, to=to).encode(),
                       timeout=_TIMEOUT,
                       headers={"Content-Type": "text/xml; charset=utf-8",
                                "SOAPAction": '"http://web.cbr.ru/KeyRate"',
                                "User-Agent": _UA})
    except Exception as e:  # noqa: BLE001 — сеть до ЦБ не должна ронять выдачу
        log.info("ЦБ недоступен (%s), беру ставку из БД", type(e).__name__)
        return key_rate_from_db(months)
    if r.status_code != 200:
        return key_rate_from_db(months)
    points = []
    for dt_raw, rate_raw in _KR_ROW_RE.findall(r.text):
        try:
            points.append({"date": dt_raw.strip()[:10],
                           "rate": float(rate_raw.strip().replace(",", "."))})
        except ValueError:
            continue
    if not points:
        return key_rate_from_db(months)
    points.sort(key=lambda p: p["date"])
    _store_key_rate(points)
    out = {"current": points[-1]["rate"], "as_of": points[-1]["date"], "points": points}
    try:
        rag_cache.put("digest_keyrate", out, _KEYRATE_TTL_S, months)
    except Exception:  # noqa: BLE001
        pass
    return out
