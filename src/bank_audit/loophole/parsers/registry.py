"""Реестр парсеров: list/get/delete с обогащением runtime-статусом."""
from __future__ import annotations

import json
import logging
import shutil
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


def _parse_json_list(value: Any) -> list:
    """Приводит значение (list или JSON-строка) к списку, иначе []."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    return value if isinstance(value, list) else []


def list_catalog(*, session: Any = None) -> list[dict]:
    """Общий каталог парсеров + статистика карточки.

    Каждая строка: поля парсера + targets/query (из config) + records_count,
    last_run, is_running, pid, needs_attention.
    """
    rows = repo.list_all_parsers(session=session)
    for row in rows:
        _hoist_config(row)
        pid = row.get("parser_id")
        row["records_count"] = repo.count_records_by_parser(pid, session=session)
        row["last_run"] = repo.last_run(pid, session=session)
        runner = _RUNNING.get(pid) if pid is not None else None
        row["is_running"] = runner is not None
        row["pid"] = (
            runner._proc.pid
            if runner is not None and runner._proc is not None
            else None
        )
        row["needs_attention"] = bool(
            (row.get("heal_attempts") or 0) >= 3 and not row.get("auto_enabled")
        )
    return rows


def find_conflicts(source_keys: list[str], *, session: Any = None) -> list[dict]:
    """Парсеры, чьи source_keys пересекаются с переданными (дедуп при создании)."""
    wanted = set(source_keys or [])
    conflicts: list[dict] = []
    if not wanted:
        return conflicts
    for row in repo.list_parsers_with_source_keys(session=session):
        existing = set(_parse_json_list(row.get("source_keys")))
        overlap = sorted(existing & wanted)
        if overlap:
            conflicts.append({
                "parser_id": row["parser_id"],
                "name": row.get("name"),
                "overlap": overlap,
            })
    return conflicts


def get_parser(parser_id: int, *, session: Any = None) -> dict | None:
    """Делегирует в repository.get_parser."""
    return repo.get_parser(parser_id, session=session)


def delete_parser(parser_id: int, *, session: Any = None) -> bool:
    """Удаляет код-файл, директорию парсера и запись из БД.

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
        path = Path(code_path)
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            log.warning("[parsers.registry] не удалось удалить файл %s: %s", code_path, e)
        # Удаляем родительскую директорию парсера целиком, но только если она
        # лежит внутри общего каталога парсеров (защита от относительных путей).
        parser_dir = path.parent
        if parser_dir.name.startswith("parser_"):
            try:
                from .generator import CATALOG_DIR

                if parser_dir.is_relative_to(CATALOG_DIR):
                    shutil.rmtree(parser_dir, ignore_errors=True)
                else:
                    log.warning(
                        "[parsers.registry] parser_dir %s вне каталога %s, пропускаем удаление",
                        parser_dir, CATALOG_DIR,
                    )
            except Exception as e:
                log.warning("[parsers.registry] не удалось удалить директорию %s: %s", parser_dir, e)
    # Удаляем запись из БД напрямую (в repository нет delete_parser).
    try:
        from sqlalchemy import text
        from ... import db
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
