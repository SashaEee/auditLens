"""Тест runner: subprocess, run records, трёхключевой дедуп, лог-шина."""
from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from bank_audit.loophole.parsers import runner
from bank_audit.loophole.parsers.runner import (
    ParserRunner, _parse_parser_output, subscribe, unsubscribe, log_tail,
    finish_stream,
)
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.parsers import dedup as dedup_mod
from bank_audit.hashing import sha256_text


@pytest.fixture
def parser_id(session) -> int:
    wid = repo.create_workspace("u", "ws", session=session)
    return repo.save_parser(wid, "test-parser", "/tmp/parser_test.py",
                            config={"query": "q"}, session=session)


class _FakeStream:
    def __init__(self, data: bytes):
        self._lines = data.splitlines(keepends=True)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"",
                 returncode: int = 0, pid: int = 12345):
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._rc = returncode

    async def wait(self):
        self.returncode = self._rc
        return self._rc

    def terminate(self):
        self.returncode = self._rc

    def kill(self):
        self.returncode = self._rc


@pytest.fixture
def clean_registries():
    runner._RUNNING.clear()
    runner._LOG_BUS.clear()
    runner._LOG_TAIL.clear()
    runner._FINISHED.clear()
    yield
    runner._RUNNING.clear()
    runner._LOG_BUS.clear()
    runner._LOG_TAIL.clear()
    runner._FINISHED.clear()


def _patch_proc(monkeypatch, fake):
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=fake))


# ── парсинг вывода (без изменений поведения) ─────────────────────────────────
def test_parse_valid_json_list():
    res = _parse_parser_output(json.dumps([{"title": "a"}, {"title": "b"}]))
    assert len(res) == 2


def test_parse_garbage_returns_empty():
    assert _parse_parser_output("") == []
    assert _parse_parser_output("not json") == []


def test_parse_json_log_extracts_msg():
    line = '{"ts": "2026-07-27T10:00:00", "level": "INFO", "msg": "crawling"}'
    assert runner._format_log_line(line) == "crawling"


def test_format_log_line_returns_raw_on_bad_json():
    assert runner._format_log_line("plain text") == "plain text"


# ── run records + статусы ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_wait_success_creates_run_record(
    monkeypatch, session, parser_id, clean_registries,
):
    results = [{"title": "t1", "url": "https://a.ru/1", "snippet": "s",
                "text": "полный текст страницы"}]
    _patch_proc(monkeypatch, _FakeProc(stdout=json.dumps(results).encode()))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    saved = await r.wait(timeout=5)
    assert saved == 1

    parser = repo.get_parser(parser_id, session=session)
    assert parser["status"] == "success"
    run = repo.last_run(parser_id, session=session)
    assert run["status"] == "success"
    assert run["items_found"] == 1
    assert run["items_new"] == 1
    assert run["items_dup"] == 0
    rec = session.execute(
        text("SELECT parser_id, text_sha256 FROM loophole_record")
    ).mappings().one()
    assert rec["parser_id"] == parser_id
    assert rec["text_sha256"] == dedup_mod.page_text_sha256("полный текст страницы")


@pytest.mark.asyncio
async def test_wait_empty_when_zero_results(
    monkeypatch, session, parser_id, clean_registries,
):
    _patch_proc(monkeypatch, _FakeProc(stdout=b"[]"))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    assert await r.wait(timeout=5) == 0
    assert repo.last_run(parser_id, session=session)["status"] == "empty"
    assert repo.get_parser(parser_id, session=session)["status"] == "empty"


@pytest.mark.asyncio
async def test_wait_error_on_nonzero_rc(
    monkeypatch, session, parser_id, clean_registries,
):
    _patch_proc(monkeypatch, _FakeProc(stdout=b"", stderr=b"traceback here\n",
                                       returncode=1))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    assert await r.wait(timeout=5) == 0
    run = repo.last_run(parser_id, session=session)
    assert run["status"] == "error"
    assert "traceback here" in run["error_text"]


