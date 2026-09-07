"""Ручной статус доступен только действующему администратору или эксперту ЦК КС.

Проверяем реальные HTTP-запросы и SQLite без подмены авторизации. Отказ не
должен менять запись, примеры KB или журнал успешных действий.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit import db
from bank_audit.loophole import repository as repo
from bank_audit.loophole.kb import repository as kb_repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.web import get_session, router

from .conftest import SCHEMA_SQL

_USERNAME = "status-user"
_HEADERS = {"X-Authentik-Username": _USERNAME}
_ENDPOINT = "/api/loophole/records/verdict"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    with engine.connect() as connection:
        connection.connection.executescript(SCHEMA_SQL)
        connection.commit()
    with sessionmaker(bind=engine, expire_on_commit=False)() as current:
        yield current
    engine.dispose()


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.delenv("LOOPHOLE_DEV_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("LOOPHOLE_DEV_GRANT_ALL", raising=False)

    @contextmanager
    def audit_session():
        with sessionmaker(bind=session.get_bind())() as current:
            yield current
            current.commit()

    def request_session():
        yield session

    def unavailable_embedding(_value):
        raise RuntimeError("Эмбеддинг отключён в локальном тесте")

    monkeypatch.setattr(db, "session", audit_session)
    monkeypatch.setattr(kb_repo.embedder, "embed_one", unavailable_embedding)
    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = request_session
    with TestClient(app) as current:
        yield current


def _access(session, *, membership="active", role=None, role_status="active"):
    if membership is not None:
        session.execute(text(
            "INSERT INTO loophole_workspace_membership (username, status) VALUES (:u, :s)"
        ), {"u": _USERNAME, "s": membership})
    if role is not None:
        session.execute(text(
            "INSERT INTO loophole_role_assignment (username, role, status) VALUES (:u, :r, :s)"
        ), {"u": _USERNAME, "r": role, "s": role_status})


def _records(session, count):
    record_ids = []
    for index in range(count):
        record_id = repo.insert_record(LoopholeRecord(
            sha256=f"verdict-access-{index}", title=f"Запись {index}", snippet="Исходный текст",
            status="published", is_loophole=True,
        ), session=session)
        session.execute(text(
            "INSERT INTO loophole_kb_example (record_id, title, description, category) "
            "VALUES (:id, 'Пример', 'Исходный пример', 'manual')"
        ), {"id": record_id})
        record_ids.append(record_id)
    session.commit()
    return record_ids


def _state(session, record_ids):
    return {
        "records": [repo.get_record(record_id, session=session) for record_id in record_ids],
        "examples": [dict(row) for row in session.execute(text(
            "SELECT * FROM loophole_kb_example ORDER BY example_id"
        )).mappings()],
        "actions": [dict(row) for row in session.execute(text(
            "SELECT * FROM loophole_action_log ORDER BY log_id"
        )).mappings()],
    }


@pytest.mark.parametrize("count", [1, 2], ids=["single", "bulk"])
@pytest.mark.parametrize("is_loophole", [False, True], ids=["reject", "confirm"])
@pytest.mark.parametrize(("membership", "role", "role_status"), [
    (None, None, "active"),
    ("active", None, "active"),
    (None, "module_admin", "active"),
    (None, "ccks_expert", "active"),
    ("active", "module_admin", "revoked"),
    ("active", "ccks_expert", "revoked"),
    ("revoked", "module_admin", "active"),
    ("revoked", "ccks_expert", "active"),
])
def test_denied_verdict_preserves_records_kb_and_actions(
    client, session, count, is_loophole, membership, role, role_status,
):
    _access(session, membership=membership, role=role, role_status=role_status)
    record_ids = _records(session, count)
    before = _state(session, record_ids)

    response = client.post(_ENDPOINT, headers=_HEADERS, json={
        "record_ids": record_ids, "is_loophole": is_loophole, "comment": "Попытка изменения",
    })

    assert response.status_code == 403
    assert _state(session, record_ids) == before
    audit = session.execute(text(
        "SELECT action, decision FROM loophole_auth_audit WHERE username = :u"
    ), {"u": _USERNAME}).all()
    expected_action = "membership_check" if membership == "revoked" else "mark_verdict"
    assert (expected_action, "deny") in audit


@pytest.mark.parametrize("role", ["module_admin", "ccks_expert"])
@pytest.mark.parametrize("count", [1, 2], ids=["single", "bulk"])
@pytest.mark.parametrize("is_loophole", [False, True], ids=["reject", "confirm"])
def test_allowed_role_changes_verdict_and_synchronizes_kb(
    client, session, role, count, is_loophole,
):
    _access(session, role=role)
    record_ids = _records(session, count)
    if is_loophole:
        session.execute(text("DELETE FROM loophole_kb_example"))

    response = client.post(_ENDPOINT, headers=_HEADERS, json={
        "record_ids": record_ids, "is_loophole": is_loophole, "comment": "Решение эксперта",
    })

    assert response.status_code == 200
    assert response.json() == {"updated": count, "skipped": []}
    for record_id in record_ids:
        record = repo.get_record(record_id, session=session)
        assert bool(record["is_loophole"]) is is_loophole
        assert record["status"] == "classified"
        assert record["verdict_model"] == "manual"
        assert record["verdict_reason"] == "Решение эксперта"
        example = repo.get_kb_example_by_record(record_id, session=session)
        assert (example is not None) is is_loophole
    assert any(row["action"] == "mark_verdict" for row in repo.list_actions(
        _USERNAME, session=session,
    ))


@pytest.mark.parametrize("role", ["module_admin", "ccks_expert"])
def test_role_revocation_denies_next_verdict_and_removes_capability(client, session, role):
    _access(session, role=role)
    record_ids = _records(session, 1)
    capability = client.get("/api/loophole/contexts", headers=_HEADERS)
    assert capability.json()["capabilities"]["can_mark_verdict"] is True
    allowed = client.post(_ENDPOINT, headers=_HEADERS, json={
        "record_ids": record_ids, "is_loophole": False,
    })
    assert allowed.status_code == 200
    session.execute(text(
        "UPDATE loophole_role_assignment SET status = 'revoked' WHERE username = :u"
    ), {"u": _USERNAME})
    session.commit()
    before = _state(session, record_ids)

    denied = client.post(_ENDPOINT, headers=_HEADERS, json={
        "record_ids": record_ids, "is_loophole": True,
    })

    assert denied.status_code == 403
    assert _state(session, record_ids) == before
    capability = client.get("/api/loophole/contexts", headers=_HEADERS)
    assert capability.json()["capabilities"]["can_mark_verdict"] is False


@pytest.mark.parametrize("membership", [None, "active"])
@pytest.mark.parametrize("role", [None, "module_admin", "ccks_expert"])
def test_context_capability_requires_membership_and_supported_role(
    client, session, membership, role,
):
    _access(session, membership=membership, role=role)
    response = client.get("/api/loophole/contexts", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["capabilities"]["can_mark_verdict"] is (
        membership == "active" and role is not None
    )


@pytest.mark.parametrize("owner", [_USERNAME, "other-user"])
def test_client_workspace_and_role_claims_do_not_grant_verdict_access(client, session, owner):
    _access(session)
    session.execute(text(
        "INSERT INTO loophole_role_assignment (username, role, status) "
        "VALUES ('other-user', 'module_admin', 'active')"
    ))
    workspace_id = repo.create_workspace(owner, "Область", session=session)
    record_ids = _records(session, 1)
    before = _state(session, record_ids)

    response = client.post(_ENDPOINT, headers={
        **_HEADERS, "X-User-Id": "other-user", "X-Role": "module_admin",
        "X-Workspace-Id": str(workspace_id), "X-Capabilities": "can_mark_verdict",
    }, json={
        "record_ids": record_ids, "is_loophole": False, "workspace_id": workspace_id,
        "role": "module_admin", "can_mark_verdict": True,
    })

    assert response.status_code == 403
    assert _state(session, record_ids) == before


def test_unauthenticated_verdict_preserves_data(client, session):
    record_ids = _records(session, 1)
    before = _state(session, record_ids)
    response = client.post(_ENDPOINT, json={"record_ids": record_ids, "is_loophole": False})
    assert response.status_code == 401
    assert _state(session, record_ids) == before
