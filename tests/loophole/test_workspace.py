"""Тест workspace: per-user изоляция, пути на ФС, история."""
from __future__ import annotations

import pytest

from bank_audit.loophole import workspace as ws_mod
from bank_audit.loophole import repository as repo
from bank_audit.loophole.config import LoopholeSettings


from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


@pytest.fixture
def session(sqlite_session):
    return sqlite_session


def test_workspace_dir_isolated_per_user(tmp_path):
    settings = LoopholeSettings(workspace_dir=tmp_path)
    d_a = ws_mod.workspace_dir("user-a", 1, settings=settings)
    d_b = ws_mod.workspace_dir("user-b", 1, settings=settings)
    assert d_a != d_b
    assert d_a.exists() and d_b.exists()
    assert d_a.parent.name == "user-a"
    assert d_b.parent.name == "user-b"


def test_workspace_dir_sanitizes_user_id(tmp_path):
    settings = LoopholeSettings(workspace_dir=tmp_path)
    d = ws_mod.workspace_dir("../etc", 1, settings=settings)
    assert ".." not in d.parts
    assert d.exists()


def test_create_persists_and_makes_dir(session, tmp_path):
    settings = LoopholeSettings(workspace_dir=tmp_path)
    # Подменяем LoopholeSettings.load через monkeypatch.
    import bank_audit.loophole.workspace as ws
    orig = ws.LoopholeSettings.load
    ws.LoopholeSettings.load = staticmethod(lambda: settings)
    try:
        wid = ws_mod.create("user-x", "my ws", session=session)
    finally:
        ws.LoopholeSettings.load = orig
    assert wid > 0
    wss = ws_mod.list_for_user("user-x", session=session)
    assert len(wss) == 1
    assert wss[0]["name"] == "my ws"
    assert (tmp_path / "user-x" / str(wid)).exists()


def test_history(session):
    wid = repo.create_workspace("u", session=session)
    repo.add_chat_message(wid, "user", "q", session=session)
    repo.add_chat_message(wid, "assistant", "a", session=session)
    hist = ws_mod.history(wid, session=session)
    assert len(hist) == 2


def test_save_result(session):
    wid = repo.create_workspace("u", session=session)
    rid = ws_mod.save_result(
        wid, "лазейка", bank_slugs=["sberbank"],
        records=[{"record_id": 1}], session=session,
    )
    assert rid > 0
