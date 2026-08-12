"""Хелпер регистрации источников в bundle.

Вынесен отдельно чтобы tools могли писать в SourceRegistry не зная деталей
(там есть bank_slug resolution и нормализация URL).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..knowledge_bundle import Source

log = logging.getLogger(__name__)


def register_source(bundle, *, url: str, title: str, domain: str,
                     trust: float, kind: str, excerpt: str = "",
                     bank_slug: str | None = None) -> int:
    """Регистрирует источник в bundle.sources и возвращает его n-маркер [N].

    bank_slug уточняется по домену если не передан.
    """
    if not url:
        return 0
    if not bank_slug:
        bank_slug = _bank_slug_from_domain(domain)
    src = Source(
        url=url, title=title or url[:80], domain=domain,
        bank_slug=bank_slug, trust=trust, kind=kind, excerpt=excerpt,
    )
    return bundle.sources.add(src)


_BANK_DOMAIN_MAP = {
    "sberbank.ru": "sberbank", "sberbank.com": "sberbank",
    "vtb.ru": "vtb", "alfabank.ru": "alfabank",
    "tbank.ru": "tinkoff", "tinkoff.ru": "tinkoff",
    "sovcombank.ru": "sovcombank", "gazprombank.ru": "gazprombank",
    "rshb.ru": "rshb", "domrfbank.ru": "domrf", "open.ru": "otkritie",
    "raiffeisen.ru": "raiffeisen", "pochtabank.ru": "pochtabank",
    "mkb.ru": "mkb", "psbank.ru": "psb", "rosbank.ru": "rosbank",
    "mtsbank.ru": "mtsbank", "bank.yandex.ru": "yandexbank",
}


def _bank_slug_from_domain(domain: str) -> str | None:
    """Слаг банка по домену.

    Поддомен, ведущий чужой бизнес (hotels.tinkoff.ru — бронирование отелей),
    банку НЕ приписываем: иначе он становится «источником о Т-Банке» и попадает
    в отчёт про автокредиты.
    """
    d = (domain or "").lower().removeprefix("www.")
    from .web_tools import is_offtopic_source
    if is_offtopic_source(d):
        return None
    for dom, slug in _BANK_DOMAIN_MAP.items():
        if d == dom or d.endswith("." + dom):
            return slug
    return None
