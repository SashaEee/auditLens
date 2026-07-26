"""Реестр парсеров: list/get/delete с обогащением runtime-статусом."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .. import repository as repo
from .runner import _RUNNING

log = logging.getLogger(__name__)


def _hoist_config(row: dict) -> None:
    """Поднимает query/targets из config (dict или JSON-строка) наверх."""
    cfg = row.get("config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    row["targets"] = cfg.get("targets") or []
    row["query"] = cfg.get("query") or ""


def list_parsers(workspace_id: int, *, session: Any = None) -> list[dict]:
    """Список парсеров workspace + runtime-статус из _RUNNING."""
    rows = repo.list_parsers(workspace_id, session=session)
    for row in rows:
        _hoist_config(row)
        pid = row.get("parser_id")
        runner = _RUNNING.get(pid) if pid is not None else None
        row["is_running"] = runner is not None
        if runner is not None and runner._proc is not None:
            row["pid"] = runner._proc.pid
        else:
            row["pid"] = None
    return rows


def get_parser(parser_id: int, *, session: Any = None) -> dict | None:
    """Делегирует в repository.get_parser."""
    return repo.get_parser(parser_id, session=session)


def delete_parser(parser_id: int, *, session: Any = None) -> bool:
    """Удаляет код-файл и запись из БД (если парсер не running).

    Возвращает True если удалено, False если парсер running или не найден.
    """
    if parser_id in _RUNNING:
        log.warning("[parsers.registry] нельзя удалить running parser_id=%s", parser_id)
        return False
    row = repo.get_parser(parser_id, session=session)
    if row is None:
        return False
    code_path = row.get("code_path")
    if code_path:
        try:
            Path(code_path).unlink(missing_ok=True)
        except Exception as e:
            log.warning("[parsers.registry] не удалось удалить файл %s: %s", code_path, e)
    # Удаляем запись из БД напрямую (в repository нет delete_parser).
    try:
        from sqlalchemy import text
        from .. import db
        from .. import db_schema as schema

        if session is not None:
            session.execute(
                text(f"DELETE FROM {schema.T_PARSER} WHERE parser_id = :id"),
                {"id": parser_id},
            )
        else:
            with db.session() as s:
                s.execute(
                    text(f"DELETE FROM {schema.T_PARSER} WHERE parser_id = :id"),
                    {"id": parser_id},
                )
    except Exception as e:
        log.warning("[parsers.registry] не удалось удалить запись БД: %s", e)
        return False
    return True
