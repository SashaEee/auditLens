"""ReviewQuery — срез «Аудита отзывов» одним объектом (reviews_dash): одно условие для
ленты, выгрузки, отбора поиска и групп, одна ссылка для ИИ (план 04.11, К1)."""
from __future__ import annotations

import os
from urllib.parse import parse_qs

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import review_period as RP  # noqa: E402
from bank_audit.rag import reviews_dash as RD  # noqa: E402


def _qs(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(url.split("?", 1)[1]).items()} if "?" in url else {}


def test_where_sql_pending_theme_flag():
    p: dict = {}
    w = RD.ReviewQuery(bank="Б").where_sql(p)
    assert "(i.kind IS NULL OR" in w and p["bank"] == "Б"           # свежие неразмеченные — в ленте
    p = {}
    w = RD.ReviewQuery(bank="Б", theme="chargeback").where_sql(p)
    assert "i.issue = :tkey" in w and "i.kind IS NULL" not in w and p["tkey"] == "chargeback"
    p = {}
    w = RD.ReviewQuery(bank="Б", flag="no_consent").where_sql(p)
    assert "EXISTS (SELECT 1 FROM review_annotation a" in w and "i.kind IS NULL" not in w and "sv" in p
    p = {}
    assert "i.kind IS NULL" not in RD.ReviewQuery(bank="Б").where_sql(p, pending=False)


def test_where_sql_window_month_city_bank():
    per = RP.parse("90d")
    p: dict = {}
    w = RD.ReviewQuery(bank="Б", period=per, city="Омск", product="dir:field").where_sql(p)
    assert "i.dt >= :w0" in w and "i.city = :city" in w and "al_product_is" in w and p["product"] == "dir:field"
    p = {}
    w = RD.ReviewQuery(bank="Б", period=per, month="sig:2026-11-03").where_sql(p)
    assert ":w0" not in w and "sig_d" in p                       # у недели всплеска своё окно
    p = {}
    w = RD.ReviewQuery(bank="Б", period=per, city="Омск", month="ev:2026-10").where_sql(p, window=False, city=False)
    assert ":w0" not in w and "i.city" not in w and "ev_date" in w
    p = {}
    assert "i.bank" not in RD.ReviewQuery(bank=None).where_sql(p)     # поиск ИИ по всем банкам


def test_errors():
    assert RD.ReviewQuery(bank="Б", theme="nope").error() == "unknown_theme"
    assert RD.ReviewQuery(bank="Б", flag="nope").error() == "unknown_flag"
    assert RD.ReviewQuery(bank="Б", source="nope").error() == "unknown_source"
    assert RD.ReviewQuery(bank="Б", theme="chargeback").error() is None


def test_to_url_matches_tab_params():
    u = RD.ReviewQuery(bank="ВТБ", product="Ипотека", theme="chargeback", period=RP.parse("30d"),
                       esc=True, city="Омск", q="перевод").to_url()
    assert u.startswith("#reviews?") and _qs(u) == {
        "tab": "complaints", "bank": "ВТБ", "product": "Ипотека", "p": "30d", "theme": "chargeback",
        "esc": "1", "city": "Омск", "q": "перевод"}
    u = RD.ReviewQuery(bank="Сбербанк", theme="chargeback", month="sig:2026-11-03", period=RP.parse("90d")).to_url()
    assert _qs(u) == {"tab": "complaints", "theme": "chargeback", "month": "sig:2026-11-03"}
    u = RD.ReviewQuery(bank="Сбербанк", date_from="2026-10-01", date_to="2026-10-31").to_url()
    assert _qs(u) == {"from": "2026-10-01", "to": "2026-10-31"}
    assert RD.ReviewQuery(bank="Сбербанк").to_url() == "#reviews"
