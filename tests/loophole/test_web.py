"""Тест web.py: FastAPI TestClient /search, /chat (SSE), /export, логирование.

Сессия БД подменяется через app.dependency_overrides[get_session] на in-memory SQLite.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit.hashing import sha256_text
from bank_audit.loophole import keywords as kw_mod
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.web import get_session, get_user_id, router

from .conftest import SCHEMA_SQL


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
    rec = LoopholeRecord(
        sha256=sha256_text("e1"),
        title="лазейка",
        bank_slug="sberbank",
        status="published",
        is_loophole=True,
    )
    rid = repo.insert_record(rec, session=app_session)
    r = client.post("/api/loophole/export", json={"records": [rid], "format": "json"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["record_id"] == rid


def test_export_csv(client, app_session):
    rec = LoopholeRecord(
        sha256=sha256_text("e2"),
        title="лазейка",
        bank_slug="sberbank",
        status="published",
        is_loophole=True,
    )
    rid = repo.insert_record(rec, session=app_session)
    r = client.post("/api/loophole/export", json={"records": [rid], "format": "csv"})
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert "лазейка" in r.text


def test_export_csv_only_selected(client, app_session):
    """Выгружаются ТОЛЬКО переданные ids, а не все записи таблицы."""
    rec1 = LoopholeRecord(
        sha256=sha256_text("s1"),
        title="лазейка выделенная",
        bank_slug="sberbank",
        status="published",
        is_loophole=True,
    )
    rec2 = LoopholeRecord(
        sha256=sha256_text("s2"),
        title="лазейка невыделенная",
        bank_slug="vtb",
        status="published",
        is_loophole=True,
    )
    rid1 = repo.insert_record(rec1, session=app_session)
    repo.insert_record(rec2, session=app_session)
    r = client.post("/api/loophole/export", json={"records": [rid1], "format": "csv"})
    assert r.status_code == 200
    assert "лазейка выделенная" in r.text
    assert "лазейка невыделенная" not in r.text


def test_export_over_limit(client):
    """Более 10000 ids за раз — отказ с понятной ошибкой."""
    r = client.post(
        "/api/loophole/export",
        json={"records": list(range(10001)), "format": "csv"},
    )
    assert r.status_code == 400
    assert "10000" in r.json()["detail"]


def test_search_logs_action(client, app_session):
    client.post("/api/loophole/search", json={"query_text": "тест", "bank_slugs": []})
    actions = repo.list_actions("test-user", session=app_session)
    assert any(a["action"] == "search" for a in actions)


def test_chat_sse(client):
    """SSE-чат: стримит события. /команда не используется → plain answer."""
    # Story 1.1: /chat проверяет ownership — workspace должен существовать
    # и принадлежать текущему пользователю (override → "test-user").
    wid = client.post("/api/loophole/workspace", json={"name": "ws"}).json()["workspace_id"]
    r = client.post("/api/loophole/chat", json={
        "workspace_id": wid, "message": "вопрос", "history": []
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
    """POST /clarify/answer — token clarification превращается в execution token."""
    from bank_audit.loophole.chat import clarify as clarify_mod

    async def fake_generate(question, history=None):
        return {
            "complete": False,
            "reason": "не указан банк",
            "questions": [{"id": "bank", "question": "Какой банк?"}],
        }

    async def fake_build(question, answers):
        return f"{question} + enriched"

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_generate)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", fake_build)
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "clarification"}
    ).json()["workspace_id"]
    challenge = client.post(
        "/api/loophole/clarify",
        json={"question": "лазейка", "history": [], "workspace_id": workspace_id},
    )
    token = challenge.json().get("clarification_token")
    r = client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": "лазейка",
            "answers": [{"question": "банк?", "selected": ["sberbank"]}],
            "clarification_token": token,
        },
    )
    assert r.status_code == 200
    assert r.json()["enriched_question"] == "лазейка + enriched"
    assert r.json().get("execution_token")


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
    """GET /parsers — пустой список через мок registry (общий каталог)."""
    from bank_audit.loophole.parsers import registry as parser_registry

    monkeypatch.setattr(parser_registry, "list_catalog", lambda session=None: [])
    r = client.get("/api/loophole/parsers", params={"workspace_id": 1})
    assert r.status_code == 200
    assert r.json() == {"parsers": []}


def test_parser_request_creates_pending_request(client, app_session, monkeypatch):
    """POST /parser-requests не вызывает генератор и возвращает pending-заявку."""
    from bank_audit.loophole.parsers import generator as parser_generator

    generator_spy = AsyncMock(side_effect=AssertionError("Генератор не должен вызываться"))
    monkeypatch.setattr(parser_generator, "generate_parser", generator_spy)
    workspace_id = repo.create_workspace("test-user", session=app_session)

    r = client.post("/api/loophole/parser-requests", json={
        "workspace_id": workspace_id,
        "url": "https://b.ru/y",
        "description": "Комиссии и исключения",
    })
    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    generator_spy.assert_not_awaited()


def test_parser_run_not_found(client, monkeypatch):
    """POST /parsers/{id}/run — 404 если парсера нет."""
    monkeypatch.setattr(repo, "get_parser", lambda pid, session=None: None)
    r = client.post("/api/loophole/parsers/999/run")
    assert r.status_code == 404


def test_parser_run_ok(client, app_session, monkeypatch):
    """POST /parsers/{id}/run — запуск через мок runner.run, возвращает run_id."""
    from bank_audit.loophole.parsers import runner as runner_mod

    workspace_id = repo.create_workspace("test-user", session=app_session)
    parser_id = repo.save_parser(
        workspace_id,
        "готовый",
        "/tmp/ready.py",
        config={"query": "q", "targets": ["https://example.test"]},
        session=app_session,
    )
    repo.update_parser_status(parser_id, "ready", session=app_session)
    monkeypatch.setattr(runner_mod, "run", AsyncMock(return_value=99))
    r = client.post(f"/api/loophole/parsers/{parser_id}/run")
    assert r.status_code == 200
    assert r.json() == {"parser_id": parser_id, "run_id": 99}


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
    from bank_audit.loophole.parsers import registry as parser_registry
    from bank_audit.loophole.parsers import runner as runner_mod

    monkeypatch.setattr(runner_mod, "_RUNNING", {})
    monkeypatch.setattr(parser_registry, "get_parser", lambda pid, session=None: None)
    r = client.get("/api/loophole/parsers/777/status")
    assert r.status_code == 404


def test_parser_status_from_db(client, monkeypatch):
    """GET /parsers/{id}/status — статус из БД (не running)."""
    from bank_audit.loophole.parsers import registry as parser_registry
    from bank_audit.loophole.parsers import runner as runner_mod

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


# ── Content endpoints ────────────────────────────────────────────────────────
def test_record_content_ok(client, app_session):
    rec = LoopholeRecord(sha256=sha256_text("ct1"), title="t", url="https://x.ru",
                         snippet="s", raw_text="ПОЛНЫЙ ТЕКСТ",
                         content_status="full", raw_text_len=12,
                         raw_text_truncated=True)
    rid = repo.insert_record(rec, session=app_session)
    r = client.get(f"/api/loophole/records/{rid}/content")
    assert r.status_code == 200
    d = r.json()
    assert d["record_id"] == rid
    assert d["raw_text"] == "ПОЛНЫЙ ТЕКСТ"
    assert d["content_status"] == "full"
    assert d["raw_text_len"] == 12
    assert d["raw_text_truncated"] is True


def test_record_content_404(client):
    r = client.get("/api/loophole/records/999999/content")
    assert r.status_code == 404


def test_backfill_content_updates_legacy(client, app_session, monkeypatch):
    from bank_audit.loophole import content_fetch

    rec = LoopholeRecord(sha256=sha256_text("bf1"), title="t",
                         url="https://x.ru/old", snippet="сниппет",
                         raw_text="сниппет")  # content_status NULL → очередь
    rid = repo.insert_record(rec, session=app_session)

    monkeypatch.setattr(
        content_fetch, "fetch_full_content",
        lambda url, **kw: content_fetch.FullContent(
            text="ДОГРУЖЕННЫЙ ПОЛНЫЙ ТЕКСТ", status=content_fetch.STATUS_FULL,
            length=24, truncated=False),
    )
    r = client.post("/api/loophole/records/backfill-content",
                    json={"limit": 10, "delay_ms": 0})
    assert r.status_code == 200
    d = r.json()
    assert d["processed"] == 1
    assert d["updated"] == 1
    assert d["remaining"] == 0
    row = repo.get_record(rid, session=app_session)
    assert row["raw_text"] == "ДОГРУЖЕННЫЙ ПОЛНЫЙ ТЕКСТ"
    assert row["content_status"] == "full"


def test_backfill_content_fetch_failed_keeps_text(client, app_session, monkeypatch):
    from bank_audit.loophole import content_fetch

    rec = LoopholeRecord(sha256=sha256_text("bf2"), title="t",
                         url="https://x.ru/dead", snippet="важный сниппет",
                         raw_text="важный сниппет")
    rid = repo.insert_record(rec, session=app_session)

    monkeypatch.setattr(
        content_fetch, "fetch_full_content",
        lambda url, **kw: content_fetch.FullContent(
            text=None, status=content_fetch.STATUS_FAILED, length=0,
            truncated=False),
    )
    r = client.post("/api/loophole/records/backfill-content",
                    json={"limit": 10, "delay_ms": 0})
    assert r.status_code == 200
    d = r.json()
    assert d["failed"] == 1
    assert d["updated"] == 0
    assert d["remaining"] == 1  # fetch_failed остаётся в очереди на повтор
    row = repo.get_record(rid, session=app_session)
    assert row["raw_text"] == "важный сниппет"  # не затёрт (COALESCE)
    assert row["content_status"] == "fetch_failed"


def test_export_csv_contains_content_columns(client, app_session):
    rec = LoopholeRecord(sha256=sha256_text("csv1"), title="t", url="https://x.ru",
                         snippet="s", raw_text="ПОЛНЫЙ ТЕКСТ В CSV",
                         content_status="full", raw_text_len=17,
                         status="published", is_loophole=True)
    rid = repo.insert_record(rec, session=app_session)
    r = client.post("/api/loophole/export",
                    json={"records": [rid], "format": "csv"})
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    header = body.splitlines()[0]
    assert "content_status" in header
    assert "raw_text_len" in header
    assert "raw_text" in header
    assert "ПОЛНЫЙ ТЕКСТ В CSV" in body


def test_export_csv_filtered_contains_content(client, app_session):
    rec = LoopholeRecord(sha256=sha256_text("csv2"), title="t2",
                         url="https://x.ru/2", snippet="s2",
                         raw_text="КОНТЕНТ ФИЛЬТРОВАННОГО CSV",
                         content_status="full", raw_text_len=25,
                         status="published", is_loophole=True)
    repo.insert_record(rec, session=app_session)
    r = client.post("/api/loophole/export/csv", json={})
    assert r.status_code == 200
    assert "КОНТЕНТ ФИЛЬТРОВАННОГО CSV" in r.content.decode("utf-8-sig")
