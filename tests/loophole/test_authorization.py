"""Тест server-side авторизации модуля «Лазейки» (story 1.1).

Граница доверия: identity — только trusted nginx-заголовки X-Authentik-*
(get_current_user). Отсутствие membership-истории даёт default base access;
любая существующая история без active-строки означает explicit revoke.
Привилегированные queue/admin требуют active membership и active role,
перечитываемые из БД на каждом запросе. X-User-Id не доверяем.

Покрывает I/O-матрицу спеки: базовые и привилегированные контексты, 403 без
active membership+role, active-first исторические строки, отсутствие утечки
защищённых данных при deny, explicit dev bypass, 401 без trusted principal и
запрет создания workspace до авторизации. Плюс contract-тест миграции 042.

Без сети и реальной БД: in-memory SQLite (паттерн test_web.py), авторизация
НЕ переопределяется — ходим реальными заголовками X-Authentik-Username.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit import db as db_mod
from bank_audit.hashing import sha256_text
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.web import get_session, router

from .conftest import SCHEMA_SQL

MIGRATION_042 = (
    Path(__file__).resolve().parents[2] / "migrations" / "042_loophole_authorization.sql"
)

# Маркер защищённых данных: при deny его не должно быть ни в JSON, ни в теле.
_SECRET_TITLE = "СЕКРЕТНАЯ ЛАЗЕЙКА ЦК"


@pytest.fixture
def app_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.connection.executescript(SCHEMA_SQL)
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client(app_session, monkeypatch):
    """TestClient БЕЗ override авторизации: реальный путь trusted principal.

    log_auth_event пишет аудит через отдельную db.session() — направляем её
    в тестовую БД (prod-семантика: свой commit, независимый от request-сессии).
    """
    engine = app_session.get_bind()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_mod, "session", lambda: _engine_session(SessionLocal))
    monkeypatch.delenv("LOOPHOLE_DEV_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("LOOPHOLE_DEV_GRANT_ALL", raising=False)

    def override_session():
        yield app_session

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as c:
        yield c


# ── Сидирование membership / ролей (админ-назначение — история 1.5, здесь SQL) ──
def _grant_membership(session, username: str, status: str | None = "active") -> None:
    session.execute(
        text(
            "INSERT INTO loophole_workspace_membership (username, status) "
            "VALUES (:u, :st)"
        ),
        {"u": username, "st": status},
    )


def _grant_role(session, username: str, role: str = "ccks_expert", status: str = "active") -> None:
    session.execute(
        text(
            "INSERT INTO loophole_role_assignment (username, role, status) "
            "VALUES (:u, :r, :st)"
        ),
        {"u": username, "r": role, "st": status},
    )


def _seed_queue_record(session) -> int:
    rec = LoopholeRecord(
        sha256=sha256_text("queue1"), title=_SECRET_TITLE,
        snippet="кейс очереди", bank_slug="sberbank", url="https://x.ru/case",
    )
    rid = repo.insert_record(rec, session=session)
    repo.update_verdict(
        rid, is_loophole=True, confidence=0.8, reason="кандидат", model="llm",
        session=session,
    )
    return rid


def _auth(username: str) -> dict:
    return {"X-Authentik-Username": username, "X-Authentik-Name": username}


@contextmanager
def _engine_session(SessionLocal):
    """Семантика prod `db.session()`: commit при успехе, rollback на исключении."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ── 401/403 без trusted principal ───────────────────────────────────────────
def test_contexts_without_principal_401(client):
    r = client.get("/api/loophole/contexts")
    assert r.status_code == 401


def test_local_dev_auth_enabled_by_explicit_env(client, app_session, monkeypatch):
    """Локальный bypass доступен только по явному dev-флагу."""
    _grant_membership(app_session, "local-dev")
    monkeypatch.setenv("LOOPHOLE_DEV_AUTH_ENABLED", "1")

    r = client.get("/api/loophole/contexts")

    assert r.status_code == 200
    assert {context["id"] for context in r.json()["contexts"]} == {
        "catalog", "sources", "ai_research",
    }


def test_dev_grant_all_gives_any_principal_all_module_contexts(client, monkeypatch):
    """Только явный dev-флаг снимает membership и role gates локального модуля."""
    monkeypatch.setenv("LOOPHOLE_DEV_GRANT_ALL", "1")

    headers = _auth("temporary-user")
    r = client.get("/api/loophole/contexts", headers=headers)
    queue = client.get("/api/loophole/queue", headers=headers)
    admin = client.get("/api/loophole/admin/roles", headers=headers)

    assert r.status_code == 200
    assert {context["id"] for context in r.json()["contexts"]} == {
        "catalog", "sources", "ai_research", "queue", "admin",
    }
    assert queue.status_code == 200
    assert queue.json() == {"records": [], "count": 0}
    assert admin.status_code == 200
    assert admin.json() == {"roles": [], "active_experts": 0, "max_experts": 5}


