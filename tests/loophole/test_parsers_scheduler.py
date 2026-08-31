"""Тест scheduler: cron-валидация, расчёт next_run, тик due-парсеров."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bank_audit.clock import MSK
from bank_audit.loophole import repository as repo
from bank_audit.loophole.parsers import runner as runner_mod
from bank_audit.loophole.parsers import scheduler


# ── next_run ─────────────────────────────────────────────────────────────────
def test_next_run_valid():
    base = datetime(2026, 7, 26, 12, 0, tzinfo=MSK)
    nxt = scheduler.next_run("*/15 * * * *", base)
    assert nxt == datetime(2026, 7, 26, 12, 15, tzinfo=MSK)


def test_next_run_daily():
    base = datetime(2026, 7, 26, 12, 0, tzinfo=MSK)
    nxt = scheduler.next_run("0 5 * * *", base)
    assert nxt.day == 27 and nxt.hour == 5


def test_next_run_invalid_raises():
    with pytest.raises(ValueError, match="invalid cron"):
        scheduler.next_run("not-a-cron")
    with pytest.raises(ValueError):
        scheduler.next_run("61 * * * *")


def test_parse_dt_formats():
    assert scheduler._parse_dt(None) is None
    aware = scheduler._parse_dt("2026-07-26T10:00:00+03:00")
    assert aware.tzinfo is not None
    naive = scheduler._parse_dt("2026-07-26 10:00:00")
    assert naive.tzinfo is not None
    assert scheduler._parse_dt("garbage") is None


# ── tick ─────────────────────────────────────────────────────────────────────
@pytest.fixture
def db_session_cm(session, monkeypatch):
    """Подменяет db.session в scheduler/healer на тестовую SQLite-сессию."""
    @contextmanager
    def _cm():
        yield session
    monkeypatch.setattr(scheduler.db, "session", _cm)
    return session


@pytest.mark.asyncio
async def test_tick_runs_due_parser(session, db_session_cm, monkeypatch):
    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "p", "/tmp/p.py", session=session)
    repo.update_parser_status(pid, "ready", session=session)
    past = datetime.now(MSK) - timedelta(minutes=5)
    repo.update_parser_schedule(
        pid, cron_expr="*/5 * * * *", auto_enabled=True,
        next_run_at=past, last_edited_by="u", session=session,
    )
    run_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(scheduler.runner_mod, "run", run_mock)
    monkeypatch.setattr(scheduler.healer_mod, "heal_tick", AsyncMock(return_value=[]))

    started = await scheduler.tick()
    assert started == [pid]
    run_mock.assert_awaited_once_with(pid, "cron")
    # next_run_at пересчитан в будущее.
    row = repo.get_parser(pid, session=session)
    assert scheduler._parse_dt(row["next_run_at"]) > datetime.now(MSK)


@pytest.mark.asyncio
async def test_tick_skips_future_and_disabled(session, db_session_cm, monkeypatch):
    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "p", "/tmp/p.py", session=session)
    future = datetime.now(MSK) + timedelta(hours=3)
    repo.update_parser_schedule(
        pid, cron_expr="0 5 * * *", auto_enabled=True,
        next_run_at=future, last_edited_by="u", session=session,
    )
    run_mock = AsyncMock()
    monkeypatch.setattr(scheduler.runner_mod, "run", run_mock)
    monkeypatch.setattr(scheduler.healer_mod, "heal_tick", AsyncMock(return_value=[]))

    assert await scheduler.tick() == []
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_skips_already_running(session, db_session_cm, monkeypatch):
    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "p", "/tmp/p.py", session=session)
    past = datetime.now(MSK) - timedelta(minutes=1)
    repo.update_parser_schedule(
        pid, cron_expr="* * * * *", auto_enabled=True,
        next_run_at=past, last_edited_by="u", session=session,
    )
    runner_mod._RUNNING[pid] = object()  # помечаем как запущенный
    try:
        run_mock = AsyncMock()
        monkeypatch.setattr(scheduler.runner_mod, "run", run_mock)
        monkeypatch.setattr(scheduler.healer_mod, "heal_tick", AsyncMock(return_value=[]))
        assert await scheduler.tick() == []
        run_mock.assert_not_awaited()
    finally:
        runner_mod._RUNNING.clear()


@pytest.mark.asyncio
async def test_tick_run_failure_advances_next_run(session, db_session_cm, monkeypatch):
    """Падение run() не откатывает пересчёт next_run_at (иначе ретрай каждый тик)."""
    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "p", "/tmp/p.py", session=session)
    repo.update_parser_status(pid, "ready", session=session)
    past = datetime.now(MSK) - timedelta(minutes=5)
    repo.update_parser_schedule(
        pid, cron_expr="0 5 * * *", auto_enabled=True,
        next_run_at=past, last_edited_by="u", session=session,
    )
    monkeypatch.setattr(scheduler.runner_mod, "run",
                        AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(scheduler.healer_mod, "heal_tick", AsyncMock(return_value=[]))

    assert await scheduler.tick() == []
    row = repo.get_parser(pid, session=session)
    nxt = scheduler._parse_dt(row["next_run_at"])
    assert nxt is not None and nxt > datetime.now(MSK)


@pytest.mark.asyncio
async def test_tick_heal_phase_called(session, db_session_cm, monkeypatch):
    heal_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scheduler.healer_mod, "heal_tick", heal_mock)
    await scheduler.tick()
    heal_mock.assert_awaited_once()
