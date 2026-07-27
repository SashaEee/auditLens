"""Запуск сгенерированных парсеров как subprocess.

Парсер запускается через `python <code_path>`, stdout/stderr читаются
построчно: каждая строка транслируется в лог-шину (SSE для UI) и в кольцевой
буфер log_tail. По завершении stdout парсится как JSON-список результатов,
записи сохраняются в loophole_record с трёхключевой дедупликацией
(raw sha256 → полный URL → text_sha256), итог фиксируется в loophole_parser_run.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
from pathlib import Path
from typing import Any

from .. import repository as repo
from ...ai.llm_utils import _loose_json_loads
from ...hashing import sha256_text
from ..models import LoopholeRecord
from . import dedup as dedup_mod
from . import env

log = logging.getLogger(__name__)

# Реестры: запущенные парсеры и лог-шина (run_id → подписчики/хвост/финал).
_RUNNING: dict[int, "ParserRunner"] = {}
_LOG_BUS: dict[int, list[asyncio.Queue]] = {}
_LOG_TAIL: dict[int, collections.deque] = {}
_FINISHED: dict[int, dict] = {}
_FINISHED_MAX = 500
_TAIL_LINES = 400
_LOG_TAIL_CHARS = 8000


def _timeout_s() -> int:
    return int(os.getenv("PARSER_RUN_TIMEOUT_S", "900"))


def _venv_python(code_path: str) -> Path:
    return env.venv_python(Path(code_path).parent)


def _format_log_line(line: str) -> str:
    try:
        obj = json.loads(line)
        if isinstance(obj, dict) and "msg" in obj:
            return str(obj["msg"])
    except Exception:
        pass
    return line


# ── лог-шина (публичный API для web.py и healer.py) ─────────────────────────
def subscribe(run_id: int) -> asyncio.Queue:
    """Очередь событий лога. Завершённый run сразу отдаёт 'done'."""
    q: asyncio.Queue = asyncio.Queue()
    if run_id in _FINISHED:
        q.put_nowait({"event": "done", "data": json.dumps(_FINISHED[run_id], ensure_ascii=False)})
    else:
        _LOG_BUS.setdefault(run_id, []).append(q)
    return q


def unsubscribe(run_id: int, q: asyncio.Queue) -> None:
    subs = _LOG_BUS.get(run_id)
    if subs and q in subs:
        subs.remove(q)


def log_tail(run_id: int) -> list[str]:
    return list(_LOG_TAIL.get(run_id, ()))


def emit_line(run_id: int, line: str) -> None:
    _LOG_TAIL.setdefault(run_id, collections.deque(maxlen=_TAIL_LINES)).append(line)
    for q in list(_LOG_BUS.get(run_id, [])):
        q.put_nowait({"event": "log", "data": line})


def finish_stream(run_id: int, payload: dict) -> None:
    """Закрывает шину run'а событием 'done' (payload — финальный статус)."""
    _FINISHED[run_id] = payload
    while len(_FINISHED) > _FINISHED_MAX:
        _FINISHED.pop(next(iter(_FINISHED)))
    # _LOG_TAIL чистим той же FIFO-дисциплиной по ёмкости; текущий run_id
    # не вытесняем здесь — поздний подписчик может запросить replay хвоста.
    while len(_LOG_TAIL) > _FINISHED_MAX:
        oldest = next((k for k in _LOG_TAIL if k != run_id), None)
        if oldest is None:
            break
        _LOG_TAIL.pop(oldest)
    msg = {"event": "done", "data": json.dumps(payload, ensure_ascii=False)}
    for q in list(_LOG_BUS.pop(run_id, [])):
        q.put_nowait(msg)


# ── парсинг вывода ───────────────────────────────────────────────────────────
def _parse_parser_output(stdout: str) -> list[dict]:
    """JSON-список результатов из stdout (толерантно: прямой/loose/блок)."""
    if not stdout:
        return []
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


# ── фоновый запуск (API для web.py и scheduler.py) ──────────────────────────
async def run(parser_id: int, trigger: str, *, session: Any = None) -> int:
    """Создаёт run, стартует subprocess, wait() в фоне. Возвращает run_id.

    RuntimeError — парсер уже running; ValueError — не найден/нет code_path.

    session используется только для синхронной части (проверки/create_run);
    фоновая wait() работает с той же сессией в том же event loop — в проде
    передавать session=None.
    """
    if parser_id in _RUNNING:
        raise RuntimeError(f"parser {parser_id} already running")
    row = repo.get_parser(parser_id, session=session)
    if row is None:
        raise ValueError("parser not found")
    code_path = row.get("code_path")
    if not code_path:
        raise ValueError("parser has no code_path")
    run_id = repo.create_run(parser_id, trigger, session=session)
    runner = ParserRunner(
        parser_id, code_path,
        workspace_id=row.get("workspace_id"),
        session=session, run_id=run_id, trigger=trigger,
    )
    try:
        await runner.start()
    except Exception as e:
        # run-запись уже создана — не оставляем её «running» сиротой.
        repo.finish_run(run_id, "error", error_text=str(e)[:4000], session=session)
        raise
    asyncio.create_task(runner.wait())
    return run_id


