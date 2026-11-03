"""Период из текста вопроса (обращения октября 2026: «запросил за полгода — смотрят 2024»)."""
from __future__ import annotations

from datetime import date

import pytest

from bank_audit.rag.question_period import anchor, extract

T = date(2026, 11, 3)
CASES = [
    ("Запросил исследование за полгода по кредиткам", date(2026, 5, 5), T),
    ("жалобы за последние 3 месяца", date(2026, 8, 6), T),
    ("за последние полгода", date(2026, 5, 5), T),
    ("с начала года", date(2026, 1, 1), T),
    ("в 2025 году", date(2025, 1, 1), date(2025, 12, 31)),
    ("за 2025–2026 годы", date(2025, 1, 1), T),
    ("продажи автокредитов 2025 и 2026", date(2025, 1, 1), T),
    ("во II квартале 2026", date(2026, 4, 1), date(2026, 6, 30)),
    ("за 3 кв. 2025", date(2025, 7, 1), date(2025, 9, 30)),
    ("с мая по октябрь 2026", date(2026, 5, 1), date(2026, 10, 31)),
    ("с ноября по март", date(2025, 11, 1), date(2026, 3, 31)),
    ("в октябре", date(2026, 10, 1), date(2026, 10, 31)),
    ("в декабре", date(2025, 12, 1), date(2025, 12, 31)),       # ещё не наступил — прошлый год
    ("за сентябрь 2025", date(2025, 9, 1), date(2025, 9, 30)),
    ("с 01.03.2026 по 15.04.2026", date(2026, 3, 1), date(2026, 4, 15)),
    ("за последнюю неделю", date(2026, 10, 28), T),
    ("за 90 дней", date(2026, 8, 6), T),
    ("последние два месяца", date(2026, 9, 5), T),
    ("первое полугодие 2026", date(2026, 1, 1), date(2026, 6, 30)),
]


@pytest.mark.parametrize("q,d0,d1", CASES)
def test_periods(q, d0, d1):
    p = extract(q, T)
    assert p is not None and (p.d0, p.d1) == (d0, d1), q


@pytest.mark.parametrize("q", ["сравни вклады Сбера и ВТБ", "Топ-5 банков по ставкам",
                               "Ипотека с господдержкой 2026", "Мошеннические схемы вокруг СБОЛ"])
def test_no_period(q):
    assert extract(q, T) is None


def test_anchor_text():
    assert anchor("сравни вклады") == ""
    a = anchor("жалобы на кредитки за полгода")
    assert a.startswith("ПЕРИОД ВОПРОСА: с ") and "«за полгода»" in a


def test_period_reaches_prompts_and_complaints(monkeypatch):
    """Период вопроса — в якоре даты всех агентов и в глубине поиска жалоб отчёта."""
    from bank_audit import clock
    from bank_audit.ai import hermes_quick
    from bank_audit.research.gptr import reviews
    from bank_audit.web import runctx
    runctx.set_origin(kind="report", username="u", question="жалобы на кредитки за полгода")
    assert "ПЕРИОД ВОПРОСА" in clock.today_anchor()
    assert 150 <= reviews._since_days() <= 200
    assert "ПЕРИОД ВОПРОСА" in hermes_quick.instructions(False, "жалобы за последние 3 месяца")
    runctx.set_origin(kind="report", username="u", question="сравни вклады Сбера и ВТБ")
    assert "ПЕРИОД ВОПРОСА" not in clock.today_anchor()
    assert reviews._since_days() == 540
