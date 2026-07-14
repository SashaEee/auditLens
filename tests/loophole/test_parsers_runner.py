"""Тест runner: мок subprocess, проверяем start/stop/status/wait и парсинг вывода."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from bank_audit.loophole.parsers import runner
from bank_audit.loophole.parsers.runner import ParserRunner, _parse_parser_output
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.hashing import sha256_text

from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


PARSER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS loophole_parser ("
    "parser_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "workspace_id INTEGER, name TEXT, code_path TEXT, "
    "status TEXT DEFAULT 'created', config TEXT, "
    "created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_run_at TEXT)"
)
RECORD_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS loophole_record ("
    "record_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "sha256 TEXT NOT NULL, title TEXT, url TEXT, snippet TEXT, "
    "domain TEXT, trust_score REAL, fetched_at TEXT, "
    "collected_at TEXT DEFAULT CURRENT_TIMESTAMP, bank_slug TEXT, "
    "keyword TEXT, raw_text TEXT, is_loophole INTEGER, "
    "verdict_confidence REAL, verdict_reason TEXT, verdict_model TEXT, "
    "classified_at TEXT, status TEXT DEFAULT 'new')"
)


@pytest.fixture
def session(sqlite_session):
    sqlite_session.execute(text(PARSER_SCHEMA))
    sqlite_session.execute(text(RECORD_SCHEMA))
    sqlite_session.commit()
    return sqlite_session


@pytest.fixture
def parser_id(session) -> int:
    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "test-parser", "/tmp/parser_test.py",
                           config={"query": "q"}, session=session)
    return pid


class _FakeProc:
    """Эмуляция asyncio.subprocess.Process для тестов."""
    def __init__(self, stdout: bytes = b"", returncode: int = 0, pid: int = 12345):
        self.pid = pid
        self.returncode: int | None = None
        self._stdout = stdout
        self._rc = returncode
        self._killed = False

    async def communicate(self):
        self.returncode = self._rc
        return self._stdout, b""

    def terminate(self):
        self.returncode = self._rc

    def kill(self):
        self._killed = True
        self.returncode = self._rc

    async def wait(self):
        self.returncode = self._rc
        return self._rc


@pytest.fixture
def clean_registry():
    runner._RUNNING.clear()
    yield
    runner._RUNNING.clear()


# ── _parse_parser_output ─────────────────────────────────────────────────────
def test_parse_valid_json_list():
    out = json.dumps([{"title": "a", "url": "u1"}, {"title": "b", "url": "u2"}])
    res = _parse_parser_output(out)
    assert len(res) == 2
    assert res[0]["title"] == "a"


def test_parse_single_object():
    res = _parse_parser_output(json.dumps({"title": "x", "url": "y"}))
    assert len(res) == 1
    assert res[0]["title"] == "x"


def test_parse_results_wrapper():
    res = _parse_parser_output(json.dumps({"results": [{"title": "z"}]}))
    assert len(res) == 1
    assert res[0]["title"] == "z"


def test_parse_garbage_returns_empty():
    assert _parse_parser_output("") == []
    assert _parse_parser_output("not json at all") == []
    assert _parse_parser_output("<<<>>>") == []


def test_parse_fenced_json():
    raw = "```json\n" + json.dumps([{"title": "f"}]) + "\n```"
    res = _parse_parser_output(raw)
    assert len(res) == 1
    assert res[0]["title"] == "f"


# ── start/stop/status/wait ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_updates_status_running(
    monkeypatch, session, parser_id, clean_registry,
):
    fake = _FakeProc()
    async def _fake_exec(*args, **kwargs):
        return fake
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    pid = await r.start()
    assert pid == 12345
    assert parser_id in runner._RUNNING
    row = repo.get_parser(parser_id, session=session)
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_status_reflects_process(
    monkeypatch, session, parser_id, clean_registry,
):
    fake = _FakeProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=fake))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    st = await r.status()
    assert st["pid"] == 12345
    assert st["running"] is True
    assert st["returncode"] is None
    assert st["status_db"] == "running"


@pytest.mark.asyncio
async def test_stop_terminates_and_updates(
    monkeypatch, session, parser_id, clean_registry,
):
    fake = _FakeProc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=fake))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    await r.stop()
    assert parser_id not in runner._RUNNING
    row = repo.get_parser(parser_id, session=session)
    assert row["status"] == "stopped"


@pytest.mark.asyncio
async def test_wait_saves_results(
    monkeypatch, session, parser_id, clean_registry,
):
    results = [
        {"title": "лазейка 1", "url": "https://a.ru/1", "snippet": "скрытая комиссия",
         "domain": "a.ru", "bank_slug": "sberbank", "keyword": "комиссия"},
        {"title": "лазейка 2", "url": "https://b.ru/2", "snippet": "навязанная страховка",
         "domain": "b.ru", "bank_slug": "vtb", "keyword": "страховка"},
    ]
    fake = _FakeProc(stdout=json.dumps(results).encode("utf-8"), returncode=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=fake))

    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    saved = await r.wait(timeout=5)
    assert saved == 2

    row = repo.get_parser(parser_id, session=session)
    assert row["status"] == "completed"
    rows = session.execute(text("SELECT count(*) FROM loophole_record")).scalar()
    assert rows == 2


@pytest.mark.asyncio
async def test_wait_dedup_by_sha256(
    monkeypatch, session, parser_id, clean_registry,
):
    results = [{"title": "dup", "url": "https://a.ru/1"}]
    # Предзаполним запись с тем же sha.
    raw_text = json.dumps(results[0], ensure_ascii=False)
    sha = sha256_text(raw_text)
    repo.insert_record(
        LoopholeRecord(sha256=sha, title="dup", url="https://a.ru/1", raw_text=raw_text),
        session=session,
    )

    fake = _FakeProc(stdout=json.dumps(results).encode("utf-8"), returncode=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=fake))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    saved = await r.wait(timeout=5)
    assert saved == 0  # дедуп
    rows = session.execute(text("SELECT count(*) FROM loophole_record")).scalar()
    assert rows == 1


@pytest.mark.asyncio
async def test_wait_nonzero_returncode_sets_error(
    monkeypatch, session, parser_id, clean_registry,
):
    fake = _FakeProc(stdout=b"[]", returncode=1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=fake))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    saved = await r.wait(timeout=5)
    assert saved == 0
    row = repo.get_parser(parser_id, session=session)
    assert row["status"] == "error"
