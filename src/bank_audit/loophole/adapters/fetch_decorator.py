"""Декоратор над bank_audit.rag.fetcher.fetch + rag.parsers.parse_auto.

Добавляет:
- сохранение raw-контента в loophole_record (passive persist) — опционально;
- извлечение excerpt (первые N символов текста);
- graceful fallback при ошибке fetch.

Делегирует в rag.fetcher.fetch — НЕ дублирует его логику.

Два адаптивных слоя (здесь, а не в rag.fetcher — см. правило loophole):
1. Санитизация CA_BUNDLE_PATH: dotenv подхватывает docker-путь
   (/app/config/...), которого нет на Windows-хосте → httpx падает с
   FileNotFoundError на любой HTTPS-запрос. Сбрасываем невалидный путь до
   импорта fetcher (он читает env при import).
2. Нормализация кодировки в UTF-8: rag-парсеры декодируют строго как UTF-8,
   а часть сайтов отдаёт windows-1251. Перекодируем по charset из
   Content-Type / <meta charset> — до parse_auto.
"""
from __future__ import annotations

import inspect
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _sanitize_ca_bundle_env() -> None:
    """Сбрасывает CA_BUNDLE_PATH, указывающий на несуществующий файл.

    Вызывается лениво в fetch_and_parse: config.load_dotenv срабатывает при
    импорте rag.fetcher (через cache → db → config) и может выставить
    docker-путь ПОСЛЕ импорта этого модуля. Если fetcher уже загружен —
    обновляем и его module-level CA_BUNDLE_PATH.
    """
    path = os.getenv("CA_BUNDLE_PATH")
    if path and not os.path.exists(path):
        log.warning(
            "[fetch_decorator] CA_BUNDLE_PATH=%r не существует — сбрасываю "
            "(dotenv подхватил docker-путь); будет системный bundle",
            path,
        )
        os.environ.pop("CA_BUNDLE_PATH", None)
        import sys
        mod = sys.modules.get("bank_audit.rag.fetcher")
        if mod is not None and getattr(mod, "CA_BUNDLE_PATH", None) == path:
            mod.CA_BUNDLE_PATH = None

_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_-]+)", re.IGNORECASE)
_PUBLISHED_AT_RES = (
    re.compile(r'"datePublished"\s*:\s*"([^"]{8,80})"', re.IGNORECASE),
    re.compile(
        r"<meta\b[^>]*(?:property|name|itemprop)=[\"']"
        r"(?:article:published_time|datePublished|datepublished|pubdate)[\"']"
        r"[^>]*\bcontent=[\"']([^\"']{8,80})",
        re.IGNORECASE,
    ),
    re.compile(
        r"<meta\b[^>]*\bcontent=[\"']([^\"']{8,80})[\"']"
        r"[^>]*(?:property|name|itemprop)=[\"']"
        r"(?:article:published_time|datePublished|datepublished|pubdate)[\"']",
        re.IGNORECASE,
    ),
    re.compile(r"<time\b[^>]*\bdatetime=[\"']([^\"']{8,80})", re.IGNORECASE),
)


def _normalize_to_utf8(content: bytes, content_type: str | None) -> bytes:
    """Перекодирует контент в UTF-8 по charset из Content-Type или <meta>.

    Оставляет как есть: пустой контент, уже-UTF-8, и текстовые форматы
    (json/txt), которые rag-парсеры сами декодируют как UTF-8.
    """
    if not content:
        return content
    declared: str | None = None
    if content_type:
        m = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type, re.IGNORECASE)
        if m:
            declared = m.group(1)
    ct = (content_type or "").lower()
    is_html = not ct or "html" in ct or "xml" in ct
    if not declared and is_html:
        m = _CHARSET_RE.search(content[:8192])
        if m:
            try:
                declared = m.group(1).decode("ascii", errors="ignore")
            except Exception:
                declared = None
    if not declared:
        return content
    enc = declared.lower().replace("windows1251", "windows-1251")
    if enc.startswith("utf"):
        return content
    try:
        return content.decode(enc, errors="replace").encode("utf-8")
    except (LookupError, ValueError):
        log.info("[fetch_decorator] неизвестный charset %r — оставляю как есть", declared)
        return content


def _exact_published_at(content: bytes) -> str | None:
    """Извлекает timestamp публикации первоисточника из разметки.

    tz-aware значение возвращается как есть; наивное (без пояса) якорится
    к UTC — календарный день сохраняется. Дата в сниппете поиска и оценка
    из URL не подходят: здесь считается только разметка самого поста.
    """
    markup = content.decode("utf-8", errors="replace")
    for pattern in _PUBLISHED_AT_RES:
        match = pattern.search(markup)
        if match is None:
            continue
        raw = match.group(1).strip()
        normalized = raw.removesuffix("Z") + ("+00:00" if raw.endswith("Z") else "")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    return None


