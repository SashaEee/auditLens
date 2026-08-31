"""Сверка чисел отчёта — против ФАКТОВ, а не против кучи прочитанных страниц.

ЗАЧЕМ. Первая версия сверяла числа отчёта с числами склейки ВСЕХ прочитанных
страниц и сравнивала голые float без класса единицы. В корпусе из восьмидесяти
страниц банковских тарифов найдётся почти любое двузначное и трёхзначное
число, поэтому «сверено 100%» получалось всегда и не доказывало ничего:
выдуманная «комиссия 1,5%» подтверждалась строкой «лимит 1,5 млн ₽» с другой
страницы. Аудитору показывали зелёную плашку на пустом месте.

Здесь используется numbers.audit_report_numbers — та же сверка, что писалась
для критика конвейера v2: она знает класс единицы (процент это не рубль и не
год), умеет производные величины и отличает «не сходится» от «пересчитать
нечем».
"""
from __future__ import annotations

import logging

from ..v2 import numbers as _num

log = logging.getLogger(__name__)


def verify_report(report: str, registry=None, pages: dict[str, str] | None = None) -> dict:
    """Разносит числа отчёта по корзинам достоверности.

    registry — реестр фактов прогона: основная база сверки. pages нужны только
    для запасного пути, когда фактов нет вовсе.
    """
    facts = list(getattr(registry, "facts", None) or [])
    if facts:
        res = _num.audit_report_numbers(report or "", facts)
        verified = list(res.get("verified") or []) + list(res.get("derived_ok") or [])
        unverified = list(res.get("unverified") or [])
        manual = [v for v, _k in (res.get("derived_unchecked") or [])]
        checked = len(verified) + len(unverified) + len(manual)
        return {
            "numeric_checked": checked,
            "verified": len(verified),
            "unverified": sorted(set(unverified))[:40],
            "manual_check": sorted(set(manual))[:20],
            "removal_candidates": sorted(set(res.get("removal_candidates") or []))[:20],
            "ratio": (len(verified) / checked) if checked else 1.0,
            "база": "факты прогона",
        }

    # Фактов нет — сверяемся по прочитанным страницам и честно это помечаем:
    # такая проверка слабее, потому что не знает единиц.
    corpus = "\n".join((pages or {}).values())
    source_nums = _num.all_numbers(corpus)
    verified, unverified = _num.split_verified(
        _num.parse_with_units(report or ""), source_nums)
    checked = len(verified) + len(unverified)
    return {
        "numeric_checked": checked, "verified": len(verified),
        "unverified": sorted(set(unverified))[:40], "manual_check": [],
        "removal_candidates": [],
        "ratio": (len(verified) / checked) if checked else 1.0,
        "база": "прочитанные страницы (слабая проверка, без единиц)",
    }
