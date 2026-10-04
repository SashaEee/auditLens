"""Волна 5 «охват и удержание»."""
from __future__ import annotations

import os
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

T = date(2026, 10, 4)


def _days(*ago):
    return {T - timedelta(days=a) for a in ago}


def test_retention_segments_risk_churn_and_cohorts():
    from bank_audit.web.telemetry import _retention_calc
    daily = {
        "core": _days(*range(0, 20, 2)),            # 10 дней за 30 — ядро
        "reg": _days(1, 5, 9),                     # регулярный
        "once": _days(3),                          # разовый
        "slip": _days(10, 14, 20, 25, 33),          # регулярный, пропал 10 дн. назад
        "gone": _days(40, 45, 50),                 # был 1–2 мес. назад — ушёл
        "newbie": _days(20, 12),                   # пришёл 20 дн. назад, вернулся на 2-й неделе
    }
    first = {"core": T - timedelta(days=80), "reg": T - timedelta(days=90), "once": T - timedelta(days=3),
             "slip": T - timedelta(days=60), "gone": T - timedelta(days=50), "newbie": T - timedelta(days=20)}
    r = _retention_calc(daily, first, T, last_page={"slip": "reviews"}, ai={"slip": 4})
    assert r["segments"] == {"core": 1, "regular": 2, "once": 2, "churned": 1, "new": 2}
    assert [x["username"] for x in r["at_risk"]] == ["slip"]
    assert r["at_risk"][0]["gap_days"] == 10 and r["at_risk"][0]["page"] == "reviews"
    assert [x["username"] for x in r["churned"]] == ["gone"] and r["churned_total"] == 1
    nb = [c for c in r["cohorts"] if c["n"] and c["week"] <= (T - timedelta(days=20)).isoformat()]
    assert any(c["w2"] == 1 and c["w2_ready"] == 1 for c in nb)
    # неделя, которой ещё нет двух недель, — «рано», а не 0 %
    assert all(c["w2_ready"] == 0 for c in r["cohorts"] if c["week"] >= (T - timedelta(days=6)).isoformat())
