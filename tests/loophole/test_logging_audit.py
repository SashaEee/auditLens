"""Тест logging_audit: каждая запись лога содержит user_id, action, detail."""
from __future__ import annotations

import json

from bank_audit.loophole import logging_audit
from bank_audit.loophole import repository as repo


from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


def test_log_action_stores_fields(sqlite_session):
    lid = logging_audit.log_action(
        "user-1", "search",
        workspace_id=42, detail={"q": "лазейка"}, ip="10.0.0.1",
        session=sqlite_session,
    )
    assert lid > 0
    actions = logging_audit.list_actions("user-1", session=sqlite_session)
    assert len(actions) == 1
    a = actions[0]
    assert a["user_id"] == "user-1"
    assert a["action"] == "search"
    assert a["workspace_id"] == 42
    assert a["ip"] == "10.0.0.1"
    detail = json.loads(a["detail"])
    assert detail == {"q": "лазейка"}


def test_log_action_minimal(sqlite_session):
    lid = logging_audit.log_action("u", "export", session=sqlite_session)
    assert lid > 0
    a = logging_audit.list_actions("u", session=sqlite_session)[0]
    assert a["action"] == "export"


def test_log_action_fail_safe(sqlite_session):
    """При сбое (невалидная сессия) — не падает, возвращает -1."""
    bad = object()
    lid = logging_audit.log_action("u", "x", session=bad)
    assert lid == -1


def test_list_actions_isolated_per_user(sqlite_session):
    logging_audit.log_action("a", "search", session=sqlite_session)
    logging_audit.log_action("b", "search", session=sqlite_session)
    assert len(logging_audit.list_actions("a", session=sqlite_session)) == 1
    assert len(logging_audit.list_actions("b", session=sqlite_session)) == 1
