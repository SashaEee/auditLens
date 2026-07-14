"""Тест web.py: FastAPI TestClient /search, /chat (SSE), /export, логирование.

Сессия БД подменяется через app.dependency_overrides[get_session] на in-memory SQLite.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit.loophole.web import router, get_session, get_user_id
from bank_audit.loophole import repository as repo
from bank_audit.loophole import keywords as kw_mod
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.hashing import sha256_text

from fastapi import FastAPI


SCHEMA_SQL = open(
    __import__("pathlib").Path(__file__).parent / "test_repository.py"
).read().split('SCHEMA_SQL = """')[1].split('"""')[0]


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
def client(app_session):
    def override_session():
        yield app_session

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_user_id] = lambda: "test-user"
    with TestClient(app) as c:
        yield c


def test_search_empty(client):
    r = client.post("/api/loophole/search", json={
        "period_from": None, "period_to": None, "bank_slugs": [], "query_text": ""
    })
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0


def test_search_returns_records(client, app_session):
    rec = LoopholeRecord(sha256=sha256_text("x"), title="лазейка сбербанк",
                        snippet="скрытая комиссия", bank_slug="sberbank", raw_text="комиссия")
    rid = repo.insert_record(rec, session=app_session)
    repo.update_verdict(rid, is_loophole=True, confidence=0.9, reason="ок", model="m", session=app_session)
    r = client.post("/api/loophole/search", json={
        "bank_slugs": ["sberbank"], "query_text": ""
    })
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_keywords_endpoint(client, app_session):
    kw_mod.seed_keywords(session=app_session)
    r = client.get("/api/loophole/keywords")
    assert r.status_code == 200
    assert len(r.json()["keywords"]) > 0


def test_workspace_create_and_list(client):
    r = client.post("/api/loophole/workspace", json={"name": "ws1"})
    assert r.status_code == 200
    wid = r.json()["workspace_id"]
    r2 = client.get("/api/loophole/workspaces")
    assert r2.status_code == 200
    assert any(w["workspace_id"] == wid for w in r2.json()["workspaces"])


def test_history_empty(client):
    r = client.post("/api/loophole/workspace", json={"name": "ws"})
    wid = r.json()["workspace_id"]
    r2 = client.get(f"/api/loophole/history/{wid}")
    assert r2.status_code == 200
    assert r2.json()["messages"] == []


def test_export_json(client, app_session):
    rec = LoopholeRecord(sha256=sha256_text("e1"), title="лазейка", bank_slug="sberbank")
    rid = repo.insert_record(rec, session=app_session)
    r = client.post("/api/loophole/export", json={"records": [rid], "format": "json"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["record_id"] == rid


def test_export_csv(client, app_session):
    rec = LoopholeRecord(sha256=sha256_text("e2"), title="лазейка", bank_slug="sberbank")
    rid = repo.insert_record(rec, session=app_session)
    r = client.post("/api/loophole/export", json={"records": [rid], "format": "csv"})
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert "лазейка" in r.text


def test_search_logs_action(client, app_session):
    client.post("/api/loophole/search", json={"query_text": "тест", "bank_slugs": []})
    actions = repo.list_actions("test-user", session=app_session)
    assert any(a["action"] == "search" for a in actions)


def test_chat_sse(client):
    """SSE-чат: стримит события. /команда не используется → plain answer."""
    r = client.post("/api/loophole/chat", json={
        "workspace_id": 1, "message": "вопрос", "history": []
    })
    assert r.status_code == 200
    # EventSourceResponse отдаёт text/event-stream.
    assert "event-stream" in r.headers.get("content-type", "")


# ── Тесты новых эндпоинтов: clarify / parsers / table/load ─────────────────
def test_clarify_endpoint(client, monkeypatch):
    """POST /clarify — мок generate_clarifications."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    expected = {"complete": False, "reason": "", "questions": [{"id": "q0", "question": "q?"}]}
    async def fake_gen(question, history=None):
        return expected

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)
    r = client.post("/api/loophole/clarify", json={"question": "лазейка", "history": []})
    assert r.status_code == 200
    assert r.json() == expected


