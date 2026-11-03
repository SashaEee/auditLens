"""Выбросы ставок: условная/устаревшая ставка агрегатора, поданная как общая
(03.11.2026 — «Выгодный старт +» Сбера 19% при линейке ~12,65%)."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.quality import checks as C  # noqa: E402


def test_outlier_against_own_bank_line():
    why = C.outlier_reason(19.0, 12.65, 9, 14.42)
    assert why.startswith("ставка 19% на 6,35 п. п. выше остальных продуктов банка (медиана 12,65%)")
    assert "сверьте с сайтом банка" in why


def test_outlier_against_market_when_bank_line_is_short():
    # у банка два продукта — своей линейке не верим, но выше рынка на 2+ п. п. — сомнение
    assert C.outlier_reason(17.0, 16.0, 2, 14.4).startswith("ставка 17% выше 95% предложений рынка (14,4%)")
    assert C.outlier_reason(15.0, 14.0, 2, 14.4) is None


def test_normal_top_rates_pass():
    assert C.outlier_reason(13.5, 12.65, 9, 14.42) is None      # лучший вклад Сбера
    assert C.outlier_reason(16.0, 14.35, 6, 14.42) is None      # на 1,65 п. п. выше линейки