class ParserRunner:
    """Управляет subprocess парсера: start/stop/status/wait + лог-шина."""

    def __init__(
        self,
        parser_id: int,
        code_path: str,
        *,
        workspace_id: int,
        session: Any = None,
        run_id: int | None = None,
        trigger: str = "manual",
    ) -> None:
        self.parser_id = parser_id
        self.code_path = code_path
        self.workspace_id = workspace_id
        self.session = session
        self.run_id = run_id
        self.trigger = trigger
        self._proc: asyncio.subprocess.Process | None = None
        self._returncode: int | None = None
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._finalized = False

    async def start(self) -> int:
        """Запускает `python <code_path>`, status='running'. Возвращает pid."""
        # Регистрируем слот ДО await, чтобы закрыть check-then-set гонку
        # между проверкой `parser_id in _RUNNING` в run() и фактическим стартом.
        _RUNNING[self.parser_id] = self
        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(_venv_python(self.code_path)), self.code_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Парсеры печатают весь JSON одной строкой — лимит readline
                # по умолчанию (64 KiB) даёт ValueError на длинных строках.
                limit=10 * 1024 * 1024,
            )
        except Exception:
            _RUNNING.pop(self.parser_id, None)
            raise
        if self.run_id is None:
            self.run_id = repo.create_run(self.parser_id, self.trigger, session=self.session)
        repo.update_parser_status(self.parser_id, "running", session=self.session)
        log.info("[parsers.runner] запущен parser_id=%s pid=%s run_id=%s",
                 self.parser_id, self._proc.pid, self.run_id)
        return self._proc.pid

    async def _pump(self, stream, sink: list[str]) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            text_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            sink.append(text_line)
            if self.run_id is not None:
                emit_line(self.run_id, _format_log_line(text_line))

    async def stop(self) -> None:
        """Terminate subprocess; run фиксируется как error('stopped by user')."""
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
        if self.run_id is not None and not self._finalized:
            self._finalized = True
            repo.finish_run(self.run_id, "error", error_text="stopped by user",
                            session=self.session)
            finish_stream(self.run_id, {"status": "error",
                                        "error_text": "stopped by user"})
        _RUNNING.pop(self.parser_id, None)

    async def status(self) -> dict:
        pid = self._proc.pid if self._proc is not None else None
        running = self._proc is not None and self._proc.returncode is None
        returncode = (
            self._proc.returncode if self._proc is not None else self._returncode
        )
        row = repo.get_parser(self.parser_id, session=self.session)
        return {
            "pid": pid,
            "running": running,
            "returncode": returncode,
            "status_db": row.get("status") if row else None,
        }

    async def wait(self, timeout: int | None = None, *, finalize: bool = True) -> int:
        """Ждёт завершения, сохраняет результаты, финализирует run.

        Возвращает количество новых сохранённых записей.
        """
        timeout = timeout if timeout is not None else _timeout_s()
        if self._proc is None:
            self._finalize("error", 0, 0, 0, "process not started")
            return 0
        tasks = [
            asyncio.ensure_future(self._pump(self._proc.stdout, self._stdout_lines)),
            asyncio.ensure_future(self._pump(self._proc.stderr, self._stderr_lines)),
            asyncio.ensure_future(self._proc.wait()),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("[parsers.runner] timeout parser_id=%s", self.parser_id)
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                await self._proc.wait()  # reap после kill, иначе зомби
            except Exception as e:
                log.warning("[parsers.runner] reap after kill failed: %s", e)
            self._finalize("error", 0, 0, 0, f"timeout {timeout}s")
            return 0
        except Exception as e:
            # Любая ошибка pump/proc не должна оставлять run висеть 'running':
            # финализируем как error и отменяем оставшиеся задачи (иначе
            # "Task exception was never retrieved" / подвисшие корутины).
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._finalize("error", 0, 0, 0, f"{type(e).__name__}: {e}"[:4000])
            return 0

        if self._finalized:  # stop() уже зафиксировал итог
            return 0

        self._returncode = self._proc.returncode
        results_path = Path(self.code_path).parent / "results.json"
        if results_path.exists():
            try:
                results = _parse_parser_output(results_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("[parsers.runner] failed to read results.json: %s", e)
                results = []
        else:
            # Fallback: старые парсеры могут писать результаты в stdout.
            stdout = "\n".join(self._stdout_lines)
            results = _parse_parser_output(stdout)
        found = len(results)
        new = dup = 0
        for r in results:
            raw_text = json.dumps(r, ensure_ascii=False, default=str)
            sha = sha256_text(raw_text)
            url = (r.get("url") or "").strip()
            tsha = dedup_mod.page_text_sha256(r.get("text"), r.get("snippet"))
            if (
                repo.exists_sha256(sha, session=self.session)
                or (url and repo.exists_url(url, session=self.session))
                or (tsha and repo.exists_text_sha256(tsha, session=self.session))
            ):
                dup += 1
                continue
            rec = LoopholeRecord(
                sha256=sha,
                parser_id=self.parser_id,
                text_sha256=tsha,
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
                new += 1
            except Exception as e:
                log.warning("[parsers.runner] insert_record failed url=%s sha=%s: %s",
                            url, sha, e)

        if finalize:
            if self._returncode == 0:
                status = "success" if found > 0 else "empty"
                err = None
            else:
                status = "error"
                err = "\n".join(self._stderr_lines)[-4000:] or f"returncode={self._returncode}"
            self._finalize(status, found, new, dup, err)
        return new

    def _finalize(self, status: str, found: int, new: int, dup: int,
                  error_text: str | None) -> None:
        if self._finalized:
            return
        self._finalized = True
        tail = None
        if self.run_id is not None:
            tail = "\n".join(log_tail(self.run_id))[-_LOG_TAIL_CHARS:]
            repo.finish_run(
                self.run_id, status,
                items_found=found, items_new=new, items_dup=dup,
                error_text=error_text, log_tail=tail, session=self.session,
            )
            finish_stream(self.run_id, {
                "status": status, "items_found": found, "items_new": new,
                "items_dup": dup, "error_text": error_text,
            })
        repo.update_parser_status(self.parser_id, status, session=self.session)
        _RUNNING.pop(self.parser_id, None)
        log.info("[parsers.runner] завершён parser_id=%s status=%s new=%s dup=%s",
                 self.parser_id, status, new, dup)
