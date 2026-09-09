"""Пагинация общей базы: limit/offset, total и валидация параметров /catalog."""
from __future__ import annotations

from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from tests.loophole import test_record_verdict_authorization as access

session = access.session
client = access.client


def _seed_records(session, count: int) -> None:
    for index in range(count):
        repo.insert_record(
            LoopholeRecord(
                sha256=f"page-{index}", title=f"Запись {index}", is_loophole=True,
            ),
            session=session,
        )


def test_catalog_endpoint_caps_limit_and_validates_params(client, session):
    access._access(session)
    _seed_records(session, 3)

    ok = client.get("/api/loophole/catalog", headers=access._HEADERS)
    assert ok.status_code == 200
    body = ok.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["total"] == 3
    assert body["count"] == 3

    for query in ("limit=51", "limit=0", "offset=-1"):
        response = client.get(f"/api/loophole/catalog?{query}", headers=access._HEADERS)
        assert response.status_code == 422, query


def test_catalog_total_and_offset_paginate_without_overlap(client, session):
    access._access(session)
    _seed_records(session, 60)
    assert repo.count_catalog_cases(session=session) == 60

    first = client.get("/api/loophole/catalog", headers=access._HEADERS).json()
    second = client.get("/api/loophole/catalog?offset=50", headers=access._HEADERS).json()

    assert first["total"] == 60 and len(first["records"]) == 50
    assert second["total"] == 60 and len(second["records"]) == 10
    first_ids = {row["record_id"] for row in first["records"]}
    assert first_ids.isdisjoint(row["record_id"] for row in second["records"])


def test_count_catalog_cases_respects_the_same_filters(session):
    _seed_records(session, 4)
    repo.insert_record(
        LoopholeRecord(sha256="other", title="Не лазейка", is_loophole=False),
        session=session,
    )

    assert repo.count_catalog_cases(session=session) == 5
    assert repo.count_catalog_cases(classification="confirmed", session=session) == 4
    # SQLite LOWER() не сворачивает кириллицу — паттерн без букв, как в
    # test_catalog_classification.
    assert repo.count_catalog_cases(query_text="1", session=session) == 1
    assert repo.count_catalog_cases(
        classification="not_confirmed", session=session,
    ) == 1


def test_catalog_all_includes_not_confirmed_and_confirmed_excludes_it(client, session):
    access._access(session)
    _seed_records(session, 2)
    repo.insert_record(
        LoopholeRecord(
            sha256="neg", title="Явно не лазейка",
            is_loophole=False, classification="not_confirmed",
        ),
        session=session,
    )

    all_body = client.get("/api/loophole/catalog", headers=access._HEADERS).json()
    confirmed = client.get(
        "/api/loophole/catalog?classification=confirmed", headers=access._HEADERS,
    ).json()

    assert all_body["total"] == 3
    assert all_body["count"] == 3
    assert confirmed["total"] == 2
    assert confirmed["count"] == 2
    titles = {row["title"] for row in confirmed["records"]}
    assert "Явно не лазейка" not in titles
