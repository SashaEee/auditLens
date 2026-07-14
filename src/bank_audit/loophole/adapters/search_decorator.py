"""Декоратор над bank_audit.rag.web_search.search.

Добавляет:
- явный site_filter (whitelist доменов) — проброс в search (уже поддерживается);
- нормализацию результатов (title/url/snippet/domain);
- опциональный кеш по (query, site_filter) в памяти процесса (TTL).

Делегирует в rag.web_search.search — НЕ дублирует его логику.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_CACHE_TTL = 600  # 10 минут


def search(
    query: str,
    *,
    max_results: int = 10,
    site_filter: list[str] | None = None,
    region: str = "ru-ru",
    cache_ttl_seconds: int = _CACHE_TTL,
    _impl: Any = None,
) -> list[dict]:
    """Обёртка над rag.web_search.search с кешем и нормализацией.

    _impl — инъекция для тестов (мок); по умолчанию rag.web_search.search.
    """
    impl = _impl
    if impl is None:
        from ...rag import web_search
        impl = web_search.search
    if not query or not query.strip():
        return []
    key = (query, tuple(sorted(site_filter or [])), max_results, region)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < cache_ttl_seconds:
        return cached[1][:max_results]
    try:
        results = impl(
            query,
            max_results=max_results,
            site_filter=site_filter,
            region=region,
        )
    except TypeError:
        # Старая сигнатура без region.
        results = impl(query, max_results=max_results, site_filter=site_filter)
    results = [_normalize(r) for r in (results or [])]
    _CACHE[key] = (now, results)
    return results[:max_results]


def _normalize(r: dict) -> dict:
    return {
        "title": str(r.get("title") or "")[:300],
        "url": str(r.get("url") or ""),
        "snippet": str(r.get("snippet") or "")[:600],
        "domain": str(r.get("domain") or ""),
    }


def clear_cache() -> None:
    _CACHE.clear()