# ── трёхключевой дедуп ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dedup_by_raw_sha256(monkeypatch, session, parser_id, clean_registries):
    # Точный дубль результата (тот же JSON → тот же sha256) уже в БД.
    results = [{"title": "t", "url": "https://a.ru/same"}]
    raw_text = json.dumps(results[0], ensure_ascii=False, default=str)
    repo.insert_record(
        LoopholeRecord(sha256=sha256_text(raw_text), url="https://a.ru/same",
                       raw_text=raw_text),
        session=session,
    )
    _patch_proc(monkeypatch, _FakeProc(stdout=json.dumps(results).encode()))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    assert await r.wait(timeout=5) == 0
    run = repo.last_run(parser_id, session=session)
    assert run["items_dup"] == 1
    assert run["items_new"] == 0


@pytest.mark.asyncio
async def test_dedup_by_url(monkeypatch, session, parser_id, clean_registries):
    # Запись с таким URL уже есть (другой sha и текст).
    repo.insert_record(
        LoopholeRecord(sha256="other", url="https://a.ru/dup", raw_text="x"),
        session=session,
    )
    results = [{"title": "новый заголовок", "url": "https://a.ru/dup",
                "text": "уникальный текст"}]
    _patch_proc(monkeypatch, _FakeProc(stdout=json.dumps(results).encode()))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    assert await r.wait(timeout=5) == 0
    run = repo.last_run(parser_id, session=session)
    assert run["items_dup"] == 1
    assert run["items_new"] == 0


@pytest.mark.asyncio
async def test_dedup_by_page_text(monkeypatch, session, parser_id, clean_registries):
    tsha = dedup_mod.page_text_sha256("одинаковый текст страницы")
    repo.insert_record(
        LoopholeRecord(sha256="s1", url="https://a.ru/old", raw_text="x",
                       text_sha256=tsha),
        session=session,
    )
    results = [{"title": "t", "url": "https://a.ru/new-url",
                "text": "одинаковый   текст\nстраницы"}]
    _patch_proc(monkeypatch, _FakeProc(stdout=json.dumps(results).encode()))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    assert await r.wait(timeout=5) == 0
    assert repo.last_run(parser_id, session=session)["items_dup"] == 1


# ── лог-шина SSE ─────────────────────────────────────────────────────────────
class _ExplodingStream:
    async def readline(self) -> bytes:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_wait_pump_exception_finalizes_error(
    monkeypatch, session, parser_id, clean_registries,
):
    fake = _FakeProc()
    fake.stdout = _ExplodingStream()
    _patch_proc(monkeypatch, fake)
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    assert await r.wait(timeout=5) == 0
    run = repo.last_run(parser_id, session=session)
    assert run["status"] == "error"
    assert "RuntimeError" in (run["error_text"] or "")


@pytest.mark.asyncio
async def test_log_stream_emits_lines_and_done(
    monkeypatch, session, parser_id, clean_registries,
):
    results = [{"title": "t", "url": "https://a.ru/9"}]
    _patch_proc(monkeypatch, _FakeProc(stdout=json.dumps(results).encode()))
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    q = subscribe(r.run_id)
    await r.wait(timeout=5)
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert any(e["event"] == "log" for e in events)
    done = [e for e in events if e["event"] == "done"]
    assert done and json.loads(done[-1]["data"])["status"] == "success"
    assert log_tail(r.run_id)


def test_subscribe_after_finish_gets_done(clean_registries):
    finish_stream(77, {"status": "empty", "items_new": 0})
    q = subscribe(77)
    msg = q.get_nowait()
    assert msg["event"] == "done"
    unsubscribe(77, q)  # не падает


# ── venv python path ─────────────────────────────────────────────────────────
def test_venv_python_path(tmp_path):
    from bank_audit.loophole.parsers import env
    runner.env = env
    parser_dir = tmp_path / "p1"
    python = runner._venv_python(parser_dir / "parser.py")
    if sys.platform == "win32":
        assert python.name == "python.exe"
    else:
        assert python.name == "python"