# ── Оценочная дата публикации (мягкий period-фильтр) ────────────────────────
# Иерархия: точный tz-aware timestamp → оценка из URL → оценка из текста.
# Оценка не подтверждает дату, но позволяет не отсекать источник fail-closed.
# Словарь единый для loophole: tools_nanobot импортирует его отсюда.
RU_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}
# Стебель «ма\w*» матчил «машин»/«магазинов»: у мая перечислены явные окончания.
RU_MONTH_PATTERN = (
    "январ\\w*|феврал\\w*|март\\w*|апрел\\w*|ма[йяе]\\w*|июн\\w*|"
    "июл\\w*|август\\w*|сентябр\\w*|октябр\\w*|ноябр\\w*|декабр\\w*"
)
_URL_FULL_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/_](\d{1,2})[-/_](\d{1,2})(?!\d)")
_URL_MONTH_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-/_](\d{1,2})(?!\d)")
_TEXT_ISO_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)")
_TEXT_DMY_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})\b")
_TEXT_RU_DATE_RE = re.compile(
    rf"\b(\d{{1,2}})\s+({RU_MONTH_PATTERN})\s+((?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
_PUBLICATION_MARKER_RE = re.compile(
    r"опубликован\w*|дата\s+публикации|размещен\w*|published",
    re.IGNORECASE,
)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _plausible(found: date | None) -> date | None:
    """Отбрасывает даты в будущем: дата публикации не может быть позже сегодня."""
    if found is None or found > date.today():
        return None
    return found


def _first_date_in(fragment: str) -> date | None:
    """Первая правдоподобная дата во фрагменте: русские месяцы, dd.mm.yyyy, ISO."""
    match = _TEXT_RU_DATE_RE.search(fragment)
    if match is not None:
        month_word = match.group(2).lower()
        month = next(
            (number for stem, number in RU_MONTHS.items() if month_word.startswith(stem)),
            None,
        )
        if month is not None:
            found = _plausible(_safe_date(int(match.group(3)), month, int(match.group(1))))
            if found is not None:
                return found
    match = _TEXT_DMY_DATE_RE.search(fragment)
    if match is not None:
        found = _plausible(_safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1))))
        if found is not None:
            return found
    match = _TEXT_ISO_DATE_RE.search(fragment)
    if match is not None:
        return _plausible(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    return None


def _date_from_url(url: str) -> date | None:
    """Дата из пути URL: /2026/09/09/, 2026-09-09, /2026/09/."""
    match = _URL_FULL_DATE_RE.search(url)
    if match is not None:
        found = _plausible(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        if found is not None:
            return found
    match = _URL_MONTH_RE.search(url)
    if match is not None:
        return _plausible(_safe_date(int(match.group(1)), int(match.group(2)), 1))
    return None


def _date_from_text(text: str) -> date | None:
    """Дата из текста страницы: сначала рядом с маркерами публикации, затем первая."""
    sample = text[:20000]
    for marker in _PUBLICATION_MARKER_RE.finditer(sample):
        found = _first_date_in(sample[marker.start():marker.start() + 160])
        if found is not None:
            return found
    return _first_date_in(sample)


def estimate_published_date(url: str, text: str = "") -> str | None:
    """Оценочная дата публикации: сначала из URL, затем из текста страницы.

    Возвращает ISO-дату (YYYY-MM-DD) или None. Это не подтверждённый timestamp:
    источник с оценкой вне окна отклоняется, без любой даты — допускается
    с пометкой неподтверждённой даты.
    """
    found = _date_from_url(url or "")
    if found is None and text:
        found = _date_from_text(text)
    return found.isoformat() if found is not None else None


def published_date_from_text(text: str) -> str | None:
    """Текстовая дата публикации «YYYY-MM-DD» из видимого текста (URL не участвует).

    Обёртка над _date_from_text для мест, где страница не прочитана
    (например triaged-сниппет поисковой выдачи): будущее и битые строки
    отбрасываются молча (_plausible).
    """
    found = _date_from_text(text or "")
    return found.isoformat() if found is not None else None


def _text_published_at(text: str) -> str | None:
    """Точная дата из видимого текста поста: полночь UTC («…T00:00:00+00:00»).

    Fallback, когда в разметке дат не нашлось; будущие и битые даты дают None.
    """
    found = _date_from_text(text or "")
    if found is None:
        return None
    return datetime(found.year, found.month, found.day, tzinfo=timezone.utc).isoformat()


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    text: str
    title: str | None
    excerpt: str
    via: str
    content_type: str | None = None
    published_at: str | None = None
    estimated_published_at: str | None = None


def fetch_and_parse(
    url: str,
    *,
    excerpt_len: int = 1000,
    prefer_browser: bool = False,
    _fetch_impl: Any = None,
) -> FetchedPage | None:
    """Fetch URL → parse → FetchedPage. Возвращает None при ошибке.

    _fetch_impl — инъекция для тестов (мок fetcher.fetch).
    """
    _sanitize_ca_bundle_env()
    fimpl = _fetch_impl
    if fimpl is None:
        from ...rag import fetcher
        fimpl = fetcher.fetch
    try:
        parameters = inspect.signature(fimpl).parameters.values()
        supports_direct = any(
            parameter.name == "direct" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if supports_direct:
            result = fimpl(url, prefer_browser=prefer_browser, direct=True)
        else:
            result = fimpl(url, prefer_browser=prefer_browser)
    except Exception as e:
        log.warning("[fetch_decorator] fetch failed %s: %s", url[:80], e)
        return None
    if result is None:
        return None
    content_type = getattr(result, "content_type", None)
    content = _normalize_to_utf8(getattr(result, "content", b"") or b"", content_type)
    try:
        from ...rag.parsers import parse_auto
        doc = parse_auto(content, url=url, content_type=content_type)
        text = doc.text or ""
        title = doc.title
    except Exception as e:
        log.warning("[fetch_decorator] parse failed %s: %s", url[:80], e)
        text = content.decode("utf-8", errors="replace")[:excerpt_len * 4]
        title = None
    excerpt = text[:excerpt_len]
    final_url = getattr(result, "final_url", url)
    estimated = estimate_published_date(final_url, text) or estimate_published_date(url)
    # Приоритет: tz-aware/наивная разметка (UTC) → видимая дата текста (полночь UTC).
    published_at = _exact_published_at(content) or _text_published_at(text)
    return FetchedPage(
        url=url,
        final_url=final_url,
        status=getattr(result, "status", 0),
        text=text,
        title=title,
        excerpt=excerpt,
        via=getattr(result, "via", "unknown"),
        content_type=content_type,
        published_at=published_at,
        estimated_published_at=estimated,
    )
