"""Полный контент страницы-источника: единая точка получения и лимитирования.

Потребители:
- chat save_loophole — точка гарантии: серверный fetch, если агент не передал raw_text;
- collector — лимитирование уже скачанного page.text (limit_content);
- backfill-эндпоинт — догрузка legacy/fetch_failed/empty записей.

Сеть — только через adapters.fetch_decorator (наследуем таймауты fetcher'а:
connect 10s / read 30s, browser-fallback, parse_auto). Своих сетевых вызовов нет.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .adapters import fetch_decorator
from .config import LoopholeSettings

log = logging.getLogger(__name__)

STATUS_FULL = "full"
STATUS_TRUNCATED = "truncated"
STATUS_EMPTY = "empty"
STATUS_FAILED = "fetch_failed"
STATUS_LEGACY = "legacy"


@dataclass
class FullContent:
    text: str | None
    status: str
    length: int
    truncated: bool


def limit_content(text: str | None, *, max_chars: int) -> FullContent:
    """Чистая функция: применяет лимит к уже полученному тексту."""
    cleaned = (text or "").strip()
    if not cleaned:
        return FullContent(text=None, status=STATUS_EMPTY, length=0, truncated=False)
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        return FullContent(
            text=cut, status=STATUS_TRUNCATED, length=len(cut), truncated=True
        )
    return FullContent(
        text=cleaned, status=STATUS_FULL, length=len(cleaned), truncated=False
    )


def fetch_full_content(
    url: str,
    *,
    settings: LoopholeSettings | None = None,
    _fetch_impl: Any = None,
) -> FullContent:
    """Скачивает страницу и возвращает полный текст (с лимитом из конфига).

    Никогда не бросает исключений: при сбое — status=fetch_failed, text=None.
    """
    settings = settings or LoopholeSettings.load()
    try:
        page = fetch_decorator.fetch_and_parse(url, _fetch_impl=_fetch_impl)
    except Exception as e:
        log.warning("[content_fetch] fetch failed %s: %s", url[:80], e)
        return FullContent(text=None, status=STATUS_FAILED, length=0, truncated=False)
    if page is None:
        return FullContent(text=None, status=STATUS_FAILED, length=0, truncated=False)
    # Старые fetch-double могли отдавать только excerpt, поэтому не требуем
    # необязательный атрибут text и оставляем безопасный fallback.
    page_text = getattr(page, "text", None)
    if page_text is None:
        page_text = getattr(page, "excerpt", None)
    return limit_content(page_text, max_chars=settings.raw_text_max_chars)