def test_dev_grant_all_authenticates_local_user_without_sso(client, monkeypatch):
    """Локальный запуск не требует одновременно включать два dev-переключателя."""
    monkeypatch.setenv("LOOPHOLE_DEV_GRANT_ALL", "1")

    r = client.get("/api/loophole/contexts")

    assert r.status_code == 200
    assert {context["id"] for context in r.json()["contexts"]} == {
        "catalog", "sources", "ai_research", "queue", "admin",
    }


def test_dev_grant_all_requires_exactly_one(client, monkeypatch):
    """Нечёткие значения флага не ослабляют авторизацию по ошибке конфигурации."""
    monkeypatch.setenv("LOOPHOLE_DEV_GRANT_ALL", "true")

    r = client.get("/api/loophole/contexts", headers=_auth("temporary-user"))

    assert r.status_code == 200
    assert {context["id"] for context in r.json()["contexts"]} == {
        "catalog",
        "sources",
        "ai_research",
    }


def test_x_user_id_header_not_trusted(client):
    """Never: X-User-Id от клиента не является identity."""
    r = client.get("/api/loophole/contexts", headers={"X-User-Id": "admin"})
    assert r.status_code == 401


def test_data_endpoint_without_principal_401(client):
    """Отказ до чтения данных, а не только на /contexts."""
    r = client.get("/api/loophole/records")
    assert r.status_code == 401


def test_contexts_authenticated_without_membership_gets_base_access(client):
    r = client.get("/api/loophole/contexts", headers=_auth("stranger"))
    queue = client.get("/api/loophole/queue", headers=_auth("stranger"))

    assert r.status_code == 200
    assert {context["id"] for context in r.json()["contexts"]} == {
        "catalog",
        "sources",
        "ai_research",
    }
    assert queue.status_code == 403
    assert "records" not in queue.json()


@pytest.mark.parametrize(
    "status",
    [
        pytest.param("revoked", id="revoked"),
        pytest.param(None, id="null"),
        pytest.param("suspended", id="unknown"),
    ],
)
def test_existing_non_active_membership_denies_contexts_and_queue(
    client, app_session, status
):
    _grant_membership(app_session, "ex-member", status=status)
    _seed_queue_record(app_session)

    r = client.get("/api/loophole/contexts", headers=_auth("ex-member"))
    queue = client.get("/api/loophole/queue", headers=_auth("ex-member"))

    assert r.status_code == 403
    assert queue.status_code == 403
    assert "records" not in queue.json()
    assert _SECRET_TITLE not in queue.text
    assert "https://x.ru/case" not in queue.text


# ── Видимость контекстов по роли ────────────────────────────────────────────
def test_contexts_member_gets_catalog_and_research(client, app_session):
    _grant_membership(app_session, "analyst")
    r = client.get("/api/loophole/contexts", headers=_auth("analyst"))
    assert r.status_code == 200
    contexts = r.json()["contexts"]
    ids = {c["id"] for c in contexts}
    assert ids == {"catalog", "sources", "ai_research"}
    titles = {c["title"] for c in contexts}
    assert "Общая база" in titles
    assert "Новое AI-исследование" in titles


def test_contexts_expert_also_gets_queue(client, app_session):
    _grant_membership(app_session, "expert")
    _grant_role(app_session, "expert")
    r = client.get("/api/loophole/contexts", headers=_auth("expert"))
    assert r.status_code == 200
    contexts = r.json()["contexts"]
    ids = {c["id"] for c in contexts}
    assert ids == {"catalog", "sources", "ai_research", "queue"}
    queue = next(c for c in contexts if c["id"] == "queue")
    assert queue["title"] == "Очередь верификации"


def test_role_without_active_membership_gets_base_contexts_and_queue_403(
    client, app_session
):
    _grant_role(app_session, "role-only")
    _seed_queue_record(app_session)

    contexts = client.get("/api/loophole/contexts", headers=_auth("role-only"))
    queue = client.get("/api/loophole/queue", headers=_auth("role-only"))

    assert contexts.status_code == 200
    assert {context["id"] for context in contexts.json()["contexts"]} == {
        "catalog",
        "sources",
        "ai_research",
    }
    assert queue.status_code == 403
    assert "records" not in queue.json()
    assert _SECRET_TITLE not in queue.text


