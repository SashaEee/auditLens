"""Период «Аудита отзывов» (rag/review_period.py): готовые периоды, сравнение,
выравнивание по полноте данных и начало корпуса. БД не нужна: горизонт данных
передаётся явно."""
from __future__ import annotations

from datetime import date, datetime

from bank_audit.rag import review_period as RP

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=RP.MSK)
H = datetime(2026, 10, 15, tzinfo=RP.MSK)          # данные полные по 14.10


def P(*a, **k):
    k.setdefault("now", NOW)
    k.setdefault("horizon", H)
    return RP.parse(*a, **k)


def test_rolling_aligned_to_full_days():
    p = P("90d")
    i = p.info()
    assert (i["from"], i["to"], i["days"]) == ("2026-07-18", "2026-10-15", 90)
    # сегодняшний недособранный день в сравнение не идёт — и такой же кусок прошлого
    assert i["aligned_to"] == "2026-10-14"
    assert (i["cmp_from"], i["cmp_to"]) == ("2026-04-19", "2026-07-16")
    assert abs(p.k - 1) < 1e-9 and not i["per_day"]
    assert p.delta_pct(110, 100) == 10.0


def test_legacy_days_is_rolling():
    assert P(days=180).info()["from"] == P("180d").info()["from"]
    assert P().n == 90                                  # без параметров — 90 дней


def test_twelve_months_clipped_by_data_start():
    """Прошлое окно «12 мес» начинается в октябре 2024, данных там нет: сравнение
    в пересчёте на день по покрытой части (было: −10% вместо ≈ −29%)."""
    p = P("365d")
    i = p.info()
    assert i["cmp_from"] == "2025-01-01" and i["per_day"]
    assert 0.78 < p.cover < 0.8
    assert abs(p.k - 364 / 287) < 1e-9
    assert p.delta_pct(9235, 10219) < -25            # то, что показывалось как −9,6%


def test_month_against_previous_month_per_day():
    p = P(date_from="2026-09-01", date_to="2026-09-30")
    i = p.info()
    assert p.kind == "month" and not p.partial
    assert (i["cmp_from"], i["cmp_to"]) == ("2026-08-01", "2026-08-31")
    assert "aligned_to" not in i and abs(p.k - 30 / 31) < 1e-9


def test_month_to_date_against_same_days():
    """«Этот месяц»: 1–14 октября против 1–14 сентября, не против всего сентября."""
    p = P("mtd")
    i = p.info()
    assert p.kind == "month" and p.partial
    assert i["aligned_to"] == "2026-10-14"
    assert (i["cmp_from"], i["cmp_to"]) == ("2026-09-01", "2026-09-14")
    same = P(date_from="2026-10-01", date_to="2026-10-15")      # то же датами
    assert same.key == p.key


def test_short_previous_month_is_per_day():
    now = datetime(2027, 3, 30, 12, tzinfo=RP.MSK)
    p = RP.parse("mtd", now=now, horizon=datetime(2027, 3, 30, tzinfo=RP.MSK))
    i = p.info()
    assert (i["cmp_from"], i["cmp_to"]) == ("2027-02-01", "2027-02-28")
    assert abs(p.k - 29 / 28) < 1e-9


def test_quarter_and_year_shapes():
    q = P(date_from="2026-07-01", date_to="2026-09-30")
    assert q.kind == "quarter" and q.info()["cmp_from"] == "2026-04-01"
    y = P("ytd")
    i = y.info()
    assert y.kind == "ytd" and y.cmp == "yoy"
    assert (i["cmp_from"], i["cmp_to"]) == ("2025-01-01", "2025-10-14")


def test_no_comparison_before_data():
    y = P(date_from="2025-01-01", date_to="2025-12-31")
    assert y.kind == "year" and not y.has_cmp
    assert y.info()["cmp_note"] == "data" and y.delta_pct(10, 5) is None
    assert not P("all").has_cmp and P("all").info()["cmp_note"] == "off"
    assert P("30d", cmp="none").info()["cmp_note"] == "off"


def test_custom_range_and_clamps():
    p = P(date_from="2026-08-16", date_to="2026-08-10")          # перепутаны местами
    i = p.info()
    assert (i["from"], i["to"], p.kind) == ("2026-08-10", "2026-08-16", "range")
    assert (i["cmp_from"], i["cmp_to"]) == ("2026-08-03", "2026-08-09")
    early = P(date_from="2023-05-01", date_to="2025-03-31")
    assert early.info()["from"] == RP.DATA_START.isoformat()
    future = P(date_from="2026-10-01", date_to="2026-12-31")
    assert future.info()["to"] == "2026-10-15"


def test_year_ago_comparison():
    p = P(date_from="2026-09-01", date_to="2026-09-30", cmp="yoy")
    i = p.info()
    assert (i["cmp_from"], i["cmp_to"]) == ("2025-09-01", "2025-09-30")
    assert P("30d", cmp="yoy").has_cmp
    assert not P(date_from="2025-03-01", date_to="2025-03-31", cmp="yoy").has_cmp


def test_binds_are_naive_msk():
    b = P("30d").binds()
    assert all(v.tzinfo is None for v in b.values())
    assert b["t0"] == datetime(2026, 9, 16) and b["t1"] == datetime(2026, 10, 16)
    assert P(date_from="2026-09-01", date_to="2026-09-30").key != P("30d").key
    assert date(2026, 9, 16) == P("30d").t0.date()


def test_any_rolling_days():
    assert P("45d").n == 45 and P("45d").info()["from"] == "2026-09-01"
    assert P("bogus").n == 90


def test_slices_start_with_their_labeling():
    """У срезов данные — с начала их разметки: «12 мес» по зарплатным проектам
    не сравниваются с почти пустым прошлым годом."""
    from bank_audit.rag import reviews_dash as RD
    ds = RD.data_start_for("Зарплатные проекты")
    assert ds.isoformat() == "2025-09-01" and RD.data_start_for("Вклады") == RP.DATA_START
    p = P("365d", data_start=ds)
    assert not p.has_cmp and p.info()["cmp_note"] == "data"


def test_endpoints_take_period(monkeypatch):
    """Эндпоинты вкладки: p / from-to / cmp, старый days=N, лента без периода — весь корпус."""
    import os
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from fastapi.testclient import TestClient

    from bank_audit.rag import reviews_dash as RD
    from bank_audit.web import app as A
    seen = {}
    monkeypatch.setattr(RD, "overview", lambda b, pr=None, days=90, period=None: seen.update(ov=period) or {})
    monkeypatch.setattr(RD, "list_reviews_ex", lambda *a, **k: seen.update(feed=k.get("period")) or
                        {"items": [], "mode": "feed", "error": None})
    monkeypatch.setattr(RP, "data_horizon", lambda now=None: None)
    c = TestClient(A.app)
    h = {"X-Authentik-Username": "u1", "X-Authentik-Name": "u1"}
    c.get("/api/reviews/overview?p=30d", headers=h)
    assert seen["ov"].n == 30
    c.get("/api/reviews/overview?from=2026-09-01&to=2026-09-30&cmp=yoy", headers=h)
    assert seen["ov"].kind == "month" and seen["ov"].cmp == "yoy"
    c.get("/api/reviews/overview?days=180", headers=h)
    assert seen["ov"].n == 180
    c.get("/api/reviews/feed?bank=Сбербанк", headers=h)
    assert seen["feed"] is None
    c.get("/api/reviews/feed?bank=Сбербанк&p=7d", headers=h)
    assert seen["feed"].n == 7
