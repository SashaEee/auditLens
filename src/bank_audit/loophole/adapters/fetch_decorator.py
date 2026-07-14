"""Декоратор над bank_audit.rag.fetcher.fetch + rag.parsers.parse_auto.

Добавляет:
- сохранение raw-контента в loophole_record (passive persist) — опционально;
- извлечение excerpt (первые N символов текста);
- graceful fallback при ошибке fetch.

Делегирует в rag.fetcher.fetch — НЕ дублирует его логику.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


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
    content = getattr(result, "content", b"") or b""
    content_type = getattr(result, "content_type", None)
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
