"""Тест эндпоинтов /api/loophole/parsers: каталог, дедуп 409, PATCH, runs, SSE."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit.loophole import repository as repo
from bank_audit.loophole.parsers import runner as runner_mod
from bank_audit.loophole.web import get_session, get_user_id, router

from .conftest import SCHEMA_SQL


@pytest.fixture
def app_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.connection.executescript(SCHEMA_SQL)
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client(app_session):
    def override_session():
        yield app_session

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_user_id] = lambda: "test-user"
    with TestClient(app) as c:
        yield c


@pytest.fixture
def parser_id(app_session) -> int:
    wid = repo.create_workspace("other-user", "ws", session=app_session)
    return repo.save_parser(
        wid, "p1", "/tmp/p1.py",
        config={"query": "q", "targets": ["https://a.ru/x"]},
        created_by="other-user", source_keys=["a.ru/x"], session=app_session,
    )


@pytest.fixture
def workspace_id(app_session) -> int:
    return repo.create_workspace("test-user", "Заявка на источник", session=app_session)


# ── каталог ──────────────────────────────────────────────────────────────────
def test_catalog_lists_all_users_parsers(client, parser_id):
    # Парсер создан "другим" пользователем — виден всем (общий каталог).
    r = client.get("/api/loophole/parsers")
    assert r.status_code == 200
    parsers = r.json()["parsers"]
    assert len(parsers) == 1
    p = parsers[0]
    assert p["parser_id"] == parser_id
    assert p["records_count"] == 0
    assert p["last_run"] is None
    assert p["created_by"] == "other-user"


# ── заявки на разработку парсера ─────────────────────────────────────────────
def test_parser_request_creates_pending_proposal_only(
    client, app_session, workspace_id, monkeypatch,
):
    from bank_audit.loophole.parsers import generator

    llm_spy = AsyncMock(side_effect=AssertionError("LLM не должен вызываться"))
    monkeypatch.setattr(generator, "generate_parser", llm_spy)

    r = client.post("/api/loophole/parser-requests", json={
        "workspace_id": workspace_id,
        "url": "https://www.a.ru/x/?utm_source=y",
        "description": "Собирать тарифы и комиссии",
    })
    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    proposal = app_session.execute(
        text(
            "SELECT purpose, url, domain, reason, proposed_by, status FROM source_proposal"
        )
    ).mappings().one()
    assert dict(proposal) == {
        "purpose": "loophole_parser",
        "url": "https://www.a.ru/x/?utm_source=y",
        "domain": "a.ru",
        "reason": "Собирать тарифы и комиссии",
        "proposed_by": "test-user",
        "status": "pending",
    }
    assert app_session.execute(text("SELECT COUNT(*) FROM loophole_parser")).scalar_one() == 0
    assert app_session.execute(text("SELECT COUNT(*) FROM loophole_parser_run")).scalar_one() == 0
    llm_spy.assert_not_awaited()


@pytest.mark.parametrize("url", ["ftp://bank.example/tariffs", "mailto:info@bank.example"])
def test_parser_request_rejects_non_web_url(client, workspace_id, url):
    r = client.post("/api/loophole/parser-requests", json={
        "workspace_id": workspace_id, "url": url, "description": "Тарифы",
    })
    assert r.status_code == 422


@pytest.mark.parametrize("url", ["https://t.me/bank_news", "https://telegram.me/bank_news"])
def test_parser_request_rejects_messenger_url_without_branding(client, app_session, workspace_id, url):
    r = client.post("/api/loophole/parser-requests", json={
        "workspace_id": workspace_id, "url": url, "description": "Тарифы",
    })

    assert r.status_code == 422
    assert "telegram" not in r.text.lower()
    assert app_session.execute(text("SELECT COUNT(*) FROM source_proposal")).scalar_one() == 0


def test_parser_request_rejects_existing_pending_domain(client, workspace_id):
    body = {"workspace_id": workspace_id, "url": "https://bank.example/tariffs", "description": "Тарифы"}
    assert client.post("/api/loophole/parser-requests", json=body).status_code == 201
    r = client.post("/api/loophole/parser-requests", json={**body, "url": "https://www.bank.example/fees"})
    assert r.status_code == 409


def test_parser_request_requires_workspace_owner(client, app_session):
    other_workspace_id = repo.create_workspace("other-user", "Чужая область", session=app_session)
    r = client.post("/api/loophole/parser-requests", json={
        "workspace_id": other_workspace_id,
        "url": "https://bank.example/tariffs",
        "description": "Тарифы",
    })
    assert r.status_code == 403
    assert app_session.execute(text("SELECT COUNT(*) FROM source_proposal")).scalar_one() == 0


def test_parser_request_writes_audit_in_same_request(client, app_session, workspace_id):
    r = client.post("/api/loophole/parser-requests", json={
        "workspace_id": workspace_id,
        "url": "https://bank.example/tariffs",
        "description": "Тарифы",
    })
    assert r.status_code == 201
    audit = app_session.execute(
        text(
            "SELECT user_id, workspace_id, action, detail FROM loophole_action_log"
        )
    ).mappings().one()
    assert audit["user_id"] == "test-user"
    assert audit["workspace_id"] == workspace_id
    assert audit["action"] == "parser_development_request_create"


def test_legacy_parser_create_is_not_available(client, workspace_id):
    r = client.post("/api/loophole/parsers", json={
        "workspace_id": workspace_id, "query": "https://bank.example/tariffs",
    })
    assert r.status_code == 405


def test_parser_request_rejects_existing_parser(client, app_session, workspace_id):
    repo.save_parser(
        workspace_id, "Тарифы", "/tmp/parser.py", config={}, created_by="test-user",
        source_keys=["bank.example/tariffs"], session=app_session,
    )
    r = client.post("/api/loophole/parser-requests", json={
        "workspace_id": workspace_id,
        "url": "https://www.bank.example/tariffs",
        "description": "Тарифы",
    })
    assert r.status_code == 409




# ── PATCH расписания ─────────────────────────────────────────────────────────
def test_patch_schedule_valid(client, app_session, parser_id):
    repo.update_parser_status(parser_id, "ready", session=app_session)
    r = client.patch(f"/api/loophole/parsers/{parser_id}", json={
        "cron_expr": "0 5 * * *", "auto_enabled": True, "name": "renamed",
    })
    assert r.status_code == 200
    p = r.json()["parser"]
    assert p["cron_expr"] == "0 5 * * *"
    assert p["auto_enabled"] in (True, 1)
    assert p["next_run_at"] is not None
    assert p["name"] == "renamed"
    assert p["last_edited_by"] == "test-user"


def test_patch_schedule_rejects_parser_with_failed_validation(client, app_session, parser_id):
    """Расписание доступно только после успешной валидации."""
    repo.update_parser_status(parser_id, "validation_failed", session=app_session)

    r = client.patch(f"/api/loophole/parsers/{parser_id}", json={
        "cron_expr": "0 5 * * *", "auto_enabled": True,
    })

    assert r.status_code == 409
    assert "валидац" in r.json()["detail"].lower()


def test_patch_invalid_cron_422(client, parser_id):
    r = client.patch(f"/api/loophole/parsers/{parser_id}", json={
        "cron_expr": "not-a-cron",
    })
    assert r.status_code == 422
    assert "invalid cron" in r.json()["detail"]


def test_patch_not_found_404(client):
    r = client.patch("/api/loophole/parsers/9999", json={"auto_enabled": False})
    assert r.status_code == 404


def test_patch_clear_cron(client, app_session, parser_id):
    """Пустая строка cron_expr очищает расписание (NULL), поле не залипает."""
    repo.update_parser_status(parser_id, "ready", session=app_session)
    r = client.patch(f"/api/loophole/parsers/{parser_id}", json={
        "cron_expr": "0 5 * * *", "auto_enabled": True,
    })
    assert r.status_code == 200
    r = client.patch(f"/api/loophole/parsers/{parser_id}", json={
        "cron_expr": "", "auto_enabled": False,
    })
    assert r.status_code == 200
    p = r.json()["parser"]
    assert p["cron_expr"] is None
    assert p["next_run_at"] is None


# ── run / runs / stop ────────────────────────────────────────────────────────
def test_manual_run_returns_run_id(client, app_session, parser_id, monkeypatch):
    repo.update_parser_status(parser_id, "ready", session=app_session)
    run_mock = AsyncMock(return_value=42)
    monkeypatch.setattr(runner_mod, "run", run_mock)
    r = client.post(f"/api/loophole/parsers/{parser_id}/run")
    assert r.status_code == 200
    assert r.json()["run_id"] == 42
    # Контракт: без request-session — фон коммитит через свои db.session().
    run_mock.assert_awaited_once_with(parser_id, "manual")


def test_manual_run_rejects_parser_without_successful_validation(client, parser_id, monkeypatch):
    """Невалидный парсер нельзя запустить ни вручную, ни в обход UI."""
    run_mock = AsyncMock(return_value=42)
    monkeypatch.setattr(runner_mod, "run", run_mock)

    response = client.post(f"/api/loophole/parsers/{parser_id}/run")

    assert response.status_code == 409
    assert "валидац" in response.json()["detail"].lower()
    run_mock.assert_not_awaited()


def test_manual_run_conflict_409(client, parser_id, monkeypatch):
    monkeypatch.setattr(
        runner_mod, "run",
        AsyncMock(side_effect=RuntimeError("parser 1 already running")),
    )
    r = client.post(f"/api/loophole/parsers/{parser_id}/run")
    assert r.status_code == 409


def test_runs_history(client, app_session, parser_id):
    rid = repo.create_run(parser_id, "manual", session=app_session)
    repo.finish_run(rid, "empty", session=app_session)
    r = client.get(f"/api/loophole/parsers/{parser_id}/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert runs[0]["run_id"] == rid
    assert runs[0]["status"] == "empty"


# ── SSE лог-стрим ────────────────────────────────────────────────────────────
def test_log_stream_finished_run(client):
    runner_mod._FINISHED.clear()
    runner_mod._LOG_TAIL.clear()
    runner_mod.finish_stream(7, {"status": "success", "items_new": 3})
    r = client.get("/api/loophole/parsers/1/log/stream?run_id=7")
    assert r.status_code == 200
    assert "event: done" in r.text
    assert "success" in r.text


# ── heal ─────────────────────────────────────────────────────────────────────
def test_heal_503_without_nanobot(client, parser_id, monkeypatch):
    from bank_audit.loophole.parsers import healer
    monkeypatch.setattr(healer, "nanobot_available", lambda: False)
    r = client.post(f"/api/loophole/parsers/{parser_id}/heal")
    assert r.status_code == 503


def test_heal_ok(client, parser_id, monkeypatch):
    from bank_audit.loophole.parsers import healer
    monkeypatch.setattr(healer, "nanobot_available", lambda: True)
    monkeypatch.setattr(healer, "heal", AsyncMock(return_value=55))
    r = client.post(f"/api/loophole/parsers/{parser_id}/heal")
    assert r.status_code == 200
    assert r.json()["heal_run_id"] == 55


# ── delete ───────────────────────────────────────────────────────────────────
def test_delete_running_conflict_409(client, parser_id):
    runner_mod._RUNNING[parser_id] = object()
    try:
        r = client.delete(f"/api/loophole/parsers/{parser_id}")
        assert r.status_code == 409
    finally:
        runner_mod._RUNNING.clear()


def test_delete_ok(client, app_session, parser_id, tmp_path):
    code = tmp_path / "p.py"
    code.write_text("print('[]')", encoding="utf-8")
    repo.update_parser_code_path(parser_id, str(code), session=app_session)
    r = client.delete(f"/api/loophole/parsers/{parser_id}")
    assert r.status_code == 200
    assert not code.exists()