def test_clarify_answer_endpoint(client, monkeypatch):
    """POST /clarify/answer — мок build_enriched_question."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    async def fake_build(question, answers):
        return f"{question} + enriched"

    monkeypatch.setattr(clarify_mod, "build_enriched_question", fake_build)
    r = client.post("/api/loophole/clarify/answer", json={
        "question": "лазейка", "answers": [{"question": "банк?", "selected": ["sberbank"]}]
    })
    assert r.status_code == 200
    assert r.json() == {"enriched_question": "лазейка + enriched"}


def test_table_load_empty(client):
    """POST /table/load без записей."""
    r = client.post("/api/loophole/table/load", json={})
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_table_load_with_record(client, app_session):
    """POST /table/load возвращает запись по фильтру bank_slugs."""
    rec = LoopholeRecord(sha256=sha256_text("tl1"), title="лазейка", bank_slug="vtb")
    repo.insert_record(rec, session=app_session)
    r = client.post("/api/loophole/table/load", json={"bank_slugs": ["vtb"]})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["records"][0]["bank_slug"] == "vtb"


def test_parsers_list_empty(client, monkeypatch):
    """GET /parsers — пустой список через мок registry."""
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(parser_registry, "list_parsers", lambda ws, session=None: [])
    r = client.get("/api/loophole/parsers", params={"workspace_id": 1})
    assert r.status_code == 200
    assert r.json() == {"parsers": []}


def test_parsers_create(client, monkeypatch):
    """POST /parsers — мок generator.generate_parser."""
    from bank_audit.loophole.parsers import generator as parser_generator

    async def fake_gen(user_id, workspace_id, query, *, llm=None, session=None):
        return {"parser_id": 42, "code_path": "/tmp/p.py", "name": "parser"}

    monkeypatch.setattr(parser_generator, "generate_parser", fake_gen)
    r = client.post("/api/loophole/parsers", json={"workspace_id": 1, "query": "комиссии"})
    assert r.status_code == 200
    assert r.json()["parser_id"] == 42


def test_parser_run_not_found(client, monkeypatch):
    """POST /parsers/{id}/run — 404 если парсера нет."""
    monkeypatch.setattr(repo, "get_parser", lambda pid, session=None: None)
    r = client.post("/api/loophole/parsers/999/run")
    assert r.status_code == 404


def test_parser_run_ok(client, monkeypatch):
    """POST /parsers/{id}/run — запуск через мок ParserRunner."""
    from bank_audit.loophole.parsers import runner as runner_mod

    monkeypatch.setattr(repo, "get_parser", lambda pid, session=None: {
        "parser_id": pid, "code_path": "/tmp/p.py", "workspace_id": 1,
    })

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass
        async def start(self):
            return 12345

    monkeypatch.setattr(runner_mod, "ParserRunner", FakeRunner)
    r = client.post("/api/loophole/parsers/7/run")
    assert r.status_code == 200
    assert r.json() == {"parser_id": 7, "pid": 12345}


def test_parser_stop_not_running(client, monkeypatch):
    """POST /parsers/{id}/stop — 404 если не running."""
    from bank_audit.loophole.parsers import runner as runner_mod

    monkeypatch.setattr(runner_mod, "_RUNNING", {})
    r = client.post("/api/loophole/parsers/5/stop")
    assert r.status_code == 404


def test_parser_stop_ok(client, monkeypatch):
    """POST /parsers/{id}/stop — успешная остановка."""
    from bank_audit.loophole.parsers import runner as runner_mod

    stopped = {"called": False}

    class FakeRunner:
        async def stop(self):
            stopped["called"] = True

    fake_running = {11: FakeRunner()}
    monkeypatch.setattr(runner_mod, "_RUNNING", fake_running)
    r = client.post("/api/loophole/parsers/11/stop")
    assert r.status_code == 200
    assert r.json() == {"parser_id": 11, "stopped": True}
    assert stopped["called"]


def test_parser_status_not_found(client, monkeypatch):
    """GET /parsers/{id}/status — 404 если нет нигде."""
    from bank_audit.loophole.parsers import runner as runner_mod
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(runner_mod, "_RUNNING", {})
    monkeypatch.setattr(parser_registry, "get_parser", lambda pid, session=None: None)
    r = client.get("/api/loophole/parsers/777/status")
    assert r.status_code == 404


def test_parser_status_from_db(client, monkeypatch):
    """GET /parsers/{id}/status — статус из БД (не running)."""
    from bank_audit.loophole.parsers import runner as runner_mod
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(runner_mod, "_RUNNING", {})
    monkeypatch.setattr(parser_registry, "get_parser", lambda pid, session=None: {
        "parser_id": pid, "status": "created",
    })
    r = client.get("/api/loophole/parsers/3/status")
    assert r.status_code == 200
    data = r.json()
    assert data["parser_id"] == 3
    assert data["runtime"] is None
    assert data["parser"]["status"] == "created"


def test_parser_delete_ok(client, monkeypatch):
    """DELETE /parsers/{id} — успех."""
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(parser_registry, "delete_parser", lambda pid, session=None: True)
    r = client.delete("/api/loophole/parsers/9")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}


def test_parser_delete_not_found(client, monkeypatch):
    """DELETE /parsers/{id} — 404."""
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(parser_registry, "delete_parser", lambda pid, session=None: False)
    r = client.delete("/api/loophole/parsers/9")
    assert r.status_code == 404
