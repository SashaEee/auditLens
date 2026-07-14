"""Общие фикстуры тестов модуля loophole.

Без сети и реальной БД: используем in-memory SQLite для SQL-тестов, где это
безопасно (таблицы без Greenplum-специфики), и моки для LLM/web_search/fetch.
Для тестов, требующих Postgres-специфики (BIGSERIAL/JSONB/TEXT[]), проверяем
структуру миграции без выполнения.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Гарантируем, что src/ в sys.path даже без установленного пакета.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Дефолты env, чтобы импорт config не падал в тестах.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")


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
def fake_user_id() -> str:
    return "test-user"


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
