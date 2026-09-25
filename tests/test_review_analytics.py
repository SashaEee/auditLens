"""Тесты аналитики вкладки «Отзывы» (волна 3): статистика и фильтры.

Индекс «банк против рынка», география и значимость изменений тем держатся на
трёх чистых функциях — отношение долей с интервалом, изменение счётчика и
наполненность месяца события. Фильтр признаков риска подставляет код в SQL,
поэтому белый список обязан отвергать всё незнакомое. БД не нужна.
"""
import datetime as dt

import pytest

from bank_audit.rag.reviews_dash import (
    _flag_sql, _month_clause, _month_completeness, _rate_change, _ratio_ci, flag_label,
)


def test_ratio_ci_equal_shares_not_significant():
    r = _ratio_ci(50, 1000, 500, 10000)
    assert r["rr"] == pytest.approx(1.0)
    assert r["lo"] < 1 < r["hi"]
    assert r["p"] > 0.5


def test_ratio_ci_doubled_share_significant():
    # 9,5% жалоб банка против 4,2% у рынка — как «одностороннее изменение условий»
    r = _ratio_ci(417, 4412, 1795, 42737)
    assert r["rr"] == pytest.approx(2.25, rel=0.01)
    assert r["lo"] > 1.9 and r["p"] < 1e-6


def test_ratio_ci_zero_is_finite():
    r = _ratio_ci(0, 100, 30, 1000)
    assert r is not None and r["hi"] < float("inf")
    assert _ratio_ci(5, 0, 5, 10) is None


def test_rate_change_small_counts_wide_interval():
    # «Кредитные каникулы: 99 против 56» — рост есть, но интервал широкий
    ch = _rate_change(99, 56)
    assert ch["lo"] > 0 and ch["hi"] > 100
    assert _rate_change(0, 10) is None


def test_month_clause_publication_and_event():
    p: dict = {}
    assert "i.dt" in _month_clause("i", "2026-05", p) and p["month"] == "2026-05"
    p = {}
    assert "i.ev_date" in _month_clause("i", "ev:2026-05", p) and p["month"] == "2026-05"
    assert _month_clause("i", None, {}) == ""
    # мусор в параметре — пустой срез, а не весь корпус и не ошибка SQL
    assert _month_clause("i", "2026-05'; drop", {}) == " AND false"


@pytest.mark.parametrize("flag", ["to:cbr", "to:fas", "vuln:any", "vuln:svo", "esc:filed",
                                  "no_consent", "misled", "amount", "amount:1m"])
def test_flag_whitelist_known(flag):
    assert _flag_sql(flag)
    assert flag_label(flag)


@pytest.mark.parametrize("flag", ["to:x", "vuln:'; drop table review", "amount:5", "esc:none", "bogus"])
def test_flag_whitelist_rejects_unknown(flag):
    assert _flag_sql(flag) is None
    assert flag_label(flag) is None


def test_no_flag_means_no_filter():
    assert _flag_sql(None) == "" and _flag_sql("") == ""


def test_month_completeness_grows_with_time():
    # задержка публикации: половина в тот же день, остальное равномерно за 100 дней
    cdf = [min(1.0, 0.5 + 0.005 * k) for k in range(731)]
    fresh = _month_completeness("2026-09", dt.date(2026, 9, 25), cdf)
    older = _month_completeness("2026-06", dt.date(2026, 9, 25), cdf)
    old = _month_completeness("2025-06", dt.date(2026, 9, 25), cdf)
    assert fresh < older < old == pytest.approx(1.0)
