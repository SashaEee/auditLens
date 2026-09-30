"""Сборщики отзывов площадок и ночной сбор (аудит парсеров 30.09).

БД и сеть не нужны: справочники площадок и распознаватель банков подменены.
"""
from datetime import timedelta

import pytest

from bank_audit.digest import scheduler
from bank_audit.rag import bankiru_reviews as br
from bank_audit.sources import finuslugi_reviews as fin
from bank_audit.sources import review_streams as rs


def test_nightly_sources_exclude_review_streams():
    """review_streams пишет журнал дважды в сутки под именами banki_reviews и
    sravni_reviews — ночной сбор не должен принимать их за свой прогон."""
    nightly = scheduler.nightly_sources()
    assert "banki_reviews" not in nightly and "sravni_reviews" not in nightly
    assert "bankiros_reviews" not in nightly            # выключен 30.09
    assert "finuslugi_reviews" in nightly
    tariffs = scheduler._tariff_sources()
    assert "sravni_api" in tariffs
    assert not [s for s in tariffs if "review" in s]      # свежесть тарифов — не по отзывам


@pytest.mark.parametrize("n,key,ok", [
    ("рост банк", "т банк", False),          # WRatio 90 — было «Т-Банк»
    ("тинькоф банк", "т банк", False),
    ("пао сбербанк", "сбербанк", True),
    ("сбербанк россии", "сбербанк", True),
    ("втб 24", "втб", True),
    ("альфабанк", "альфа банк", True),        # опечатка/слитно — похоже целиком
])
def test_fuzzy_match_needs_whole_words(n, key, ok):
    assert br._fuzzy_ok(n, key) is ok


def _fake_resolver(monkeypatch, table: dict[str, str]):
    monkeypatch.setattr(br, "resolve_bank", lambda name: table.get(name))


def test_sravni_prefers_exact_name(monkeypatch):
    orgs = [{"id": "rost", "alias": "rost-bank", "name": "РОСТ БАНК"},
            {"id": "tb", "alias": "t-bank", "name": "Т-Банк"}]
    _fake_resolver(monkeypatch, {"РОСТ БАНК": "Т-Банк", "Т-Банк": "Т-Банк"})
    assert rs._sravni_match(orgs, "Т-Банк")["alias"] == "t-bank"


def test_sravni_picks_most_active_among_namesakes(monkeypatch):
    orgs = [{"id": "south", "alias": "uralsib-jug-bank", "name": "Уралсиб-Юг Банк "},
            {"id": "main", "alias": "uralsib", "name": "Банк Уралсиб"}]
    _fake_resolver(monkeypatch, {"Уралсиб-Юг Банк ": "Уралсиб", "Банк Уралсиб": "Уралсиб"})
    totals = {"south": (0, ""), "main": (1410, "2026-09-29")}
    monkeypatch.setattr(rs, "_sravni_total", lambda c, oid: totals[oid])
    monkeypatch.setattr(rs, "_PAUSE_S", 0)
    assert rs._sravni_match(orgs, "Уралсиб", c=object())["alias"] == "uralsib"
    # без клиента — единственный кандидат или первый (как раньше), но не падаем
    assert rs._sravni_match(orgs[:1], "Уралсиб")["alias"] == "uralsib-jug-bank"


def test_finuslugi_matches_renamed_banks(monkeypatch):
    companies = {"45": {"name": "Т-Банк"}, "368": {"name": "БСТ-БАНК"},
                 "30": {"name": "Банк ПСБ"}, "1": {"name": "Сбербанк"}}
    _fake_resolver(monkeypatch, {"Банк ПСБ": "ПСБ", "ПСБ": "ПСБ", "Т-Банк": "Т-Банк",
                                 "Сбербанк": "Сбербанк"})
    assert fin._match_company(companies, "Т-Банк") == "45"      # не «БСТ-БАНК»
    assert fin._match_company(companies, "ПСБ") == "30"
    assert fin._match_company(companies, "Сбербанк") == "1"


def test_finuslugi_time_is_moscow():
    d = fin._parse_date("2026-08-29 21:31:00")
    assert d.utcoffset() == timedelta(hours=3)
    assert d.astimezone(fin.timezone.utc).hour == 18
    assert fin._parse_date("1971-01-01 00:00:00") is None       # заглушка площадки
