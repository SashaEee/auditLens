"""Тест ручной маркировки: POST /api/loophole/records/verdict.

SQLite in-memory (паттерн test_web.py). Эмбеддинг НЕ вызывается: embed_one
мокается на сбой → graceful fallback (пример сохраняется без embedding, что
совместимо с SQLite — нет ::vector каста). Покрывает: одиночную и массовую
маркировку, KB-дедуп/откат, 400 на пустой список, skipped, дефолтный reason.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit.hashing import sha256_text
from bank_audit.loophole import repository as repo
from bank_audit.loophole.kb import repository as kb_repo
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


@pytest.fixture
def no_embedding():
    """Принудительный сбой эмбеддинга → graceful fallback без embedding."""
    with patch.object(
        kb_repo.embedder, "embed_one", side_effect=RuntimeError("no emb")
    ) as m:
        yield m


def _insert(session, sha: str, **kw) -> int:
    return repo.insert_record(LoopholeRecord(sha256=sha256_text(sha), **kw), session=session)


# ── Одиночная маркировка ────────────────────────────────────────────────────
def test_mark_single_loophole(client, app_session, no_embedding):
    rid = _insert(app_session, "m1", title="лазейка", snippet="скрытая комиссия")
    r = client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True, "comment": "проверено вручную",
    })
    assert r.status_code == 200
    assert r.json() == {"updated": 1, "skipped": []}
    rec = repo.get_record(rid, session=app_session)
    assert rec["is_loophole"]  # SQLite: 1
    assert rec["verdict_model"] == "manual"
    assert rec["verdict_confidence"] == 1.0
    assert rec["verdict_reason"] == "проверено вручную"
    assert rec["status"] == "classified"
    assert rec["classified_at"] is not None


def test_mark_empty_comment_default_reason(client, app_session, no_embedding):
    rid = _insert(app_session, "m2", title="запись")
    r = client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True,
    })
    assert r.status_code == 200
    rec = repo.get_record(rid, session=app_session)
    assert rec["verdict_reason"] == "manual:test-user"


# ── Массовая маркировка ─────────────────────────────────────────────────────
def test_mark_bulk(client, app_session, no_embedding):
    rid1 = _insert(app_session, "b1", title="запись 1")
    rid2 = _insert(app_session, "b2", title="запись 2")
    rid3 = _insert(app_session, "b3", title="запись 3")
    r = client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid1, rid2, rid3], "is_loophole": False, "comment": "не лазейки",
    })
    assert r.status_code == 200
    assert r.json() == {"updated": 3, "skipped": []}
    for rid in (rid1, rid2, rid3):
        rec = repo.get_record(rid, session=app_session)
        assert not rec["is_loophole"]
        assert rec["verdict_model"] == "manual"


# ── KB-синхронизация ────────────────────────────────────────────────────────
def test_mark_true_creates_kb_example(client, app_session, no_embedding):
    rid = _insert(app_session, "k1", title="Скрытая комиссия", snippet="Банк не раскрывает ПСК")
    client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True,
    })
    ex = repo.get_kb_example_by_record(rid, session=app_session)
    assert ex is not None
    assert ex["title"] == "Скрытая комиссия"
    assert ex["description"] == "Банк не раскрывает ПСК"
    assert ex["category"] == "manual"


def test_mark_true_twice_no_duplicate(client, app_session, no_embedding):
    rid = _insert(app_session, "k2", title="Лазейка", snippet="описание")
    for _ in range(2):
        client.post("/api/loophole/records/verdict", json={
            "record_ids": [rid], "is_loophole": True,
        })
    assert kb_repo.count_examples(session=app_session) == 1


def test_mark_false_deletes_kb_example(client, app_session, no_embedding):
    rid = _insert(app_session, "k3", title="Лазейка", snippet="описание")
    client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True,
    })
    assert repo.get_kb_example_by_record(rid, session=app_session) is not None
    r = client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": False,
    })
    assert r.status_code == 200
    assert repo.get_kb_example_by_record(rid, session=app_session) is None


def test_kb_description_fallback_raw_text(client, app_session, no_embedding):
    """Без snippet — description из raw_text (≤2000 символов)."""
    rid = _insert(app_session, "k4", title="T", raw_text="x" * 3000)
    client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True,
    })
    ex = repo.get_kb_example_by_record(rid, session=app_session)
    assert ex["description"] == "x" * 2000


def test_kb_description_fallback_title(client, app_session, no_embedding):
    """Без snippet и raw_text — description из title."""
    rid = _insert(app_session, "k5", title="Только заголовок")
    client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True,
    })
    ex = repo.get_kb_example_by_record(rid, session=app_session)
    assert ex["description"] == "Только заголовок"


# ── Ошибки и граничные случаи ───────────────────────────────────────────────
def test_empty_record_ids_400(client):
    r = client.post("/api/loophole/records/verdict", json={
        "record_ids": [], "is_loophole": True,
    })
    assert r.status_code == 400


def test_nonexistent_id_skipped(client, app_session, no_embedding):
    rid = _insert(app_session, "s1", title="запись")
    r = client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid, 999999], "is_loophole": True,
    })
    assert r.status_code == 200
    assert r.json() == {"updated": 1, "skipped": [999999]}


def test_mark_logs_action(client, app_session, no_embedding):
    rid = _insert(app_session, "l1", title="запись")
    client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True, "comment": "c",
    })
    actions = repo.list_actions("test-user", session=app_session)
    marks = [a for a in actions if a["action"] == "mark_verdict"]
    assert marks, "действие mark_verdict не залогировано"


def test_record_without_llm_verdict_markable(client, app_session, no_embedding):
    """Запись с is_loophole = NULL маркируется так же."""
    rid = _insert(app_session, "n1", title="без вердикта")
    assert repo.get_record(rid, session=app_session)["is_loophole"] is None
    r = client.post("/api/loophole/records/verdict", json={
        "record_ids": [rid], "is_loophole": True,
    })
    assert r.status_code == 200
    assert repo.get_record(rid, session=app_session)["is_loophole"]
