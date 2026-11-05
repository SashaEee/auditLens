"""Реестр ЦБ и привязка карточек источников к справочнику отделений."""
from __future__ import annotations

from sqlalchemy import select

from bank_audit.vsp.models import SourceBranch
from bank_audit.vsp.registry.apply import import_registry, match_all
from bank_audit.vsp.registry.cbr import parse_row, split_address
from bank_audit.vsp.storage import Store, branches, source_branches


def _cbr(ref, addr, kind="BNK_DOPO", name="№ 8602/0100"):
    return parse_row({"FILIAL_SBJ_ID": ref, "FILIAL_SBJ_TYPE_CODE": kind, "FILIAL_NAME": name,
                      "FILIAL_NUM": "1481/1", "FILIAL_ADDRESS": addr, "FILIAL_OPEN_DATE": "2001-07-13T00:00:00"})


def test_split_cbr_address():
    assert split_address("655017, РЕСПУБЛИКА ХАКАСИЯ, Г. АБАКАН, УЛ. ПУШКИНА, Д. 165") == ("Республика Хакасия", "Абакан")
    assert split_address("119048, Г. МОСКВА, УЛ. УСАЧЕВА, Д. 35") == ("Москва", "Москва")
    assert split_address("164224, АРХАНГЕЛЬСКАЯ ОБЛ., НЯНДОМСКИЙ Р-Н, ДЕР. МАКАРОВСКАЯ, УЛ. НАБЕРЕЖНАЯ, Д.6")[1] \
        == "Макаровская"


def test_parse_row_kinds_and_key():
    r = _cbr("1", "655017, РЕСПУБЛИКА ХАКАСИЯ, Г. АБАКАН, УЛ. ПУШКИНА, Д. 165, СТРОЕНИЕ 1, ПОМ. 2Н")
    assert (r.kind, r.city, r.akey) == ("office", "Абакан", "абакан|пушкина|165с1")
    assert _cbr("2", "655017, Г. АБАКАН, УЛ. ЛЕНИНА, Д. 1#655018, Г. АБАКАН, УЛ. МИРА, Д. 2",
                kind="BNK_PPKO").kind == "mobile"


def test_import_and_match(tmp_path):
    st = Store(f"sqlite:///{tmp_path / 'r.sqlite'}")
    rows = [_cbr("1", "655017, РЕСПУБЛИКА ХАКАСИЯ, Г. АБАКАН, УЛ. ПУШКИНА, Д. 165, СТРОЕНИЕ 1"),
            _cbr("2", "655017, РЕСПУБЛИКА ХАКАСИЯ, Г. АБАКАН, УЛ. ЛЕНИНА, Д. 10"),
            _cbr("3", "655017, РЕСПУБЛИКА ХАКАСИЯ, Г. АБАКАН, УЛ. ЛЕНИНА, Д. 10")]   # два ВСП по одному адресу
    assert import_registry(st, rows) == {"new": 3, "updated": 0}
    assert import_registry(st, rows) == {"new": 0, "updated": 3}                     # повтор — без дублей
    # 2ГИС: карточка по адресу «Пушкина, 165 ст1»; Яндекс — та же точка без разборчивого адреса
    st.upsert_source_branch(SourceBranch("2gis", "g1", address="Пушкина, 165 ст1", city="Абакан",
                                         lat=53.72, lon=91.44))
    st.upsert_source_branch(SourceBranch("yandex", "y1", address="Абакан, Торговый центр", city="Абакан",
                                         lat=53.72005, lon=91.44003))
    st.upsert_source_branch(SourceBranch("2gis", "g2", address="Ленина, 10", city="Абакан",
                                         lat=53.70, lon=91.40))
    st.upsert_source_branch(SourceBranch("2gis", "g3", address="Мира, 99", city="Абакан",
                                         lat=53.60, lon=91.30))
    res = match_all(st)
    with st.engine.connect() as c:
        got = {(r.source, r.source_branch_id): (r.branch_id, r.match_method, r.match_status)
               for r in c.execute(select(source_branches))}
        b1 = c.execute(select(branches.c.branch_id, branches.c.lat).where(branches.c.registry_ref == "1")).first()
    assert got[("2gis", "g1")] == (b1.branch_id, "registry_address", "auto")
    assert got[("yandex", "y1")] == (b1.branch_id, "cross_source", "auto")          # по зданию через 2ГИС
    assert got[("2gis", "g2")][2] == "conflict"                                      # два ВСП по адресу
    assert got[("2gis", "g3")] == (None, None, "none")
    assert b1.lat == 53.72                                                           # координаты от карточки
    assert res["2gis:auto"] == 1 and res["yandex:auto"] == 1
