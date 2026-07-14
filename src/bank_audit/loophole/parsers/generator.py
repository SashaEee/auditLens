"""Генерация кода Scrapy+Playwright парсеров через LLM.

Сгенерированный код сохраняется ТОЛЬКО в workspace-директории
`workspace/loophole/<user>/<ws>/parsers/` (вне репозитория) и регистрируется
в таблице loophole_parser через repository.save_parser.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .. import repository as repo
from ..config import LoopholeSettings
from ..workspace import workspace_dir

log = logging.getLogger(__name__)


PROMPT_TEMPLATE = (
    "Сгенерируй Scrapy-паука на Python для поиска лазеек в банковских "
    "продуктах по запросу: {query}. Используй playwright-stealth для "
    "рендеринга JS. Паук должен собирать title, url, snippet, text. "
    "Возвращает JSON-список результатов. Код должен быть готов к запуску "
    "через `scrapy crawl`."
)


def sanitize_filename(name: str) -> str:
    """Безопасное имя файла: только alnum/-_, без точек/пробелов в начале."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "parser"


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
                    "сбора данных о лазейках в банковских продуктах. Возвращай "
                    "ТОЛЬКО валидный Python-код без markdown-обёрток и "
                    "пояснений."
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


async def generate_parser(
    user_id: str,
    workspace_id: int,
    query: str,
    *,
    llm: Any = None,
    session: Any = None,
) -> dict:
    """Генерирует Scrapy-паука через LLM, сохраняет код в workspace и БД.

    Возвращает {"parser_id", "code_path", "name"}.
    """
    if llm is None:
        llm = _default_llm()

    name = sanitize_filename(query[:40] or "parser")
    messages = _build_messages(query)
    try:
        resp = await llm.ainvoke(messages)
        raw = getattr(resp, "content", None) or str(resp)
    except Exception as e:
        log.warning("[parsers.generator] LLM failed: %s", e)
        raise

    code = _strip_code_fences(raw)

    parsers_dir = workspace_dir(user_id, workspace_id) / "parsers"
    parsers_dir.mkdir(parents=True, exist_ok=True)
    code_path = parsers_dir / f"parser_{name}.py"
    code_path.write_text(code, encoding="utf-8")

    parser_id = repo.save_parser(
        workspace_id,
        name=name,
        code_path=str(code_path),
        config={"query": query},
        session=session,
    )
    log.info(
        "[parsers.generator] создан парсер id=%s name=%s path=%s",
        parser_id, name, code_path,
    )
    return {"parser_id": parser_id, "code_path": str(code_path), "name": name}
