"""Паспорт ВСП с сайта банка и форматы офисов (без сети, синтетические данные)."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from bank_audit.vsp import mass
from bank_audit.vsp.collectors.twogis import card_format, is_sber_office
from bank_audit.vsp.config import Settings
from bank_audit.vsp.models import SourceBlocked, SourceBranch
from bank_audit.vsp.registry import banksite as bs
from bank_audit.vsp.registry.apply import apply_formats, import_registry, link_profiles, match_all
from bank_audit.vsp.registry.cbr import parse_row
from bank_audit.vsp.storage import Store, branch_profiles, branches, profile_changes, source_branches

H = 3_600_000
WEEK = [[10 * H, 20 * H], None] * 5 + [[10 * H, 15 * H], None] + [None, None]   # пн–пт, сб, вс выходной


def feat(i, name, lat=55.75, lon=37.62, **flags):
    props = {"name": name, "code": f"0{i}", "services": ["NPS3", "LPS4"], "workingHoursNp": WEEK,
             "naturalPersonMs": True, **flags}
    return {"id": i, "geometry": {"coordinates": [lat, lon], "type": "Point"}, "properties": props, "type": "Feature"}


def test_parse_office_formats_hours_and_code():
    r = bs.parse_office(feat(1, "Доп.офис №9038/01008", naturalPersonVip=True, zone24=True))
    assert r["vsp_key"] == "9038/1008" and r["formats"] == ["mass", "sber_first", "zone24"]
    assert r["hours"]["text"] == "пн–пт 10:00–20:00 · сб 10:00–15:00 · вс выходной"
    assert bs.works_on(WEEK, 6) is False and bs.works_on(WEEK, 0) is True
    assert bs.vsp_key("№ 9038/1008") == bs.vsp_key("№9038/01008") == "9038/1008"
    assert bs.vsp_key("Доп.офис №17/0291") == bs.vsp_key("№17/291") == "17/291"   # двузначный ГОСБ
    assert bs.vsp_key("Московский банк") is None and bs.vsp_key("№123456/1") is None
    assert bs.tile_of(55.75, 37.62) == (154, 80)                     # Москва на масштабе 8


class FakeBrowser:
    def __init__(self, tiles, block_on=None):
        self.tiles, self.block_on, self.requests = tiles, block_on, 0

    async def tile(self, x, y, z):
        self.requests += 1
        if (x, y, z) == self.block_on:
            raise SourceBlocked("banksite", "captcha")
        return self.tiles.get((x, y, z), [])


def test_fetch_offices_splits_dense_tiles_and_stops_on_block(monkeypatch):
    monkeypatch.setattr(bs, "SPLIT_AT", 3)
    dense = [feat(i, f"Доп.офис №9038/0{i}") for i in range(3)]
    child = {(308, 160, 9): dense[:2], (309, 161, 9): dense[2:]}
    br = FakeBrowser({(154, 80, 8): dense, **child})
    got = asyncio.run(bs.fetch_offices(br, [(154, 80)]))
    assert sorted(got) == ["0", "1", "2"] and br.requests == 5          # плитка + 4 дочерних
    with pytest.raises(SourceBlocked):
        asyncio.run(bs.fetch_offices(FakeBrowser({}, block_on=(1, 1, 8)), [(1, 1)]))


def test_save_profiles_history_and_gone_only_in_crawled_tiles(db_url):
    st = Store(db_url)
    a = bs.parse_office(feat(1, "Доп.офис №9038/01008", naturalPersonVip=True))
    b = bs.parse_office(feat(2, "Доп.офис №9038/01009"))
    far = bs.parse_office(feat(3, "Доп.офис №8602/0100", lat=53.7, lon=91.4))
    assert st.save_profiles({"1": a, "2": b, "3": far}) == {"new": 3, "changed": 0, "same": 0, "gone": 0}
    a2 = {**a, "formats": ["mass"]}                                        # убрали СберПервый
    res = st.save_profiles({"1": a2}, crawled_tiles={bs.tile_of(55.75, 37.62)})
    assert res == {"new": 0, "changed": 1, "same": 0, "gone": 1}          # «2» пропал, «3» — плитка не пройдена
    with st.engine.connect() as c:
        ch = {(r.site_id, r.field) for r in c.execute(select(profile_changes))}
        status = dict(c.execute(select(branch_profiles.c.site_id, branch_profiles.c.status)).all())
    assert ("1", "formats") in ch and ("2", "status") in ch
    assert status == {"1": "active", "2": "gone", "3": "active"}


def _cbr(ref, name, addr, kind="BNK_DOPO"):
    return parse_row({"FILIAL_SBJ_ID": ref, "FILIAL_SBJ_TYPE_CODE": kind, "FILIAL_NAME": name,
                      "FILIAL_NUM": f"1481/1/{ref}", "FILIAL_ADDRESS": addr})


def test_link_profiles_and_formats_from_all_sources(db_url):
    st = Store(db_url)
    import_registry(st, [_cbr("1", "№ 9038/01008", "119048, Г. МОСКВА, УЛ. УСАЧЕВА, Д. 35"),
                         _cbr("2", "С000ТС00 Автомобиль", "655017, Г. АБАКАН, УЛ. ЛЕНИНА, Д. 1", kind="BNK_PPKO")])
    st.save_profiles({"1": bs.parse_office(feat(1, "Доп.офис №9038/1008", naturalPersonVip=True, legalPerson=True))})
    assert link_profiles(st)["linked"] == 1
    st.upsert_source_branch(SourceBranch("yandex", "y1", address="Москва, улица Усачёва, 35", city="Москва",
                                         lat=55.73, lon=37.57, raw={"cbr_num": "1481/1/1", "office_type": ["office_sme"]}))
    st.upsert_source_branch(SourceBranch("2gis", "g1", address="Усачёва, 35", city="Москва", lat=55.7301,
                                         lon=37.5701, raw={"format": "sber_first"}))
    match_all(st)
    res = apply_formats(st)
    with st.engine.connect() as c:
        f = {r.registry_ref: json.loads(r.formats or "{}") for r in c.execute(select(branches))}
    assert f["1"]["sber_first"] == ["2ГИС", "сайт банка"] and f["1"]["business"] == ["Яндекс"]
    assert f["1"]["legal"] == ["сайт банка"] and f["2"] == {"mobile": ["реестр ЦБ"]}
    assert res["sber_first"] == 1


def test_link_profiles_two_digit_gosb_and_mobile_stops(db_url):
    st = Store(db_url)
    import_registry(st, [_cbr("1", "№17/0291", "150000, Г. ЯРОСЛАВЛЬ, УЛ. СВОБОДЫ, Д. 1"),
                         _cbr("2", "В066ОС193 Автомобиль", "350000, Г. КРАСНОДАР, УЛ. КРАСНАЯ, Д. 1", kind="BNK_PPKO")])
    stops = {str(i): bs.parse_office(feat(i, f"МО №8626/06008 (п. Точка {i})", mobile=True)) for i in (2, 3)}
    st.save_profiles({"1": bs.parse_office(feat(1, "Доп.офис №17/0291")), **stops,
                      "4": bs.parse_office(feat(4, "Московский банк"))})
    with st.engine.begin() as c:                           # ключ, сохранённый старым разбором
        c.execute(branch_profiles.update().where(branch_profiles.c.site_id == "1").values(vsp_key=None))
    res = link_profiles(st)
    assert res == {"linked": 1, "unlinked": 1, "ambiguous": 0, "mobile_stops": 2, "mobile_routes": 1}
    with st.engine.connect() as c:
        keys = dict(c.execute(select(branch_profiles.c.site_id, branch_profiles.c.vsp_key)).all())
    assert keys["1"] == "17/291" and keys["2"] == "8626/6008"


def test_child_card_does_not_steal_the_yandex_pair(db_url):
    """В одном здании основная карточка ВСП и карточка СберПервого (2ГИС). Карточка Яндекса
    сходится с основной; карточка СберПервого привязывается к тому же ВСП как дочерняя."""
    st = Store(db_url)
    import_registry(st, [_cbr("1", "№ 9038/01008", "119048, Г. МОСКВА, УЛ. НЕРАЗБИРАЕМАЯ")])
    st.upsert_source_branch(SourceBranch("yandex", "y1", address="Москва, ТЦ «Центр»", city="Москва",
                                         lat=55.73, lon=37.57, raw={"cbr_num": "1481/1/1"}))
    st.upsert_source_branch(SourceBranch("2gis", "main", address="ТЦ Центр", city="Москва", lat=55.73001, lon=37.57001))
    st.upsert_source_branch(SourceBranch("2gis", "vip", address="ТЦ Центр", city="Москва", lat=55.73002,
                                         lon=37.57002, raw={"format": "sber_first"}))
    res = match_all(st)
    with st.engine.connect() as c:
        got = {r.source_branch_id: (r.branch_id, r.match_method) for r in c.execute(select(source_branches))}
    bid = got["y1"][0]
    assert got["main"] == (bid, "cross_source") and got["vip"] == (bid, "parent_card")
    assert res.get("cross_conflict", 0) == 0 and res["child_linked"] == 1


def test_2gis_card_formats():
    vip = {"name": "СберПервый, отдел по работе с ВИП-клиентами", "org": {"name": "СберБанк, ПАО СберБанк"},
           "rubrics": [{"name": "Банки"}]}
    pb = {"name": "Sberbank Private Banking", "org": {"name": "Sberbank Private Banking, ПАО СберБанк"},
          "rubrics": [{"name": "Инвестиционные компании"}]}
    other = {"name": "Private Banking, банк", "org": {"name": "Другой банк, банк"}, "rubrics": [{"name": "Банки"}]}
    assert (card_format(vip), is_sber_office(vip)) == ("sber_first", True)
    assert (card_format(pb), is_sber_office(pb)) == ("private", True)
    assert (card_format(other), is_sber_office(other)) == (None, False)


def test_refresh_banksite_end_to_end(db_url, monkeypatch):
    st = Store(db_url)
    import_registry(st, [_cbr("1", "№ 9038/01008", "119048, Г. МОСКВА, УЛ. УСАЧЕВА, Д. 35")])
    st.upsert_source_branch(SourceBranch("2gis", "g1", address="Усачёва, 35", city="Москва", lat=55.75, lon=37.62))

    class Br(FakeBrowser):
        def __init__(self, *a, **k):
            super().__init__({(154, 80, 8): [feat(1, "Доп.офис №9038/01008", naturalPersonPb=True)]})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return None
    monkeypatch.setattr(bs, "BankSiteBrowser", Br)
    s = Settings()
    s.bank_site = "https://bank.example"
    res = asyncio.run(mass.refresh_banksite(st, s))
    assert res["offices"] == 1 and res["linked"]["linked"] == 1 and res["formats"]["private"] == 1
    assert asyncio.run(mass.refresh_banksite(st, s)) == {"skipped": "свежий обход"}
    s.bank_site = ""
    assert asyncio.run(mass.refresh_banksite(st, s, force=True)) == {"skipped": "VSP_BANK_SITE не задан"}


def test_verify_tls_without_bundle_is_reported_not_silent():
    assert bs.verify_tls("https://bank.example", None) is False
