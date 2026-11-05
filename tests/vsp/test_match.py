"""Сопоставление отделений между источниками: адресный ключ и правила склейки."""
from __future__ import annotations

import pytest

from bank_audit.vsp.registry.match import Item, address_key, haversine_m, match_sources


@pytest.mark.parametrize("addr,city,key", [
    ("Россия, Москва, улица Ленина, 15", "Москва", "москва|ленина|15"),     # Яндекс
    ("Ленина, 15", "Москва", "москва|ленина|15"),                           # 2ГИС
    ("г. Москва, ул. Ленина, д. 15", "г. Москва", "москва|ленина|15"),
    ("Тверская ул., 7, Москва, 125009", "Москва", "москва|тверская|7"),
    ("проспект Мира, 101в стр. 1", "Москва", "москва|мира|101вс1"),
    ("ул. 8 Марта, 12/1", "Екатеринбург", "екатеринбург|8 марта|12к1"),
    ("Свердловская область, Екатеринбург, улица 8 Марта, 12 к1", "Екатеринбург", "екатеринбург|8 марта|12к1"),
    ("Московская обл., г. Химки, Юбилейный пр-т, 1", "Химки", "химки|юбилейный|1"),
    ("Москва, пос. Внуково, ул. Центральная, 3", "Москва", "москва|центральная|3"),
])
def test_address_key_formats(addr, city, key):
    assert address_key(addr, city) == key


def test_address_key_distinguishes_houses_and_needs_house():
    assert address_key("Ленина, 15", "Москва") != address_key("Ленина, 17", "Москва")
    assert address_key("Ленина, 15к1", "Москва") != address_key("Ленина, 15", "Москва")
    assert address_key("Москва, Ленинский район", "Москва") is None


def test_haversine():
    assert abs(haversine_m(55.7558, 37.6173, 55.7558, 37.6183) - 62.8) < 1.0


def _i(src, sid, lat, lon, addr, city="Москва", name="Сбербанк"):
    return Item(src, sid, lat, lon, addr, city, name)


def test_same_building_pair_is_auto():
    a = [_i("2gis", "g1", 55.75000, 37.60000, "Ленина, 15")]
    b = [_i("yandex", "y1", 55.75020, 37.60010, "Россия, Москва, улица Ленина, 15")]
    (m,) = match_sources(a, b)
    assert m.status == "auto" and m.same_address and m.distance_m < 30


def test_two_offices_in_one_building_are_conflict_not_merged():
    # у Яндекса в одном торговом центре две карточки, у 2ГИС — одна: склеивать нельзя
    a = [_i("2gis", "g1", 55.75000, 37.60000, "Ленина, 15")]
    b = [_i("yandex", "y1", 55.75005, 37.60005, "Москва, улица Ленина, 15"),
         _i("yandex", "y2", 55.75008, 37.60002, "Москва, улица Ленина, 15")]
    (m,) = match_sources(a, b)
    assert m.status == "conflict"


def test_similar_name_far_away_never_matches():
    a = [_i("2gis", "g1", 55.75000, 37.60000, "Ленина, 15", name="Сбербанк, доп. офис № 9038/01")]
    b = [_i("yandex", "y1", 55.80000, 37.70000, "Москва, улица Мира, 3", name="Сбербанк, доп. офис № 9038/01")]
    assert match_sources(a, b) == []


def test_neighbour_building_with_other_address_is_not_matched():
    a = [_i("2gis", "g1", 55.75000, 37.60000, "Ленина, 15")]
    b = [_i("yandex", "y1", 55.75100, 37.60000, "Москва, улица Ленина, 21")]   # ~110 м, другой дом
    assert match_sources(a, b) == []


def test_same_address_no_coords_goes_to_review():
    a = [_i("2gis", "g1", None, None, "Ленина, 15")]
    b = [_i("domclick", "d1", None, None, "г. Москва, ул. Ленина, д. 15")]
    (m,) = match_sources(a, b)
    assert m.status == "conflict" and "координат нет" in m.reason


def test_weak_distance_needs_same_address():
    a = [_i("2gis", "g1", 55.75000, 37.60000, "Ленина, 15")]
    b = [_i("yandex", "y1", 55.75150, 37.60000, "Москва, улица Ленина, 15")]   # ~170 м, тот же дом
    (m,) = match_sources(a, b)
    assert m.status == "auto" and m.same_address
