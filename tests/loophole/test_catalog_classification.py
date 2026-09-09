"""Тип записи: сохранение, отбор каталога, права и совместимость старых данных."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from tests.loophole import test_record_verdict_authorization as access
from tests.loophole.test_preliminary_research_source_import import _create_import_schema

session = access.session
client = access.client
TYPES = ["vulnerability", "fraud_scheme", "not_confirmed"]


@pytest.mark.parametrize("role", ["ccks_expert", "module_admin"])
@pytest.mark.parametrize("before_type", TYPES)
@pytest.mark.parametrize("after_type", TYPES)
def test_classification_transition_round_trips_and_moves_between_filters(
    client, session, role, before_type, after_type,
):
    """Все переходы сохраняют публикацию и синхронизируют KB и выборку каталога."""
    access._access(session, role=role)
    _create_import_schema(session)
    record_id = repo.insert_record(LoopholeRecord(
        sha256="transition", title="Запись", snippet="Прочитанные условия",
        is_loophole=before_type != "not_confirmed", classification=before_type,
        status="published",
    ), session=session)
    response = client.post(access._ENDPOINT, headers=access._HEADERS, json={
        "record_ids": [record_id], "classification": after_type, "comment": "Решение эксперта",
    })
    assert response.status_code == 200
    record = repo.get_record(record_id, session=session)
    assert record["classification"] == after_type
    assert record["is_loophole"] is (after_type != "not_confirmed")
    assert record["status"] == "published"
    assert record["verdict_reason"] == "Решение эксперта"
    assert repo.list_records(session=session)[0]["classification"] == after_type
    if after_type != "not_confirmed":
        assert repo.list_published_cases(session=session)[0]["classification"] == after_type
    assert (repo.get_kb_example_by_record(record_id, session=session) is not None) is (
        after_type != "not_confirmed"
    )
    for selected in ["all", "confirmed", *TYPES]:
        result = client.get(
            "/api/loophole/catalog", headers=access._HEADERS,
            params={"classification": selected},
        )
        assert result.status_code == 200
        expected = (
            selected == "all"
            or selected == after_type
            or (selected == "confirmed" and after_type != "not_confirmed")
        )
        assert [row["record_id"] for row in result.json()["records"]] == (
            [record_id] if expected else []
        )
    audit = repo.list_actions(access._USERNAME, session=session)
    assert any(after_type in str(row["detail"]) for row in audit)


@pytest.mark.parametrize("classification", TYPES)
@pytest.mark.parametrize("role_status", ["absent", "revoked"])
def test_classification_denied_without_active_role(client, session, classification, role_status):
    access._access(
        session, role=None if role_status == "absent" else "ccks_expert",
        role_status=role_status,
    )
    record_ids = access._records(session, 2)
    before = access._state(session, record_ids)
    response = client.post(access._ENDPOINT, headers=access._HEADERS, json={
        "record_ids": record_ids, "classification": classification,
    })
    assert response.status_code == 403
    assert access._state(session, record_ids) == before


@pytest.mark.parametrize("payload", [
    {}, {"classification": "unknown"}, {"classification": "fraud_scheme", "is_loophole": False},
    {"classification": "not_confirmed", "is_loophole": True},
])
def test_invalid_or_conflicting_classification_is_rejected(client, session, payload):
    access._access(session, role="ccks_expert")
    record_ids = access._records(session, 1)
    before = access._state(session, record_ids)
    response = client.post(access._ENDPOINT, headers=access._HEADERS, json={
        "record_ids": record_ids, **payload,
    })
    assert response.status_code == 422
    assert access._state(session, record_ids) == before


def test_catalog_legacy_types_and_combined_filters(client, session):
    _create_import_schema(session)
    for number, (positive, category, bank) in enumerate([
        (True, None, "sber"), (False, None, "sber"), (None, None, "sber"),
        (True, "fraud_scheme", "sber"), (True, "fraud_scheme", "vtb"),
    ]):
        repo.insert_record(LoopholeRecord(
            sha256=str(number), title=f"Запись {number}", is_loophole=positive,
            classification=category, bank_slug=bank,
        ), session=session)
    assert len(repo.list_catalog_cases(session=session)) == 4
    assert len(repo.list_catalog_cases(classification="confirmed", session=session)) == 3
    assert len(repo.list_catalog_cases(classification="not_confirmed", session=session)) == 1
    assert repo.list_catalog_cases(classification="vulnerability", session=session)[0][
        "classification"
    ] == "vulnerability"
    found = repo.list_catalog_cases(
        classification="fraud_scheme", bank_slugs=["sber"], query_text="3", session=session,
    )
    assert [row["title"] for row in found] == ["Запись 3"]
    assert client.get(
        "/api/loophole/catalog?classification=unknown", headers=access._HEADERS,
    ).status_code == 422


def test_bulk_classification_keeps_one_result_per_record(client, session):
    access._access(session, role="ccks_expert")
    record_ids = access._records(session, 2)
    response = client.post(access._ENDPOINT, headers=access._HEADERS, json={
        "record_ids": [*record_ids, 999999], "classification": "fraud_scheme",
    })
    assert response.json() == {"updated": 2, "skipped": [999999]}
    assert session.execute(text(
        "SELECT classification FROM loophole_record ORDER BY record_id"
    )).scalars().all() == ["fraud_scheme", "fraud_scheme"]