@pytest.mark.asyncio
async def test_start_uses_venv_python(
    monkeypatch, session, parser_id, clean_registries, tmp_path,
):
    from bank_audit.loophole.parsers import env
    parser_dir = tmp_path / "p1"
    venv_py = env.venv_python(parser_dir)
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    code_path = str(parser_dir / "parser.py")
    repo.update_parser_code_path(parser_id, code_path, session=session)
    called_with = {}
    async def fake_exec(*cmd, **kwargs):
        called_with["cmd"] = cmd
        return _FakeProc(stdout=b"[]")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    r = ParserRunner(parser_id, code_path, workspace_id=1, session=session)
    await r.start()
    assert called_with["cmd"][0] == str(venv_py)
    assert called_with["cmd"][1] == code_path


@pytest.mark.asyncio
async def test_wait_reads_results_json(
    monkeypatch, session, parser_id, clean_registries, tmp_path,
):
    parser_dir = tmp_path / "p1"
    parser_dir.mkdir()
    code_path = str(parser_dir / "parser.py")
    repo.update_parser_code_path(parser_id, code_path, session=session)
    results = [{"title": "t1", "url": "https://a.ru/1", "snippet": "s"}]
    (parser_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_FakeProc(stdout=b"[]")))
    r = ParserRunner(parser_id, code_path, workspace_id=1, session=session)
    await r.start()
    assert await r.wait(timeout=5) == 1
    run = repo.last_run(parser_id, session=session)
    assert run["status"] == "success"
    assert run["items_found"] == 1


@pytest.mark.asyncio
async def test_wait_finalize_false_does_not_finish_run(
    monkeypatch, session, parser_id, clean_registries, tmp_path,
):
    parser_dir = tmp_path / "p1"
    parser_dir.mkdir()
    code_path = str(parser_dir / "parser.py")
    repo.update_parser_code_path(parser_id, code_path, session=session)
    (parser_dir / "results.json").write_text(json.dumps([{"url": "https://a.ru/1"}]))
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=_FakeProc(stdout=b"[]")))
    run_id = repo.create_run(parser_id, "manual", session=session)
    r = ParserRunner(parser_id, code_path, workspace_id=1, run_id=run_id, session=session)
    await r.start()
    assert await r.wait(timeout=5, finalize=False) == 1
    # run record должен остаться в running (finalize не вызывался).
    assert repo.get_run(run_id, session=session)["status"] == "running"


# ── stop ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stop_finishes_run(
    monkeypatch, session, parser_id, clean_registries,
):
    fake = _FakeProc()
    _patch_proc(monkeypatch, fake)
    r = ParserRunner(parser_id, "/tmp/x.py", workspace_id=1, session=session)
    await r.start()
    await r.stop()
    assert parser_id not in runner._RUNNING
    run = repo.last_run(parser_id, session=session)
    assert run["status"] == "error"
    assert run["error_text"] == "stopped by user"
    # Повторный wait не перезаписывает финализацию.
    assert await r.wait(timeout=5) == 0
    assert repo.last_run(parser_id, session=session)["error_text"] == "stopped by user"


# ── фоновый run() ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_function_background(
    monkeypatch, session, parser_id, clean_registries,
):
    results = [{"title": "t", "url": "https://a.ru/bg"}]
    _patch_proc(monkeypatch, _FakeProc(stdout=json.dumps(results).encode()))
    run_id = await runner.run(parser_id, "manual", session=session)
    assert run_id > 0
    # Фоновая wait-задача завершается сама.
    for _ in range(50):
        await asyncio.sleep(0.05)
        row = repo.get_run(run_id, session=session)
        if row["status"] != "running":
            break
    assert repo.get_run(run_id, session=session)["status"] == "success"
    # Повторный запуск во время/после — конфликт только если running.
    assert repo.get_run(run_id, session=session)["items_new"] == 1
