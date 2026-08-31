"""Тест healer: детект сбоя, heal_attempts, отключение после 3 неудач, пробный запуск."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bank_audit.clock import MSK
from bank_audit.loophole import repository as repo
from bank_audit.loophole.parsers import healer
from bank_audit.loophole.parsers import runner as runner_mod


@pytest.fixture
def db_cm(session, monkeypatch):
    @contextmanager
    def _cm():
        yield session
    monkeypatch.setattr(healer.db, "session", _cm)
    return session


@pytest.fixture
def parser_id(session) -> int:
    wid = repo.create_workspace("u", "ws", session=session)
    pid = repo.save_parser(wid, "p", "/tmp/p.py", session=session)
    repo.update_parser_status(pid, "ready", session=session)
    repo.update_parser_schedule(
        pid, cron_expr="* * * * *", auto_enabled=True,
        next_run_at=datetime.now(MSK), last_edited_by="u", session=session,
    )
    return pid


@pytest.fixture(autouse=True)
def clean_healing():
    healer._HEALING.clear()
    runner_mod._FINISHED.clear()
    runner_mod._LOG_TAIL.clear()
    yield
    healer._HEALING.clear()


# ── heal_tick: фильтры ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_tick_skips_success_and_resets_attempts(session, db_cm, parser_id, monkeypatch):
    repo.set_heal_attempts(parser_id, 2, session=session)
    rid = repo.create_run(parser_id, "cron", session=session)
    repo.finish_run(rid, "success", items_new=5, session=session)
    monkeypatch.setattr(healer, "nanobot_available", lambda: True)
    heal_spy = AsyncMock()
    monkeypatch.setattr(healer, "heal", heal_spy)

    assert await healer.heal_tick() == []
    heal_spy.assert_not_awaited()
    assert repo.get_parser(parser_id, session=session)["heal_attempts"] == 0


@pytest.mark.asyncio
async def test_tick_skips_disabled_auto(session, db_cm, monkeypatch):
    wid = repo.create_workspace("u", "ws2", session=session)
    pid = repo.save_parser(wid, "p2", "/tmp/p2.py", session=session)
    rid = repo.create_run(pid, "manual", session=session)
    repo.finish_run(rid, "error", error_text="boom", session=session)
    monkeypatch.setattr(healer, "nanobot_available", lambda: True)
    heal_spy = AsyncMock()
    monkeypatch.setattr(healer, "heal", heal_spy)

    assert await healer.heal_tick() == []
    heal_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_disables_after_max_attempts(session, db_cm, parser_id, monkeypatch):
    repo.set_heal_attempts(parser_id, healer.MAX_HEAL_ATTEMPTS, session=session)
    rid = repo.create_run(parser_id, "cron", session=session)
    repo.finish_run(rid, "empty", session=session)
    monkeypatch.setattr(healer, "nanobot_available", lambda: True)
    heal_spy = AsyncMock()
    monkeypatch.setattr(healer, "heal", heal_spy)

    assert await healer.heal_tick() == []
    heal_spy.assert_not_awaited()
    row = repo.get_parser(parser_id, session=session)
    assert row["auto_enabled"] in (False, 0)


@pytest.mark.asyncio
async def test_tick_starts_heal_on_error(session, db_cm, parser_id, monkeypatch):
    rid = repo.create_run(parser_id, "cron", session=session)
    repo.finish_run(rid, "error", error_text="boom", session=session)
    monkeypatch.setattr(healer, "nanobot_available", lambda: True)
    monkeypatch.setattr(healer, "heal", AsyncMock(return_value=11))

    assert await healer.heal_tick() == [parser_id]


# ── _heal_worker ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_worker_source_unavailable_no_patch(session, db_cm, parser_id, monkeypatch):
    monkeypatch.setattr(
        healer, "_run_nanobot_heal",
        AsyncMock(return_value=("источник недоступен: timeout", False)),
    )
    run_id = repo.create_run(parser_id, "heal", session=session)
    await healer._heal_worker(parser_id, run_id, session=session)

    row = repo.get_parser(parser_id, session=session)
    assert row["heal_attempts"] == 1
    run = repo.get_run(run_id, session=session)
    assert run["status"] == "error"
    assert "недоступен" in run["heal_report"]


@pytest.mark.asyncio
async def test_worker_patch_and_trial_success(session, db_cm, parser_id, monkeypatch):
    monkeypatch.setattr(
        healer, "_run_nanobot_heal",
        AsyncMock(return_value=("исправлены селекторы", True)),
    )
    monkeypatch.setattr(
        healer.generator_mod, "install_requirements",
        AsyncMock(return_value=None),
    )
    # Пробный запуск: фейковый runner с успешным завершением.
    fake_trial = AsyncMock()
    fake_trial.start = AsyncMock(return_value=1)
    fake_trial.wait = AsyncMock(return_value=3)
    monkeypatch.setattr(healer.runner_mod, "ParserRunner",
                        lambda *a, **kw: fake_trial)

    async def _fake_finish_trial(*a, **kw):
        # имитируем успешный trial-run в БД
        pass

    repo.update_parser_code_path(parser_id, "/tmp/p.py", session=session)
    run_id = repo.create_run(parser_id, "heal", session=session)

    # get_run для trial должен вернуть success — мокаем repo.get_run выборочно.
    real_get_run = repo.get_run

    def _get_run(rid, *, session=None):
        if rid != run_id:
            return {"status": "success", "run_id": rid}
        return real_get_run(rid, session=session)

    monkeypatch.setattr(healer.repo, "get_run", _get_run)

    await healer._heal_worker(parser_id, run_id, session=session)

    row = repo.get_parser(parser_id, session=session)
    assert row["heal_attempts"] == 0
    run = repo.get_run(run_id, session=session)
    assert run["status"] == "success"
    assert "селекторы" in run["heal_report"]


@pytest.mark.asyncio
async def test_worker_patch_but_trial_fails(session, db_cm, parser_id, monkeypatch):
    monkeypatch.setattr(
        healer, "_run_nanobot_heal",
        AsyncMock(return_value=("патч применён", True)),
    )
    monkeypatch.setattr(
        healer.generator_mod, "install_requirements",
        AsyncMock(return_value=None),
    )
    fake_trial = AsyncMock()
    fake_trial.start = AsyncMock(return_value=1)
    fake_trial.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(healer.runner_mod, "ParserRunner",
                        lambda *a, **kw: fake_trial)

    # get_run для trial должен вернуть error — мокаем выборочно,
    # чтобы финальный ассерт проверял реальную строку БД.
    real_get_run = repo.get_run

    def _get_run(rid, *, session=None):
        if rid != run_id:
            return {"status": "error", "run_id": rid}
        return real_get_run(rid, session=session)

    monkeypatch.setattr(healer.repo, "get_run", _get_run)

    run_id = repo.create_run(parser_id, "heal", session=session)
    await healer._heal_worker(parser_id, run_id, session=session)

    row = repo.get_parser(parser_id, session=session)
    assert row["heal_attempts"] == 1
    assert repo.get_run(run_id, session=session)["status"] == "error"


# ── heal(): guard'ы ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_heal_manual_resets_attempts(session, db_cm, parser_id, monkeypatch):
    repo.set_heal_attempts(parser_id, 2, session=session)
    monkeypatch.setattr(healer, "_heal_worker", AsyncMock())
    run_id = await healer.heal(parser_id, manual=True, session=session)
    assert run_id > 0
    assert repo.get_parser(parser_id, session=session)["heal_attempts"] == 0
    assert repo.get_run(run_id, session=session)["run_trigger"] == "heal"


@pytest.mark.asyncio
async def test_heal_conflict_and_not_found(session, db_cm, parser_id, monkeypatch):
    monkeypatch.setattr(healer, "_heal_worker", AsyncMock())
    healer._HEALING.add(parser_id)
    with pytest.raises(RuntimeError, match="already running"):
        await healer.heal(parser_id, session=session)
    healer._HEALING.discard(parser_id)
    with pytest.raises(ValueError, match="not found"):
        await healer.heal(9999, session=session)


# ── _run_nanobot_heal: выбор предыдущего сбойного run ────────────────────────
@pytest.mark.asyncio
async def test_run_nanobot_heal_uses_previous_failed_run(
    session, db_cm, parser_id, monkeypatch, tmp_path,
):
    """last_run исключает текущий heal-run: nanobot получает ошибку предыдущего сбоя."""
    fail_id = repo.create_run(parser_id, "cron", session=session)
    repo.finish_run(fail_id, "error", error_text="real boom", session=session)
    heal_run_id = repo.create_run(parser_id, "heal", session=session)

    captured = {}

    class _FakeBot:
        async def run(self, prompt, **kw):
            captured["prompt"] = prompt

            class _R:
                content = "диагноз"
            return _R()

        async def aclose(self):
            pass

    # create_nanobot импортируется локально внутри _run_nanobot_heal —
    # патчим атрибут исходного модуля.
    from bank_audit.loophole.chat import nanobot_agent
    monkeypatch.setattr(nanobot_agent, "create_nanobot",
                        lambda **kw: (_FakeBot(), str(tmp_path / "cfg.json")))
    row = repo.get_parser(parser_id, session=session)
    report, patched = await healer._run_nanobot_heal(row, heal_run_id)
    assert "real boom" in captured["prompt"]
    assert report == "диагноз"
    assert patched is False  # /tmp/p.py не существует — before=="", файла нет


# ── _heal_worker: падение воркера инкрементирует попытки ────────────────────
@pytest.mark.asyncio
async def test_worker_exception_increments_attempts(session, db_cm, parser_id, monkeypatch):
    """Падение воркера инкрементирует heal_attempts и отключает auto на лимите."""
    repo.set_heal_attempts(parser_id, healer.MAX_HEAL_ATTEMPTS - 1, session=session)
    monkeypatch.setattr(
        healer, "_run_nanobot_heal",
        AsyncMock(side_effect=RuntimeError("nanobot down")),
    )
    run_id = repo.create_run(parser_id, "heal", session=session)
    await healer._heal_worker(parser_id, run_id, session=session)

    row = repo.get_parser(parser_id, session=session)
    assert row["heal_attempts"] == healer.MAX_HEAL_ATTEMPTS
    assert row["auto_enabled"] in (False, 0)
    assert repo.get_run(run_id, session=session)["status"] == "error"


# ── heal(): переустановка зависимостей после патча ───────────────────────────
@pytest.mark.asyncio
async def test_heal_reinstalls_requirements(session, db_cm, parser_id, monkeypatch):
    """После успешного патча healer вызывает install_requirements для директории парсера."""
    parser_dir = Path(repo.get_parser(parser_id, session=session)["code_path"]).parent
    parser_dir.mkdir(parents=True, exist_ok=True)
    (parser_dir / "requirements.txt").write_text("httpx\n")

    installed = []

    async def fake_install(path):
        installed.append(path)

    monkeypatch.setattr(healer.generator_mod, "install_requirements", fake_install)

    monkeypatch.setattr(
        healer, "_run_nanobot_heal",
        AsyncMock(return_value=("ok", True)),
    )

    async def fake_trial(*args, **kwargs):
        return 0

    monkeypatch.setattr(healer.runner_mod.ParserRunner, "start", fake_trial)
    monkeypatch.setattr(healer.runner_mod.ParserRunner, "wait", fake_trial)

    await healer.heal(parser_id, manual=True, session=session)
    await asyncio.sleep(0)

    assert installed == [parser_dir]