def test_admin_role_without_active_membership_gets_base_contexts_and_admin_403(
    client, app_session
):
    _grant_role(app_session, "admin-role-only", role="module_admin")
    _grant_role(app_session, "secret-expert")

    contexts = client.get("/api/loophole/contexts", headers=_auth("admin-role-only"))
    admin = client.get("/api/loophole/admin/roles", headers=_auth("admin-role-only"))

    assert contexts.status_code == 200
    assert {context["id"] for context in contexts.json()["contexts"]} == {
        "catalog",
        "sources",
        "ai_research",
    }
    assert admin.status_code == 403
    assert "roles" not in admin.json()
    assert "secret-expert" not in admin.text


def test_active_membership_wins_history_but_queue_requires_active_role(
    client, app_session
):
    _grant_membership(app_session, "returning-expert", status="revoked")
    _grant_membership(app_session, "returning-expert", status="active")
    _grant_role(app_session, "returning-expert", status="revoked")
    record_id = _seed_queue_record(app_session)

    contexts_without_role = client.get(
        "/api/loophole/contexts", headers=_auth("returning-expert")
    )
    queue_without_role = client.get(
        "/api/loophole/queue", headers=_auth("returning-expert")
    )

    assert contexts_without_role.status_code == 200
    assert {context["id"] for context in contexts_without_role.json()["contexts"]} == {
        "catalog",
        "sources",
        "ai_research",
    }
    assert queue_without_role.status_code == 403
    assert "records" not in queue_without_role.json()
    assert _SECRET_TITLE not in queue_without_role.text

    _grant_role(app_session, "returning-expert", status="active")
    contexts_with_role = client.get(
        "/api/loophole/contexts", headers=_auth("returning-expert")
    )
    queue_with_role = client.get(
        "/api/loophole/queue", headers=_auth("returning-expert")
    )

    assert {context["id"] for context in contexts_with_role.json()["contexts"]} == {
        "catalog",
        "sources",
        "ai_research",
        "queue",
    }
    assert queue_with_role.status_code == 200
    assert any(record["record_id"] == record_id for record in queue_with_role.json()["records"])


# ── Очередь: server-side deny без утечки данных ─────────────────────────────
def test_queue_member_without_role_403_no_protected_data(client, app_session):
    _grant_membership(app_session, "analyst")
    _seed_queue_record(app_session)
    r = client.get("/api/loophole/queue", headers=_auth("analyst"))
    assert r.status_code == 403
    # Никакие данные очереди/кейсов/источников не попадают в ответ.
    assert "records" not in r.json()
    assert _SECRET_TITLE not in r.text
    assert "https://x.ru/case" not in r.text


def test_queue_expert_200_with_records(client, app_session):
    _grant_membership(app_session, "expert")
    _grant_role(app_session, "expert")
    rid = _seed_queue_record(app_session)
    r = client.get("/api/loophole/queue", headers=_auth("expert"))
    assert r.status_code == 200
    records = r.json()["records"]
    assert any(rec["record_id"] == rid for rec in records)


def test_queue_deny_writes_redacted_audit(client, app_session):
    _grant_membership(app_session, "analyst")
    client.get("/api/loophole/queue", headers=_auth("analyst"))
    rows = app_session.execute(
        text(
            "SELECT action, decision FROM loophole_auth_audit "
            "WHERE username = 'analyst'"
        )
    ).all()
    assert ("queue_access", "deny") in [(row[0], row[1]) for row in rows]


def test_deny_audit_survives_request_session_rollback(app_session, monkeypatch):
    """Prod-семантика: request-сессия ОТКАТЫВАЕТСЯ на HTTPException
    (yield-dependency get_session → db.session() → rollback), поэтому
    deny-аудит обязан писаться в отдельной сессии с собственным commit —
    иначе журнал loophole_auth_audit в проде останется пустым.
    """
    engine = app_session.get_bind()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    _grant_membership(app_session, "stranger", status="revoked")
    app_session.commit()
    # Аудит (db.session() внутри authorization) направляем в тестовую БД.
    monkeypatch.setattr(db_mod, "session", lambda: _engine_session(SessionLocal))

    def override_session():
        # Request-сессия с prod-семантикой: rollback на HTTPException.
        with _engine_session(SessionLocal) as s:
            yield s

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as c:
        r = c.get("/api/loophole/contexts", headers=_auth("stranger"))
    assert r.status_code == 403
    rows = app_session.execute(
        text(
            "SELECT action, decision FROM loophole_auth_audit "
            "WHERE username = 'stranger'"
        )
    ).all()
    assert ("membership_check", "deny") in [(row[0], row[1]) for row in rows]


