"""«Пульс» → «Сеть ВСП»: кто пользуется разделом «Аудит сети ВСП» и кому он открыт (только владелец)."""
from __future__ import annotations

import contextlib
import os
from datetime import date, datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit import db  # noqa: E402
from bank_audit.web import org_reach, telemetry, vsp_api, vsp_pulse  # noqa: E402


def test_what_a_request_means():
    w = vsp_pulse.what_of
    assert w("/api/vsp/brief") == "main" and w("/api/vsp/waves") == "schemes" and w("/api/vsp/hotspots") == "schemes"
    assert w("/api/vsp/waves/k8") == "wave" and w("/api/vsp/hotspots/eb8e") == "hot"
    assert w("/api/vsp/dossier/b3203") == "dossier" and w("/api/vsp/evidence/yandex:abc") == "evidence"
    assert w("/api/vsp/export/stories.xlsx") == "export" and w("/api/vsp/stories") == "stories"
    assert w("/api/vsp/meta") is None and w("/api/vsp/dossier/") is None      # служебное и пустой адрес — не действие


def _setup(monkeypatch, with_owner=False):
    key = org_reach.name_key
    roster = [{"tab_no": "100", "status": "W", "unit_code": "55", "unit_hint": "Управление по СЗБ", "name_key": key("иван", "иванов")},
              {"tab_no": "101", "status": "W", "unit_code": "55", "unit_hint": "Управление по СЗБ", "name_key": key("пётр", "петров")},
              {"tab_no": "102", "status": "W", "unit_code": "55", "unit_hint": "Управление по СЗБ", "name_key": key("ольга", "новая")},
              {"tab_no": "300", "status": "F", "unit_code": "55", "unit_hint": "Управление по СЗБ", "name_key": key("олег", "бывший")},
              {"tab_no": "200", "status": "W", "unit_code": "42", "unit_hint": "Управление по ВВБ", "name_key": key("сидор", "сидоров")}]
    t = lambda d, h=10: datetime(2026, 11, d, h, tzinfo=timezone.utc)  # noqa: E731
    users = [("owner", "Владелец Инструмента", t(11)), ("ivanov-100", "Иван Иванов", t(11)),
             ("petrov-101", "Пётр Петров", t(9)), ("sid-200", "Сидор Сидоров", t(8)),
             ("ext-999", "Анна Внешняя", t(10)), ("old-300", "Олег Бывший", t(1))]
    opens = [("ivanov-100", 3, 2, t(9), t(11, 9), 12.5, 2), ("ext-999", 1, 1, t(10), t(10), 0.4, 1),
             ("owner", 40, 5, t(1), t(11), 300.0, 5)]
    api = [("ivanov-100", "/api/vsp/dossier/b1", 4), ("ivanov-100", "/api/vsp/brief", 3), ("ivanov-100", "/api/vsp/meta", 9),
           ("ext-999", "/api/vsp/export/stories.xlsx", 1), ("owner", "/api/vsp/dossier/b2", 50)]
    cases = [("ivanov-100", 2)]
    daily = [(date(2026, 11, 9), "ivanov-100", 2), (date(2026, 11, 11), "ivanov-100", 1),
             (date(2026, 11, 10), "ext-999", 1), (date(2026, 11, 11), "owner", 5)]
    axis = [(date(2026, 11, d),) for d in (9, 10, 11)]

    class R:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

        def mappings(self):
            return self.rows

    class Sess:
        def execute(self, sql, params=None):
            q = " ".join(str(sql).split())
            for mark, rows in (("FROM org_member", roster), ("FROM app_user", users),
                               ("page = 'vsp' AND kind IN", opens), ("page LIKE '/api/vsp/%'", api),
                               ("audit_case_item", cases), ("generate_series", axis),
                               ("kind = 'page_view' AND page = 'vsp'", daily)):
                if mark in q:
                    return R(rows)
            raise AssertionError("неожиданный запрос: " + q[:80])

    @contextlib.contextmanager
    def session():
        yield Sess()
    monkeypatch.setattr(db, "session", session)
    monkeypatch.setenv("ADMIN_USERS", "owner")
    monkeypatch.delenv("VSP_USERS", raising=False)
    monkeypatch.delenv("ORG_UNIT_NAMES", raising=False)
    monkeypatch.setattr(telemetry, "excluded", lambda viewer, with_me=None: [] if with_me else ["owner"])
    monkeypatch.setattr(vsp_api, "_beta", lambda: {"users": ["ext-999"], "units": ["55"], "tabs": {"100", "101", "102"},
                                                   "all_tabs": {"100", "101", "102", "200", "300"}, "names": set()})
    return vsp_pulse.report("owner", 30, with_owner)


