"""Генерация кода Scrapy+Playwright парсеров через LLM.

Сгенерированный код сохраняется в изолированную директорию внутри общего
каталога пакета `parsers/catalog/parser_<parser_id>_<name>/parser.py`
(виден всем пользователям) и регистрируется в loophole_parser с source_keys
для дедупликации. Рядом с кодом создаётся `requirements.txt` и venv.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import repository as repo
from ..config import LoopholeSettings
from . import dedup as dedup_mod

log = logging.getLogger(__name__)

# Общий каталог кода парсеров (внутри пакета; код в git не коммитится).
CATALOG_DIR = Path(__file__).resolve().parent / "catalog"


_BASE_REQUIREMENTS = [
    "scrapy",
    "playwright",
    "playwright-stealth",
    "httpx",
]


PROMPT_TEMPLATE = (
    "Сгенерируй Scrapy-паука на Python для поиска лазеек в банковских "
    "продуктах по запросу: {query}. Используй playwright-stealth для "
    "рендеринга JS. Паук должен собирать title, url, snippet, text. "
    "Код должен писать JSON-логи в stdout и сохранять результаты в файл "
    "results.json рядом с parser.py. "
    "Верни ДВА блока кода: сначала parser.py внутри ```python ... ```, "
    "затем requirements.txt внутри ```text ... ```. Базовые пакеты уже "
    "установлены: scrapy, playwright, playwright-stealth, httpx."
)


def sanitize_filename(name: str) -> str:
    """Безопасное имя файла: только alnum/-_, без точек/пробелов в начале."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "parser"


# Цели парсинга: URL ресурса или группа в мессенджере (Telegram).
TG_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/[^\s<>\"']+", re.IGNORECASE
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TG_HANDLE_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{4,31}\b")


def extract_targets(query: str) -> list[str]:
    """Извлекает из запроса URL ресурсов и группы мессенджеров.

    Возвращает дедуплицированный список целей (Telegram-ссылки первыми).
    """
    targets: list[str] = []
    for pattern in (TG_LINK_RE, URL_RE, TG_HANDLE_RE):
        for match in pattern.findall(query or ""):
            match = match.rstrip(".,);]")
            if match not in targets:
                targets.append(match)
    return targets


def _default_llm() -> Any:
    """ChatOpenAI с теми же env, что и остальные модули loophole."""
    from langchain_openai import ChatOpenAI

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    model = LoopholeSettings.load().effective_classify_model()
    return ChatOpenAI(
        model=model, base_url=base_url, api_key=api_key, temperature=0.3
    )


def _build_messages(query: str) -> list:
    prompt = PROMPT_TEMPLATE.format(query=query)
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        return [
            SystemMessage(
                content=(
                    "Ты — Python-разработчик, генерирующий Scrapy-пауков для "
                    "сбора данных о лазейках в банковских продуктах. Верни "
                    "два блока кода: parser.py внутри ```python ... ``` и "
                    "requirements.txt внутри ```text ... ```, без лишних пояснений."
                )
            ),
            HumanMessage(content=prompt),
        ]
    except Exception:
        return [
            {"role": "system", "content": "Ты — Python-разработчик."},
            {"role": "user", "content": prompt},
        ]


def _strip_code_fences(raw: str) -> str:
    """Убирает markdown ```python ... ``` обёртку, если LLM её добавил."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:python)?\s*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s.strip() + "\n"


def _extract_code_blocks(raw: str) -> dict[str, str]:
    """Извлекает parser.py и requirements.txt из markdown-ответа LLM."""
    import re

    blocks: dict[str, str] = {}
    for lang, key in (("python", "parser.py"), ("text", "requirements.txt")):
        pattern = rf"```(?:{lang})\s*\n(.*?)\n```"
        match = re.search(pattern, raw, re.DOTALL)
        if match:
            blocks[key] = match.group(1).strip() + "\n"
    return blocks


def _parser_dir_path(parser_id: int, name: str) -> Path:
    return CATALOG_DIR / f"parser_{parser_id}_{name}"


def create_parser_dir(parser_id: int, name: str) -> Path:
    path = _parser_dir_path(parser_id, name)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def create_venv(dir_path: Path) -> Path:
    venv_path = dir_path / "venv"
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "venv", str(venv_path)],
        check=True,
        capture_output=True,
    )
    return venv_path


def build_requirements(extras: list[str]) -> str:
    lines = list(_BASE_REQUIREMENTS)
    lines.extend(extras)
    return "\n".join(lines) + "\n"


def write_requirements(dir_path: Path, contents: str) -> Path:
    path = dir_path / "requirements.txt"
    path.write_text(contents, encoding="utf-8")
    return path


def write_parser_code(dir_path: Path, code: str) -> Path:
    path = dir_path / "parser.py"
    path.write_text(code, encoding="utf-8")
    return path


async def install_requirements(dir_path: Path, *, timeout: int = 300) -> None:
    """Устанавливает requirements.txt в venv парсера."""
    from .env import venv_python

    req_path = dir_path / "requirements.txt"
    proc = await asyncio.create_subprocess_exec(
        str(venv_python(dir_path)), "-m", "pip", "install", "-r", str(req_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("pip install timeout")
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install failed: {(stderr or b'').decode('utf-8', errors='replace')[:2000]}"
        )


async def generate_parser(
    user_id: str,
    workspace_id: int,
    query: str,
    *,
    llm: Any = None,
    session: Any = None,
) -> dict:
    """Генерирует Scrapy-паука, сохраняет код в catalog/ и запись в БД.

    Возвращает {"parser_id", "code_path", "venv_path", "name", "targets"}.
    Бросает ValueError, если в запросе нет URL ресурса или группы мессенджера.
    """
    targets = extract_targets(query)
    if not targets:
        raise ValueError(
            "В запросе не указан URL ресурса или группа мессенджера "
            "(например: https://example.com/page или https://t.me/group_name)"
        )
    if llm is None:
        llm = _default_llm()

    name = sanitize_filename(query[:40] or "parser")
    try:
        resp = await llm.ainvoke(_build_messages(query))
        raw = getattr(resp, "content", None) or str(resp)
    except Exception as e:
        log.warning("[parsers.generator] LLM failed: %s", e)
        raise

    blocks = _extract_code_blocks(raw)
    code = blocks.get("parser.py", _strip_code_fences(raw))
    req_text = blocks.get("requirements.txt", "")
    extras = [line.strip() for line in req_text.splitlines() if line.strip()]

    source_keys = [k for k in (dedup_mod.normalize_target(t) for t in targets) if k]

    parser_id = repo.save_parser(
        workspace_id,
        name=name,
        code_path="",
        config={"query": query, "targets": targets},
        created_by=user_id,
        source_keys=source_keys,
        session=session,
    )

    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    parser_dir = create_parser_dir(parser_id, name)
    venv_path = await create_venv(parser_dir)
    requirements = build_requirements(extras)
    write_requirements(parser_dir, requirements)
    await install_requirements(parser_dir)
    code_path = write_parser_code(parser_dir, code)

    repo.update_parser_code_path(parser_id, str(code_path), session=session)
    log.info(
        "[parsers.generator] создан парсер id=%s name=%s path=%s targets=%s",
        parser_id, name, code_path, targets,
    )
    return {
        "parser_id": parser_id,
        "code_path": str(code_path),
        "venv_path": str(venv_path),
        "name": name,
        "targets": targets,
    }