def test_revoked_role_denies_next_request(client, app_session):
    """Отзыв роли действует на СЛЕДУЮЩИЙ запрос (перечитывание из БД)."""
    _grant_membership(app_session, "expert")
    _grant_role(app_session, "expert")
    _seed_queue_record(app_session)
    r1 = client.get("/api/loophole/queue", headers=_auth("expert"))
    assert r1.status_code == 200
    app_session.execute(
        text(
            "UPDATE loophole_role_assignment SET status = 'revoked' "
            "WHERE username = 'expert'"
        )
    )
    r2 = client.get("/api/loophole/queue", headers=_auth("expert"))
    assert r2.status_code == 403
    assert _SECRET_TITLE not in r2.text
    # Контексты тоже пересчитываются: очередь пропадает.
    r3 = client.get("/api/loophole/contexts", headers=_auth("expert"))
    assert {c["id"] for c in r3.json()["contexts"]} == {
        "catalog", "sources", "ai_research",
    }


# ── Workspace не создаётся до авторизации ───────────────────────────────────
def test_workspace_not_created_before_authorization(client, app_session):
    r = client.post(
        "/api/loophole/workspace", json={"name": "default"},
    )
    assert r.status_code == 401
    count = app_session.execute(
        text("SELECT COUNT(*) FROM loophole_workspace")
    ).scalar_one()
    assert count == 0


# ── Ownership legacy workspace/history/chat ─────────────────────────────────
def test_history_foreign_workspace_403(client, app_session):
    _grant_membership(app_session, "analyst")
    _grant_membership(app_session, "intruder")
    wid = repo.create_workspace("analyst", "ws", session=app_session)
    r_forbidden = client.get(
        f"/api/loophole/history/{wid}", headers=_auth("intruder"),
    )
    assert r_forbidden.status_code == 403
    r_owner = client.get(f"/api/loophole/history/{wid}", headers=_auth("analyst"))
    assert r_owner.status_code == 200


def test_chat_foreign_workspace_403(client, app_session):
    _grant_membership(app_session, "analyst")
    _grant_membership(app_session, "intruder")
    wid = repo.create_workspace("analyst", "ws", session=app_session)
    r = client.post(
        "/api/loophole/chat",
        json={"workspace_id": wid, "message": "вопрос", "history": []},
        headers=_auth("intruder"),
    )
    assert r.status_code == 403


# ── Structural contract-тест миграции 042 ───────────────────────────────────
def test_migration_042_file_exists():
    assert MIGRATION_042.exists(), "миграция 042_loophole_authorization.sql отсутствует"
    sql = MIGRATION_042.read_text(encoding="utf-8")
    assert sql.strip(), "миграция 042 пустая"


def test_migration_042_creates_authorization_schema():
    sql = MIGRATION_042.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS loophole_principal" in sql
    assert "CREATE TABLE IF NOT EXISTS loophole_workspace_membership" in sql
    assert "CREATE TABLE IF NOT EXISTS loophole_role_assignment" in sql
    assert "CREATE TABLE IF NOT EXISTS loophole_auth_audit" in sql
    # Роль ЦК КС зафиксирована в схеме (назначение/отзыв — история 1.5).
    assert "ccks_expert" in sql


def test_migration_042_idempotent_and_greenplum_safe():
    """Идемпотентность — IF NOT EXISTS; Greenplum 6 — без PRIMARY KEY / UNIQUE."""
    sql = MIGRATION_042.read_text(encoding="utf-8")
    assert sql.count("CREATE TABLE IF NOT EXISTS") >= 4
    lines = [line.split("--")[0] for line in sql.splitlines()]
    body = "\n".join(lines).upper()
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body


def test_migration_042_restricts_role_values_db_side():
    """Ролевое назначение не принимает произвольную строку на стороне БД.

    Проверяется сам CHECK из migration 042, а не только application-level
    фильтрация: последняя не защищает от прямого SQL или будущего вызывающего
    кода. SQLite выполняет переносимое SQL-выражение CHECK как поведенческую
    проверку контракта; Greenplum 6 поддерживает CHECK без trigger/UNIQUE.
    """
    sql = MIGRATION_042.read_text(encoding="utf-8")
    table = re.search(
        r"CREATE TABLE IF NOT EXISTS loophole_role_assignment \((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert table, "в migration 042 нет DDL loophole_role_assignment"
    constraint = re.search(r"CHECK\s*\([^\n]+\)", table.group(1), re.IGNORECASE)
    assert constraint, "роль должна быть ограничена DB-side CHECK"

    contract = constraint.group(0)
    normalized = re.sub(r"\s+", " ", contract).lower()
    assert "ccks_expert" in normalized
    assert "module_admin" in normalized

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE role_contract (role TEXT NOT NULL {contract})"))
        connection.execute(text("INSERT INTO role_contract (role) VALUES ('ccks_expert')"))
        connection.execute(text("INSERT INTO role_contract (role) VALUES ('module_admin')"))
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO role_contract (role) VALUES ('arbitrary_role')"))
