"""Ключи псевдонимов банков сравниваются в нормализованном виде (дефис → пробел):
ключ с дефисом никогда не сработает — так было с «А7-финансы ПСБ»."""
from bank_audit.rag import bankiru_reviews as b
from bank_audit.rag.bankiru_fts import BANK_RENAMES, canon_bank


def test_alias_keys_are_normalized():
    assert [k for k in b._ALIAS if b._norm(k) != k] == []


def test_renames_point_forward():
    assert canon_bank("Точка") == "Точка Банк"
    assert canon_bank("Сбербанк") == "Сбербанк"
    assert "Почта Банк" not in BANK_RENAMES   # слияние с ВТБ, не переименование