def test_who_has_the_section_and_who_opened_it(monkeypatch):
    r = _setup(monkeypatch)
    s = r["summary"]
    # в открытом подразделении работают трое, один из них в AuditLens ещё не входил; уволенный не в счёт
    assert s["staff"] == 3 and s["accounts"] == 2 and s["no_account"] == 1
    assert s["extra"] == 1          # по логину сверх открытого подразделения — один человек
    assert [p["login"] for p in r["people"]] == ["ivanov-100", "ext-999", "petrov-101"]   # кто заходил позже — выше; не открывавшие — в конце
    assert s["with_access"] == 3 and s["opened"] == 2 and s["opened_period"] == 2 and s["returned"] == 1 and s["deep"] == 2
    iv, ext, pet = r["people"]
    assert iv["how"] == "unit" and iv["unit_name"] == "СЗБ" and iv["views"] == 3 and iv["days"] == 2 and iv["minutes"] == 12.5
    assert iv["did"] == {"dossier": 4, "main": 3, "case": 2} and iv["deep"] == 6
    assert ext["how"] == "login" and ext["unit_name"] is None and ext["did"] == {"export": 1}
    assert pet["last"] is None and pet["views"] == 0 and pet["did"] == {}
    # владелец себя не считает; с переключателем «со мной» — считает
    assert all(p["login"] != "owner" for p in r["people"])
    assert [(x["d"], x["users"], x["views"]) for x in r["by_day"]] == [("2026-11-09", 1, 2), ("2026-11-10", 1, 1), ("2026-11-11", 1, 1)]
    with_me = _setup(monkeypatch, with_owner=True)
    assert with_me["people"][0]["login"] == "owner" and with_me["people"][0]["how"] == "owner"
    assert with_me["by_day"][-1]["users"] == 2


def test_sections_units_and_people_to_open_for(monkeypatch):
    r = _setup(monkeypatch)
    sec = {x["key"]: (x["users"], x["n"]) for x in r["sections"]}
    assert sec["main"] == (1, 3) and sec["dossier"] == (1, 4) and sec["export"] == (1, 1) and sec["case"] == (1, 2)
    assert sec["wave"] == (0, 0)
    units = {u["code"]: u for u in r["units"]}
    assert units["55"] == {"code": "55", "name": "СЗБ", "head": 3, "accounts": 2, "open": True}
    assert units["42"] == {"code": "42", "name": "ВВБ", "head": 1, "accounts": 1, "open": False}
    assert r["units"][0]["code"] == "55"                                   # открытые — первыми
    assert r["access"] == {"users": ["ext-999"], "units": ["55"]}
    assert r["logins"] == [{"login": "ext-999", "name": "Анна Внешняя", "unit_name": None}]
    # кому раздел можно открыть: уже заходили в AuditLens, раздела нет (уволенный — по логину тоже можно)
    assert [x["login"] for x in r["rest"]] == ["old-300", "sid-200"]
    assert r["rest"][1]["unit_name"] == "ВВБ" and r["rest"][0]["unit_name"] is None


def test_endpoint_is_for_the_owner_only(monkeypatch):
    from fastapi import HTTPException
    from bank_audit.web import app as webapp
    from bank_audit.web.auth import CurrentUser
    monkeypatch.setenv("ADMIN_USERS", "owner")
    monkeypatch.setenv("PULSE_USERS", "boss")
    monkeypatch.setattr(vsp_pulse, "report", lambda viewer, days, me: {"viewer": viewer, "days": days})
    boss = CurrentUser(username="boss", name="Руководитель", authenticated=True)
    try:
        webapp.admin_pulse_vsp(30, None, boss)       # «Пульс» видит, а этот раздел — нет
        raise AssertionError("ожидался отказ")
    except HTTPException as e:
        assert e.status_code == 403
    own = CurrentUser(username="owner", name="Владелец", authenticated=True)
    assert webapp.admin_pulse_vsp(7, None, own) == {"viewer": "owner", "days": 7}
