import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bank_audit.loophole.chat.tools_nanobot import _is_read_only_select, db_query

SCHEMA_SQL = """
CREATE TABLE loophole_record (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title     TEXT,
    bank_slug TEXT
);
"""


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.connection.executescript(SCHEMA_SQL)
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    yield s
    s.close()


def test_is_read_only_select_accepts_simple_select():
    assert _is_read_only_select("SELECT * FROM loophole_record LIMIT 5") is True


def test_is_read_only_select_rejects_insert():
    assert _is_read_only_select("INSERT INTO loophole_record (title) VALUES ('x')") is False


def test_is_read_only_select_rejects_semicolon_injection():
    assert _is_read_only_select("SELECT 1; DROP TABLE loophole_record") is False


def test_is_read_only_select_rejects_comment_dash():
    assert _is_read_only_select("SELECT 1 -- DROP TABLE loophole_record") is False


def test_is_read_only_select_rejects_union_injection():
    assert _is_read_only_select("SELECT title FROM loophole_record UNION DROP TABLE x") is False


def test_db_query_rejects_non_select():
    result = db_query(sql="DROP TABLE loophole_record", session=None)
    assert result["error"] == "only SELECT queries are allowed"


def test_db_query_enforces_limit(session):
    result = db_query(
        sql="SELECT record_id, title FROM loophole_record LIMIT 1",
        session=session,
    )
    assert "error" not in result
    assert result["columns"] == ["record_id", "title"]
    assert result["rows"] == []
    assert result["row_count"] == 0
