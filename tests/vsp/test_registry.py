"""Реестр ЦБ и привязка карточек источников к справочнику отделений."""
from __future__ import annotations

from sqlalchemy import select

from bank_audit.vsp.models import SourceBranch
from bank_audit.vsp.registry.apply import import_registry, match_all
from bank_audit.vsp.registry.cbr import parse_row, split_address
from bank_audit.vsp.storage import Store, branches, source_branches


def _cbr(ref, addr, kind="BNK_DOPO", name="№ 8602/0100", num=None):
    return parse_row({"FILIAL_SBJ_ID": ref, "FILIAL_SBJ_TYPE_CODE": kind, "FILIAL_NAME": name,
                      "FILIAL_NUM": num or f"1481/1/{ref}", "FILIAL_ADDRESS": addr,
                      "FILIAL_OPEN_DATE": "2001-07-13T00:00:00"})


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


def test_pilot_pairs_2gis_with_yandex_without_registry(tmp_path):
    """Пилот берёт карточку Яндекса в том же здании, даже если реестр ЦБ адрес не разобрал;
    соседнее отделение через дорогу не склеивается."""
    from bank_audit.vsp.models import SourceBranch
    from bank_audit.vsp.pilot import _yandex_for_pilot
    from bank_audit.vsp.storage import Store
    st = Store(f"sqlite:///{tmp_path}/p.sqlite")
    st.upsert_source_branch(SourceBranch("2gis", "g1", "СберБанк", "улица Мира, 5", "Кимры", None, 56.87, 37.35))
    st.upsert_source_branch(SourceBranch("yandex", "y1", "СберБанк", "Тверская область, Кимры, улица Мира, 5",
                                         "Кимры", None, 56.8701, 37.3501))
    st.upsert_source_branch(SourceBranch("yandex", "y2", "СберБанк", "Тверская область, Кимры, улица Мира, 12",
                                         "Кимры", None, 56.8712, 37.3530))
    ids, info = _yandex_for_pilot(st, ["g1"])
    assert ids == ["y1"] and info["missing"] == [] and info["pairs"] == {"g1": "y1"}


def test_cash_center_and_match_by_cbr_number(tmp_path):
    """Кассово-инкассаторский центр не обслуживает клиентов — не кандидат для привязки по адресу.
    Карточка Яндекса с номером подразделения ЦБ привязывается по номеру, даже если адрес не разобран."""
    kic = _cbr("9", "655017, Г. АБАКАН, УЛ. ПУШКИНА, Д. 165", name='№8602/0112 кассово-инкассаторский центр "Абаканский"')
    assert kic.kind == "cash_center"
    st = Store(f"sqlite:///{tmp_path / 'c.sqlite'}")
    import_registry(st, [kic, _cbr("1", "655017, Г. АБАКАН, УЛ. ЛЕНИНА, Д. 10", num="1481/1662/34")])
    st.upsert_source_branch(SourceBranch("2gis", "g1", address="Пушкина, 165", city="Абакан", lat=53.7, lon=91.4))
    st.upsert_source_branch(SourceBranch("yandex", "y1", address="Абакан, ТЦ «Центр»", city="Абакан",
                                         lat=53.71, lon=91.41, raw={"cbr_num": "1481/1662/34"}))
    res = match_all(st)
    with st.engine.connect() as c:
        got = {r.source_branch_id: (r.branch_id, r.match_method, r.match_status)
               for r in c.execute(select(source_branches))}
        b1 = c.execute(select(branches.c.branch_id, branches.c.registry_num, branches.c.status)
                       .where(branches.c.registry_ref == "1")).first()
        kst = c.execute(select(branches.c.status).where(branches.c.registry_ref == "9")).scalar()
    assert kst == "cash_center" and got["g1"] == (None, None, "none")
    assert b1.registry_num == "1481/1662/34" and got["y1"] == (b1.branch_id, "registry_code", "auto")
    assert res["by_code"] == 1


def test_store_adds_missing_columns(tmp_path):
    """База прошлой версии без нового столбца: при запуске столбец добавляется, данные целы."""
    import sqlite3
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE vsp_branch (branch_id INTEGER PRIMARY KEY, region VARCHAR(200), city VARCHAR(200), "
                "name VARCHAR(400), address VARCHAR(600), address_key VARCHAR(300), lat FLOAT, lon FLOAT, "
                "registry_source VARCHAR(40), registry_ref VARCHAR(200), status VARCHAR(20), "
                "created_at DATETIME, updated_at DATETIME)")
    con.execute("INSERT INTO vsp_branch (branch_id, name) VALUES (1, 'старое')")
    con.commit(); con.close()
    st = Store(f"sqlite:///{db}")
    with st.engine.connect() as c:
        row = c.execute(select(branches.c.name, branches.c.registry_num)).first()
    assert row.name == "старое" and row.registry_num is None
