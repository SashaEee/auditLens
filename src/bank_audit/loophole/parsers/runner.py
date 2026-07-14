"""Запуск сгенерированных парсеров как subprocess.

Парсер запускается через `python <code_path>`, его stdout должен содержать
JSON-список результатов. Каждый результат сохраняется в loophole_record через
repository.insert_record с дедупом по sha256.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from .. import repository as repo
from ...ai.llm_utils import _loose_json_loads
from ...hashing import sha256_text
from ..models import LoopholeRecord

log = logging.getLogger(__name__)


# Глобальный реестр запущенных парсеров: parser_id -> ParserRunner.
_RUNNING: dict[int, "ParserRunner"] = {}


def _parse_parser_output(stdout: str) -> list[dict]:
    """Парсит JSON-список результатов из stdout парсера.

    Толерантно: сначала прямой json.loads, затем через _loose_json_loads,
    fallback на поиск JSON-блока в тексте.
    """
    if not stdout:
        return []
    # Сначала пробуем прямой парсинг — корректные JSON-списки должны пройти.
    try:
        data = json.loads(stdout)
    except Exception:
        try:
            data = _loose_json_loads(stdout)
        except Exception:
            data = None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            return [d for d in data["results"] if isinstance(d, dict)]
        return [data]
    return []


class ParserRunner:
    """Управляет subprocess парсера: start/stop/status/wait."""

    def __init__(
        self,
        parser_id: int,
        code_path: str,
        *,
        workspace_id: int,
        session: Any = None,
    ) -> None:
        self.parser_id = parser_id
        self.code_path = code_path
        self.workspace_id = workspace_id
        self.session = session
        self._proc: asyncio.subprocess.Process | None = None
        self._returncode: int | None = None
        self._stdout: str = ""

    async def start(self) -> int:
        """Запускает `python <code_path>` как subprocess, ставит status='running'.

        Возвращает pid.
        """
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, self.code_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        repo.update_parser_status(self.parser_id, "running", session=self.session)
        _RUNNING[self.parser_id] = self
        log.info("[parsers.runner] запущен parser_id=%s pid=%s", self.parser_id, self._proc.pid)
        return self._proc.pid

    async def stop(self) -> None:
        """Terminate subprocess, status='stopped'."""
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except Exception as e:
                log.warning("[parsers.runner] terminate failed: %s", e)
                try:
                    self._proc.kill()
                except Exception:
                    pass
        repo.update_parser_status(self.parser_id, "stopped", session=self.session)
        _RUNNING.pop(self.parser_id, None)

    async def status(self) -> dict:
        """{pid, running, returncode, status_db}."""
        pid = self._proc.pid if self._proc is not None else None
        running = (
            self._proc is not None and self._proc.returncode is None
        )
        returncode = (
            self._proc.returncode if self._proc is not None else self._returncode
        )
        row = repo.get_parser(self.parser_id, session=self.session)
        status_db = row.get("status") if row else None
        return {
            "pid": pid,
            "running": running,
            "returncode": returncode,
            "status_db": status_db,
        }

    async def wait(self, timeout: int = 300) -> int:
        """Ждёт завершения, парсит stdout, сохраняет результаты в loophole_record.

        Возвращает количество сохранённых записей.
        """
        if self._proc is None:
            repo.update_parser_status(self.parser_id, "error", session=self.session)
            return 0
        try:
            stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                self._proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.warning("[parsers.runner] timeout parser_id=%s", self.parser_id)
            try:
                self._proc.kill()
            except Exception:
                pass
            repo.update_parser_status(self.parser_id, "error", session=self.session)
            _RUNNING.pop(self.parser_id, None)
            return 0

        self._returncode = self._proc.returncode
        self._stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""

        results = _parse_parser_output(self._stdout)
        saved = 0
        for r in results:
            raw_text = json.dumps(r, ensure_ascii=False, default=str)
            sha = sha256_text(raw_text)
            if repo.exists_sha256(sha, session=self.session):
                continue
            rec = LoopholeRecord(
                sha256=sha,
                title=r.get("title"),
                url=r.get("url"),
                snippet=r.get("snippet"),
                domain=r.get("domain"),
                trust_score=r.get("trust_score"),
                bank_slug=r.get("bank_slug"),
                keyword=r.get("keyword"),
                raw_text=raw_text,
                status="new",
            )
            try:
                repo.insert_record(rec, session=self.session)
                saved += 1
            except Exception as e:
                log.warning("[parsers.runner] insert_record failed: %s", e)

        new_status = "completed" if self._returncode == 0 else "error"
        repo.update_parser_status(self.parser_id, new_status, session=self.session)
        _RUNNING.pop(self.parser_id, None)
        log.info(
            "[parsers.runner] завершён parser_id=%s rc=%s saved=%s",
            self.parser_id, self._returncode, saved,
        )
        return saved
