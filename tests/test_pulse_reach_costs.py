"""«Пульс» → «Охват и траты»: разбор кадровой выгрузки, сопоставление, подписи
подразделений и права на эндпоинты. Сеть и Postgres не нужны: выгрузка
собирается в тесте, имена — выдуманные."""
from __future__ import annotations

import base64
import io
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import openpyxl  # noqa: E402

from bank_audit.web import cloud_costs as CC  # noqa: E402
from bank_audit.web import org_reach as OR  # noqa: E402


def _xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Табельный_номер", "Статус*", "ФИО", "Грейд", "Должность", "Подразделение",
               "Вышест_е подразд_е", "Блок", "Номер_ПП"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ROWS = [
    (1000001, "W", "ПЕТРОВА АННА СЕРГЕЕВНА", 9, "Эксперт", "Отдел", "Управление по ЮЗК", "Блок", 52),
    (1000002, "F", "СИДОРОВ ОЛЕГ ИВАНОВИЧ", 7, "Старший аудитор", "Отдел", "Управление", "Блок", 52),
    (4321, "W", "ЁЖИКОВА ЕЛЕНА", 11, "Менеджер направления", "Отдел", "Управление", "Блок", "-"),
]


def test_roster_parsed_without_names():
    rows = OR.parse_roster(_xlsx(ROWS))
    assert [r["tab_no"] for r in rows] == ["1000001", "1000002", "4321"]
    assert all("fio" not in r for r in rows)                       # ФИО не храним
    assert rows[0]["unit_code"] == "52" and rows[2]["unit_code"] == "-"
    assert rows[0]["grade"] == 9 and rows[1]["status"] == "F"
    # «Имя Фамилия» из системы входа совпадает с «ФАМИЛИЯ ИМЯ ОТЧЕСТВО» из выгрузки
    assert rows[0]["name_key"] == OR.name_key(*OR._words("Анна Петрова"))
    assert rows[2]["name_key"] == OR.name_key(*OR._words("Елена Ежикова"))   # ё → е


def test_roster_requires_key_columns():
    wb = openpyxl.Workbook()
    wb.active.append(["ФИО", "Должность"])
    wb.active.append(["ИВАНОВ ИВАН", "Аудитор"])
    buf = io.BytesIO()
    wb.save(buf)
    try:
        OR.parse_roster(buf.getvalue())
    except ValueError as e:
        assert "табельный номер" in str(e)
    else:
        raise AssertionError("выгрузка без табельного номера принята")


def test_unit_labels(monkeypatch):
    monkeypatch.delenv("ORG_UNIT_NAMES", raising=False)
    assert OR.unit_label("52", "Управление по ЮЗК") == "№ 52"     # не «…Б» — номер
    assert OR.unit_label("55", "Управление по НКБ") == "НКБ"
    assert OR.unit_label("-", "Управление") == "Центр"
    monkeypatch.setenv("ORG_UNIT_NAMES", '{"52": "Юг"}')
    assert OR.unit_label("52", "") == "Юг"
    monkeypatch.setenv("ORG_UNIT_NAMES", "52=Юг,-=Центр управления")
    assert OR.unit_label("52", "") == "Юг" and OR.unit_label("-", "") == "Центр управления"


def test_roles():
    assert OR._role("Менеджер направления") == "Руководители"
    assert OR._role("Главный эксперт по технологиям") == "Эксперты и специалисты"
    assert OR._role("Старший аудитор") == "Аудиторы"


def test_costs_not_configured(monkeypatch):
    for k in ("CLOUD_COSTS_URL", "CLOUD_COSTS_TOKEN", "CLOUD_COSTS_DEPARTMENT"):
        monkeypatch.delenv(k, raising=False)
    assert CC.summary() == {"configured": False}
    assert CC.sync() == 0


def test_endpoints_rights(monkeypatch):
    from fastapi.testclient import TestClient

    from bank_audit.web import app as A
    monkeypatch.setenv("ADMIN_USERS", "owner-1")
    monkeypatch.setenv("PULSE_USERS", "viewer-1")
    seen = {}
    monkeypatch.setattr(OR, "load_roster", lambda raw: seen.update(n=len(raw)) or {"rows": 3, "current": 2})
    monkeypatch.setattr(OR, "report", lambda *a, **k: {"loaded": False})
    for k in ("CLOUD_COSTS_URL", "CLOUD_COSTS_TOKEN", "CLOUD_COSTS_DEPARTMENT"):
        monkeypatch.delenv(k, raising=False)
    c = TestClient(A.app)
    h = lambda u: {"X-Authentik-Username": u, "X-Authentik-Name": u}  # noqa: E731
    body = {"name": "штат.xlsx", "data": base64.b64encode(_xlsx(ROWS)).decode()}
    assert c.get("/api/admin/pulse/reach", headers=h("user-1")).status_code == 403
    assert c.get("/api/admin/pulse/costs", headers=h("user-1")).status_code == 403
    assert c.get("/api/admin/pulse/reach", headers=h("viewer-1")).json() == {"loaded": False}
    assert c.get("/api/admin/pulse/costs", headers=h("viewer-1")).json() == {"configured": False}
    # загружает штат только владелец
    assert c.post("/api/admin/pulse/reach", json=body, headers=h("viewer-1")).status_code == 403
    r = c.post("/api/admin/pulse/reach", json=body, headers=h("owner-1"))
    assert r.status_code == 200 and r.json()["rows"] == 3 and seen["n"] > 0
    assert c.post("/api/admin/pulse/reach", json={"data": "не base64"}, headers=h("owner-1")).status_code == 400
