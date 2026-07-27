"""Самовосстановление парсеров через nanobot.

Логика: тикер смотрит последний run каждого auto-парсера. 'success' — ничего
не делаем (и сбрасываем heal_attempts). 'error'/'empty' — запускаем heal:
nanobot сам пробует получить данные из источника (audit_fetch_target),
анализирует причину и патчит код (audit_patch_parser); пропатченный парсер
проходит пробный запуск. 3 подряд неудачи → автозапуск отключается.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from ... import db
from .. import repository as repo
from . import generator as generator_mod
from . import runner as runner_mod

log = logging.getLogger(__name__)

MAX_HEAL_ATTEMPTS = int(os.getenv("PARSER_MAX_HEAL_ATTEMPTS", "3"))

# parser_id, для которых heal уже выполняется (409 на параллельный запуск).
_HEALING: set[int] = set()

_HEAL_PROMPT = (
    "Парсер '{name}' (parser_id={parser_id}) собирает данные из источников: "
    "{targets}. Последний запуск завершился со статусом '{status}'. "
    "Ошибка/хвост лога:\n{error}\n\n"
    "Текущий код парсера:\n```python\n{code}\n```\n\n"
    "Задача:\n"
    "1. Через audit_fetch_target проверь доступность источников и изучи "
    "структуру страницы.\n"
    "2. Если источник недоступен — НЕ патчь код, опиши причину в ответе.\n"
    "3. Если страница доступна, но структура изменилась — исправь код "
    "(селекторы/логику) и сохрани через audit_patch_parser.\n"
    "4. В финальном ответе: диагноз, что исправлено, ожидаемый результат."
)


def nanobot_available() -> bool:
    try:
        import nanobot  # noqa: F401

        return True
    except Exception:
        return False


async def heal_tick(now: datetime | None = None) -> list[int]:
    """Фаза тикера: запускает heal для сбойных auto-парсеров."""
    if not nanobot_available():
        return []
    healed: list[int] = []
    with db.session() as s:
        parsers = repo.list_auto_parsers(session=s)
        for p in parsers:
            pid = p["parser_id"]
            lr = repo.last_run(pid, session=s)
            if lr is None:
                continue
            if lr["status"] == "success":
                if (p.get("heal_attempts") or 0) > 0:
                    repo.set_heal_attempts(pid, 0, session=s)
                continue
            if lr["status"] not in ("error", "empty"):
                continue
            attempts = p.get("heal_attempts") or 0
            if attempts >= MAX_HEAL_ATTEMPTS:
                repo.disable_auto(pid, session=s)
                log.warning(
                    "[healer] parser_id=%s: %s попыток исчерпано, автозапуск отключён",
                    pid, attempts,
                )
                continue
            if pid in _HEALING:
                continue
            try:
                await heal(pid, session=s)
                healed.append(pid)
            except Exception:
                log.exception("[healer] heal failed parser_id=%s", pid)
    return healed


async def heal(parser_id: int, *, manual: bool = False, session=None) -> int:
    """Создаёт heal-run и запускает фонового воркера. Возвращает run_id.

    RuntimeError — heal уже идёт; ValueError — парсер не найден.
    Ручной вызов сбрасывает heal_attempts (новый шанс после вмешательства).
    """
    if parser_id in _HEALING:
        raise RuntimeError(f"heal already running for parser {parser_id}")
    row = repo.get_parser(parser_id, session=session)
    if row is None:
        raise ValueError("parser not found")
    if manual:
        repo.set_heal_attempts(parser_id, 0, session=session)
    run_id = repo.create_run(parser_id, "heal", session=session)
    _HEALING.add(parser_id)
    asyncio.create_task(_heal_worker(parser_id, run_id, session=None))
    return run_id


async def _heal_worker(parser_id: int, run_id: int, *, session=None) -> None:
    """Фон: nanobot-анализ → патч → пробный запуск → фиксация результата."""
    try:
        row = repo.get_parser(parser_id, session=session)
        runner_mod.emit_line(run_id, f"[healer] старт анализа parser_id={parser_id}")
        report, patched = await _run_nanobot_heal(row, run_id)
        attempts = (row.get("heal_attempts") or 0) + 1

        if not patched:
            repo.set_heal_attempts(parser_id, attempts, session=session)
            repo.finish_run(run_id, "error", heal_report=report[:8000],
                            session=session)
            runner_mod.finish_stream(run_id, {"status": "error",
                                              "heal_report": report[:8000]})
            if attempts >= MAX_HEAL_ATTEMPTS:
                repo.disable_auto(parser_id, session=session)
            return

        # Код пропатчен → обновляем зависимости.
        code_path = repo.get_parser(parser_id, session=session)["code_path"]
        parser_dir = Path(code_path).parent
        try:
            runner_mod.emit_line(run_id, "[healer] обновление зависимостей")
            await generator_mod.install_requirements(parser_dir)
            runner_mod.emit_line(run_id, "[healer] зависимости обновлены")
        except Exception as e:
            log.warning("[healer] pip install failed parser_id=%s: %s", parser_id, e)
            runner_mod.emit_line(run_id, f"[healer] ошибка установки: {e}")
            repo.set_heal_attempts(parser_id, attempts, session=session)
            repo.finish_run(run_id, "error", error_text=str(e)[:4000],
                            session=session)
            runner_mod.finish_stream(run_id, {"status": "error",
                                              "error_text": str(e)[:4000]})
            return

        # Код пропатчен → пробный запуск парсера.
        runner_mod.emit_line(run_id, "[healer] код исправлен, пробный запуск")
        code_path = repo.get_parser(parser_id, session=session)["code_path"]
        trial_id = repo.create_run(parser_id, "heal", session=session)
        trial = runner_mod.ParserRunner(
            parser_id, code_path,
            workspace_id=row.get("workspace_id"),
            session=session, run_id=trial_id, trigger="heal",
        )
        await trial.start()
        saved = await trial.wait()
        trial_run = repo.get_run(trial_id, session=session)
        ok = bool(trial_run and trial_run["status"] == "success")

        if ok:
            repo.set_heal_attempts(parser_id, 0, session=session)
        else:
            repo.set_heal_attempts(parser_id, attempts, session=session)
            if attempts >= MAX_HEAL_ATTEMPTS:
                repo.disable_auto(parser_id, session=session)
        status = "success" if ok else "error"
        repo.finish_run(run_id, status, items_new=saved,
                        heal_report=report[:8000], session=session)
        runner_mod.finish_stream(run_id, {
            "status": status, "heal_report": report[:8000], "items_new": saved,
        })
    except Exception as e:
        log.exception("[healer] worker failed parser_id=%s", parser_id)
        try:
            row = repo.get_parser(parser_id, session=session)
            attempts = ((row or {}).get("heal_attempts") or 0) + 1
            repo.set_heal_attempts(parser_id, attempts, session=session)
            if attempts >= MAX_HEAL_ATTEMPTS:
                repo.disable_auto(parser_id, session=session)
            repo.finish_run(run_id, "error", error_text=str(e)[:4000], session=session)
            runner_mod.finish_stream(run_id, {"status": "error",
                                              "error_text": str(e)[:4000]})
        except Exception:
            pass
    finally:
        _HEALING.discard(parser_id)


async def _run_nanobot_heal(row: dict, run_id: int) -> tuple[str, bool]:
    """Прогон nanobot над сбойным парсером. Возвращает (report, patched)."""
    from ..chat.nanobot_agent import create_nanobot
    from ..chat.tools_nanobot import NANOBOT_HEAL_TOOLS

    code_path = row.get("code_path") or ""
    path = Path(code_path) if code_path else None
    before = path.read_text(encoding="utf-8") if path and path.exists() else ""

    cfg = row.get("config")
    if isinstance(cfg, str):
        import json as _json

        try:
            cfg = _json.loads(cfg)
        except Exception:
            cfg = {}
    targets = (cfg or {}).get("targets") or []
    # Последний run ИСКЛЮЧАЯ текущий heal-run (он 'running', без error_text).
    last = next(
        (r for r in repo.list_runs(row["parser_id"], limit=5)
         if r["run_id"] != run_id),
        None,
    )
    error_info = ((last or {}).get("error_text") or (last or {}).get("log_tail") or "")

    prompt = _HEAL_PROMPT.format(
        name=row.get("name"),
        parser_id=row["parser_id"],
        targets=", ".join(targets),
        status=(last or {}).get("status", "?"),
        error=error_info[:4000],
        code=before[:20000],
    )

    bot, config_path = create_nanobot(extra_tools=NANOBOT_HEAL_TOOLS)
    try:
        result = await bot.run(prompt, session_key=f"heal:{row['parser_id']}:{run_id}",
                               channel="loophole-heal")
        answer = result.content or ""
    finally:
        await bot.aclose()
        Path(config_path).unlink(missing_ok=True)

    patched = bool(
        path and path.exists()
        and path.read_text(encoding="utf-8") != before
    )
    return str(answer), patched
