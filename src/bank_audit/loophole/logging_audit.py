"""Логирование действий пользователя в loophole_action_log.

Каждый эндпоинт web.py вызывает log_action() через dependency. user_id —
username trusted principal (X-Authentik-* от nginx, см. loophole/web.py).
"""
from __future__ import annotations

import logging

from . import repository as repo

log = logging.getLogger(__name__)


def log_action(
    user_id: str,
    action: str,
    *,
    workspace_id: int | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    session=None,
) -> int:
    """Записывает действие в loophole_action_log. Возвращает log_id."""
    try:
        return repo.log_action(
            user_id, action,
            workspace_id=workspace_id, detail=detail, ip=ip, session=session,
        )
    except Exception as e:
        log.warning("[logging_audit] failed to log action %s: %s", action, e)
        return -1


def list_actions(user_id: str, *, limit: int = 100, session=None) -> list[dict]:
    return repo.list_actions(user_id, limit=limit, session=session)
