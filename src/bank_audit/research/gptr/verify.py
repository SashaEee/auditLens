"""Сверка чисел отчёта с прочитанными страницами.

Писатель gpt-researcher нигде не проверяет, что число из отчёта действительно
встречалось в источнике: он опирается на сжатый контекст и вполне может
округлить, сложить или перепутать. Для аудита это недопустимо — здесь каждое
число отчёта ищется среди чисел собранных страниц.

Используется наш модуль numbers: он разбирает число ВМЕСТЕ с единицей измерения
(год отличается от рублёвой суммы по единице, а не по величине) и умеет
сопоставлять совместимые классы величин.
"""
from __future__ import annotations

import logging

from ..v2 import numbers as _num
from .scraper import READ_PAGES

log = logging.getLogger(__name__)


def verify_report(report: str, pages: dict[str, str] | None = None) -> dict:
    """Разносит числа отчёта на сверенные и несверенные.

    Возвращает то же, что событие verification нашего конвейера: сколько чисел
    проверено, сколько подтвердилось и какие повисли без источника.
    """
    pages = READ_PAGES if pages is None else pages
    corpus = "\n".join(pages.values())
    source_nums = _num.all_numbers(corpus)
    report_pairs = _num.parse_with_units(report or "")
    verified, unverified = _num.split_verified(report_pairs, source_nums)
    checked = len(verified) + len(unverified)
    return {
        "numeric_checked": checked,
        "verified": len(verified),
        "unverified": sorted(set(unverified))[:40],
        "ratio": (len(verified) / checked) if checked else 1.0,
        "страниц_в_базе": len(pages),
    }


def sources_for(value: float, pages: dict[str, str] | None = None) -> list[str]:
    """Где именно встречается число — для показа аудитору рядом с цифрой."""
    pages = READ_PAGES if pages is None else pages
    return [url for url, text in pages.items()
            if value in _num.all_numbers(text)]
