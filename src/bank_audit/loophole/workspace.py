"""Per-user workspace: путь на ФС + история + результаты.

Workspace-директория: <LOOPHOLE_WORKSPACE_DIR>/<user_id>/<workspace_id>/.
Изоляция per-user — по user_id (из заголовка X-User-Id).
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import repository as repo
from .config import LoopholeSettings

log = logging.getLogger(__name__)


def workspace_dir(user_id: str, workspace_id: int,
                  *, settings: LoopholeSettings | None = None) -> Path:
    """Возвращает (и создаёт) директорию workspace на ФС."""
    settings = settings or LoopholeSettings.load()
    d = settings.workspace_dir / _safe(user_id) / str(workspace_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    """Безопасное имя для ФС (без ../ и пр.)."""
    return "".join(c for c in (name or "anonymous") if c.isalnum() or c in "-_") or "anonymous"


def create(user_id: str, name: str | None = None, *, session=None) -> int:
    """Создаёт workspace в БД + директорию на ФС. Возвращает workspace_id."""
    wid = repo.create_workspace(user_id, name=name, session=session)
    try:
        workspace_dir(user_id, wid)
    except Exception as e:
        log.warning("[workspace] не удалось создать директорию: %s", e)
    return wid


def list_for_user(user_id: str, *, session=None) -> list[dict]:
    return repo.list_workspaces(user_id, session=session)


def history(workspace_id: int, *, session=None) -> list[dict]:
    return repo.list_chat_history(workspace_id, session=session)


def save_result(
    workspace_id: int,
    query_text: str,
    *,
    period_from=None,
    period_to=None,
    bank_slugs: list[str] | None = None,
    records: list[dict] | None = None,
    session=None,
) -> int:
    return repo.save_result(
        workspace_id, query_text,
        period_from=period_from, period_to=period_to,
        bank_slugs=bank_slugs, records=records, session=session,
    )
