"""Тест repository: CRUD + дедуп по sha256.

На in-memory SQLite с адаптированной схемой (BIGSERIAL→INTEGER AUTOINCREMENT,
JSONB→TEXT, TEXT[]→TEXT, TIMESTAMPTZ→TEXT). Это проверяет SQL-логику repository
без реальной Greenplum-БД. ILIKE эмулируем через LIKE (SQLite регистрочувствителен —
тестовые данные в нижнем регистре).
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.hashing import sha256_text


# SQLite не знает ILIKE — регистрируем функцию-заглушку (lower-case сравнение).
def _ilike_pattern(pattern: str) -> str:
    return pattern.lower().replace("%", "%")


SCHEMA_SQL = """
CREATE TABLE loophole_keyword (
    keyword_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword       TEXT NOT NULL,
    category      TEXT,
    source        TEXT,
    weight        REAL DEFAULT 1.0,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    is_active     INTEGER DEFAULT 1
);
CREATE INDEX idx_lk_keyword ON loophole_keyword(keyword);

CREATE TABLE loophole_record (
    record_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256        TEXT NOT NULL,
    title         TEXT,
    url           TEXT,
    snippet       TEXT,
    domain        TEXT,
    trust_score   REAL,
    fetched_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    collected_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    bank_slug     TEXT,
    keyword       TEXT,
    raw_text      TEXT,
    is_loophole   INTEGER,
    verdict_confidence REAL,
    verdict_reason TEXT,
    verdict_model TEXT,
    classified_at TEXT,
    status        TEXT DEFAULT 'new'
);
CREATE INDEX idx_lr_sha ON loophole_record(sha256);
CREATE INDEX idx_lr_bank ON loophole_record(bank_slug);

CREATE TABLE loophole_workspace (
    workspace_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        TEXT NOT NULL,
    name           TEXT,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    last_active_at TEXT
);
CREATE INDEX idx_lw_user ON loophole_workspace(user_id);

CREATE TABLE loophole_result (
    result_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER,
    query_text    TEXT,
    period_from   TEXT,
    period_to     TEXT,
    bank_slugs    TEXT,
    records       TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT
);

CREATE TABLE loophole_chat_message (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER,
    role          TEXT,
    content       TEXT,
    tool_name     TEXT,
    tool_args     TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_lcm_ws ON loophole_chat_message(workspace_id, created_at);

CREATE TABLE loophole_action_log (
    log_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT,
    workspace_id  INTEGER,
    action        TEXT,
    detail        TEXT,
    ip            TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_lal_user ON loophole_action_log(user_id, created_at);
"""


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        raw = conn.connection
        raw.executescript(SCHEMA_SQL)
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    yield s
    s.close()


def _make_record(**kw) -> LoopholeRecord:
    base = dict(
        sha256=sha256_text("test"),
        title="лазейка в кредитном договоре",
        url="https://example.ru/doc",
        snippet="скрытая комиссия",
        domain="example.ru",
        trust_score=0.8,
        bank_slug="sberbank",
        keyword="лазейка",
        raw_text="текст договора со скрытой комиссией",
    )
    base.update(kw)
    return LoopholeRecord(**base)


def test_insert_record_returns_id(session):
    rec = _make_record()
    rid = repo.insert_record(rec, session=session)
    assert rid is not None and rid > 0


def test_insert_record_dedup_by_sha256(session):
    rec = _make_record()
    rid1 = repo.insert_record(rec, session=session)
    rid2 = repo.insert_record(rec, session=session)
    assert rid1 == rid2, "дедуп по sha256 не сработал"
    rows = session.execute(text("SELECT count(*) FROM loophole_record")).scalar()
    assert rows == 1


def test_exists_sha256(session):
    rec = _make_record()
    assert not repo.exists_sha256(rec.sha256, session=session)
    repo.insert_record(rec, session=session)
    assert repo.exists_sha256(rec.sha256, session=session)


def test_update_verdict(session):
    rid = repo.insert_record(_make_record(), session=session)
    repo.update_verdict(
        rid, is_loophole=True, confidence=0.92,
        reason="скрытая комиссия", model="test-model", session=session,
    )
    row = repo.get_record(rid, session=session)
    assert row["is_loophole"] == 1  # SQLite хранит bool как int
    assert row["verdict_confidence"] == 0.92
    assert row["status"] == "classified"


def test_search_relevant_by_query(session):
    repo.insert_record(_make_record(title="лазейка в договоре"), session=session)
    repo.insert_record(
        _make_record(sha256=sha256_text("other"), title="безопасный продукт"),
        session=session,
    )
    # Обе записи ещё не классифицированы → only_loophole=True даст 0.
    results = repo.search_relevant("лазейка", only_loophole=False, session=session)
    assert len(results) == 1
    assert "лазейка" in results[0]["title"]


def test_search_relevant_bank_filter(session):
    repo.insert_record(_make_record(bank_slug="sberbank"), session=session)
    repo.insert_record(
        _make_record(sha256=sha256_text("vtb"), bank_slug="vtb"),
        session=session,
    )
    results = repo.search_relevant(
        "", bank_slugs=["sberbank"], only_loophole=False, session=session
    )
    assert len(results) == 1
    assert results[0]["bank_slug"] == "sberbank"


def test_keyword_crud_and_dedup(session):
    kid1 = repo.add_keyword("лазейка", category="seed", source="cbr", session=session)
    kid2 = repo.add_keyword("лазейка", category="seed", source="cbr", session=session)
    assert kid1 == kid2, "дедуп ключевого слова не сработал"
    kws = repo.list_keywords(session=session)
    assert len(kws) == 1
    repo.set_keyword_active(kid1, False, session=session)
    active = repo.list_keywords(only_active=True, session=session)
    assert len(active) == 0


def test_workspace_isolation(session):
    wid_a = repo.create_workspace("user-a", "ws-a", session=session)
    wid_b = repo.create_workspace("user-b", "ws-b", session=session)
    ws_a = repo.list_workspaces("user-a", session=session)
    ws_b = repo.list_workspaces("user-b", session=session)
    assert len(ws_a) == 1 and ws_a[0]["workspace_id"] == wid_a
    assert len(ws_b) == 1 and ws_b[0]["workspace_id"] == wid_b


def test_chat_history_order(session):
    wid = repo.create_workspace("u", session=session)
    repo.add_chat_message(wid, "user", "вопрос", session=session)
    repo.add_chat_message(wid, "assistant", "ответ", session=session)
    hist = repo.list_chat_history(wid, session=session)
    assert len(hist) == 2
    assert hist[0]["role"] == "user"
    assert hist[1]["role"] == "assistant"


def test_log_action_and_list(session):
    repo.log_action("u1", "search", detail={"q": "лазейка"}, ip="127.0.0.1", session=session)
    repo.log_action("u1", "export", detail={"fmt": "pdf"}, session=session)
    actions = repo.list_actions("u1", session=session)
    assert len(actions) == 2
    # DESC по created_at — последний первым.
    assert actions[0]["action"] == "export"
    assert actions[0]["user_id"] == "u1"
