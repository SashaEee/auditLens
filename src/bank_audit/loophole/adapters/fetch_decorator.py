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

import logging
import os
import re
from dataclasses import dataclass
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

_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\\-]+)", re.I)


def _normalize_to_utf8(content: bytes, content_type: str | None) -> bytes:
    """Перекодирует контент в UTF-8 по charset из Content-Type или <meta>.

    Оставляет как есть: пустой контент, уже-UTF-8, и текстовые форматы
    (json/txt), которые rag-парсеры сами декодируют как UTF-8.
    """
    if not content:
        return content
    declared: str | None = None
    if content_type:
        m = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type, re.I)
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
    return FetchedPage(
        url=url,
        final_url=getattr(result, "final_url", url),
        status=getattr(result, "status", 0),
        text=text,
        title=title,
        excerpt=excerpt,
        via=getattr(result, "via", "unknown"),
        content_type=content_type,
    )
