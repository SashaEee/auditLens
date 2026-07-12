"""Tools графа чата loophole: веб-поиск, retrieve, экспорт, keywords,
extract_loopholes, db_query, table_load, парсеры.

Каждый tool — обычная функция (вызывается напрямую из nodes/phases и из тестов).
Имена команд (с ведущим ``/``) используются в чате и в TOOL_REGISTRY/dispatch.

Промпты LLM-инструментов (keywords, extract_loopholes) лежат в
``chat/prompt/06_keywords.md`` и ``chat/prompt/04_extract_loopholes.md``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import repository as repo
from ..adapters import search_decorator, fetch_decorator
from ..pii_mask import mask as pii_mask

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompt"


def load_prompt(name: str) -> str:
    """Читает промпт из ``chat/prompt/<name>.md`` (UTF-8)."""
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


# ── Веб / retrieve / export ──────────────────────────────────────────────────
def web_search(query: str, *, max_results: int = 8, _impl: Any = None) -> list[dict]:
    """/web_search — поиск в web через adapters.search_decorator."""
    return search_decorator.search(query, max_results=max_results, _impl=_impl)


def web_fetch(url: str, *, _impl: Any = None) -> dict | None:
    """/web_fetch — загрузка страницы через adapters.fetch_decorator."""
    page = fetch_decorator.fetch_and_parse(url, _fetch_impl=_impl)
    if page is None:
        return None
    return {
        "url": page.url,
        "final_url": page.final_url,
        "status": page.status,
        "title": page.title,
        "excerpt": page.excerpt,
        "via": page.via,
    }


def retrieve_loopholes(
    query: str,
    *,
    bank_slugs: list[str] | None = None,
    limit: int = 20,
    session=None,
) -> list[dict]:
    """/retrieve — поиск по loophole_record."""
    return repo.search_relevant(
        query, bank_slugs=bank_slugs, only_loophole=True, limit=limit, session=session
    )


def refine_export(records: list[dict], *, format: str = "json") -> dict:
    """/export — подготовка записей к экспорту."""
    return {"format": format, "count": len(records), "records": records}


# ── keywords / extract_loopholes (LLM) ───────────────────────────────────────
def _default_llm() -> Any:
    """ChatOpenAI с теми же env, что и остальные модули loophole."""
    from langchain_openai import ChatOpenAI
    import os

    from ..config import LoopholeSettings

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    model = LoopholeSettings.load().effective_chat_model()
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key, temperature=0.3)


def _llm_content(resp: Any) -> str:
    return getattr(resp, "content", None) or str(resp)


async def keywords(
    query: str,
    *,
    existing: list[str] | None = None,
    _impl: Any = None,
) -> list[str]:
    """/keywords — генерация 10-15 ключевых слов через LLM (06_keywords.md).

    ``_impl`` — инъекция async-callable (мок) для тестов; принимает
    ``(system, user)`` и возвращает сырой ответ LLM (str).
    """
    from ..ai.llm_utils import _loose_json_loads

    system = load_prompt("06_keywords")
    existing_str = ", ".join(existing) if existing else "(нет)"
    user = (
        f"Уточнённый запрос:\n{query}\n\n"
        f"Уже использованные ключевые слова:\n{existing_str}\n\n"
        f"Верни JSON по контракту."
    )
    try:
        if _impl is not None:
            raw = await _impl(system, user)
        else:
            llm = _default_llm()
            from langchain_core.messages import HumanMessage, SystemMessage

            resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
            raw = _llm_content(resp)
        data = _loose_json_loads(raw)
    except Exception as e:
        log.warning("[keywords] failed: %s", e)
        return []
    if isinstance(data, dict):
        kws = data.get("keywords") or []
    elif isinstance(data, list):
        kws = data
    else:
        return []
    return [str(k).strip() for k in kws if str(k).strip()][:15]


async def extract_loopholes(
    text: str,
    *,
    llm: Any = None,
) -> list[dict]:
    """/extract_loopholes — извлечение лазеек из текста (04_extract_loopholes.md).

    Перед отправкой в LLM текст маскируется через ``pii_mask.mask``.
    Возвращает список dict с полями title, description, category, severity,
    evidence_quote, is_loophole.
    """
    from ..ai.llm_utils import _loose_json_loads

    masked_text, _ = pii_mask(text or "")
    system = load_prompt("04_extract_loopholes")
    user = f"Текст для анализа:\n{masked_text}\n\nВерни JSON по контракту."
    try:
        if llm is None:
            llm = _default_llm()
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
        raw = _llm_content(resp)
        data = _loose_json_loads(raw)
    except Exception as e:
        log.warning("[extract_loopholes] failed: %s", e)
        return []
    if isinstance(data, dict):
        loopholes = data.get("loopholes") or []
    elif isinstance(data, list):
        loopholes = data
    else:
        return []
    out: list[dict] = []
    for item in loopholes:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": str(item.get("title") or ""),
            "description": str(item.get("description") or ""),
            "category": str(item.get("category") or ""),
            "severity": str(item.get("severity") or "medium"),
            "evidence_quote": str(item.get("evidence_quote") or ""),
            "is_loophole": bool(item.get("is_loophole", False)),
        })
    return out


# ── db_query / table_load ────────────────────────────────────────────────────
def db_query(
    *,
    bank_slugs: list[str] | None = None,
    period_from: Any = None,
    period_to: Any = None,
    query_text: str | None = None,
    only_loophole: bool | None = None,
    status: str | None = None,
    limit: int = 500,
    offset: int = 0,
    session=None,
) -> list[dict]:
    """/db_query — список записей loophole_record с фильтрами."""
    return repo.list_records(
        bank_slugs=bank_slugs,
        period_from=period_from,
        period_to=period_to,
        query_text=query_text,
        only_loophole=only_loophole,
        status=status,
        limit=limit,
        offset=offset,
        session=session,
    )


def table_load(
    *,
    bank_slugs: list[str] | None = None,
    period_from: Any = None,
    period_to: Any = None,
    query_text: str | None = None,
    only_loophole: bool = True,
    status: str | None = None,
    limit: int = 200,
    session=None,
) -> list[dict]:
    """/table_load — записи для таблицы фронта (only_loophole=True по умолчанию)."""
    return repo.list_records(
        bank_slugs=bank_slugs,
        period_from=period_from,
        period_to=period_to,
        query_text=query_text,
        only_loophole=only_loophole,
        status=status,
        limit=limit,
        session=session,
    )


# ── Парсеры ──────────────────────────────────────────────────────────────────
async def parser_create(
    user_id: str,
    workspace_id: int,
    query: str,
    *,
    llm: Any = None,
    session=None,
) -> dict:
    """/parser_create — генерация Scrapy-парсера через LLM."""
    from ..parsers.generator import generate_parser

    return await generate_parser(
        user_id, workspace_id, query, llm=llm, session=session
    )


async def parser_start(
    parser_id: int,
    *,
    workspace_id: int,
    session=None,
) -> int:
    """/parser_start — запуск парсера как subprocess. Возвращает pid."""
    from ..parsers.runner import ParserRunner

    row = repo.get_parser(parser_id, session=session)
    if not row:
        raise ValueError(f"parser {parser_id} not found")
    runner = ParserRunner(
        parser_id, row["code_path"], workspace_id=workspace_id, session=session
    )
    return await runner.start()


async def parser_stop(parser_id: int) -> None:
    """/parser_stop — остановка запущенного парсера."""
    from ..parsers.runner import _RUNNING

    runner = _RUNNING.get(parser_id)
    if runner is None:
        return
    await runner.stop()


async def parser_status(parser_id: int) -> dict:
    """/parser_status — статус парсера (runtime + БД)."""
    from ..parsers.runner import _RUNNING
    from ..parsers.registry import get_parser

    runner = _RUNNING.get(parser_id)
    if runner is not None:
        return await runner.status()
    row = get_parser(parser_id)
    if not row:
        return {"parser_id": parser_id, "running": False, "status_db": None}
    return {
        "parser_id": parser_id,
        "running": False,
        "status_db": row.get("status"),
    }


# ── Реестр / dispatch ────────────────────────────────────────────────────────
TOOL_REGISTRY = {
    "/web_search": web_search,
    "/web_fetch": web_fetch,
    "/retrieve": retrieve_loopholes,
    "/export": refine_export,
    "/keywords": keywords,
    "/extract_loopholes": extract_loopholes,
    "/db_query": db_query,
    "/table_load": table_load,
    "/parser_create": parser_create,
    "/parser_start": parser_start,
    "/parser_stop": parser_stop,
    "/parser_status": parser_status,
}


# Маппинг «команда → имя функции в этом модуле». dispatch резолвит функцию
# динамически через sys.modules, чтобы monkeypatch в тестах работал.
_NAME_MAP = {
    "/web_search": "web_search",
    "/web_fetch": "web_fetch",
    "/retrieve": "retrieve_loopholes",
    "/export": "refine_export",
    "/keywords": "keywords",
    "/extract_loopholes": "extract_loopholes",
    "/db_query": "db_query",
    "/table_load": "table_load",
    "/parser_create": "parser_create",
    "/parser_start": "parser_start",
    "/parser_stop": "parser_stop",
    "/parser_status": "parser_status",
}


def dispatch(command: str, args: dict, *, session=None) -> Any:
    """Синхронный dispatch для sync-tools (web_search, retrieve, db_query, ...).

    Для async-tools (keywords, extract_loopholes, parser_*) возвращает
    coroutine — вызывающая сторона должна await'ить его (phases делает это
    через ``await`` после проверки ``inspect.iscoroutine``).
    """
    import sys as _sys

    mod = _sys.modules[__name__]
    attr = _NAME_MAP.get(command)
    if attr is None:
        return {"error": f"unknown command: {command}"}
    fn = getattr(mod, attr)
    a = args or {}
    try:
        if command == "/web_search":
            return fn(a.get("query", ""), max_results=a.get("max_results", 8))
        if command == "/web_fetch":
            return fn(a.get("url", ""))
        if command == "/retrieve":
            return fn(a.get("query", ""), bank_slugs=a.get("bank_slugs"), session=session)
        if command == "/export":
            return fn(a.get("records", []), format=a.get("format", "json"))
        if command == "/keywords":
            return fn(a.get("query", ""), existing=a.get("existing"))
        if command == "/extract_loopholes":
            return fn(a.get("text", ""))
        if command == "/db_query":
            return fn(
                bank_slugs=a.get("bank_slugs"),
                period_from=a.get("period_from"),
                period_to=a.get("period_to"),
                query_text=a.get("query_text"),
                only_loophole=a.get("only_loophole"),
                status=a.get("status"),
                limit=a.get("limit", 500),
                offset=a.get("offset", 0),
                session=session,
            )
        if command == "/table_load":
            return fn(
                bank_slugs=a.get("bank_slugs"),
                period_from=a.get("period_from"),
                period_to=a.get("period_to"),
                query_text=a.get("query_text"),
                only_loophole=a.get("only_loophole", True),
                status=a.get("status"),
                limit=a.get("limit", 200),
                session=session,
            )
        if command == "/parser_create":
            return fn(
                a.get("user_id", ""), a.get("workspace_id"), a.get("query", ""),
                llm=a.get("llm"), session=session,
            )
        if command == "/parser_start":
            return fn(a.get("parser_id"), workspace_id=a.get("workspace_id"), session=session)
        if command == "/parser_stop":
            return fn(a.get("parser_id"))
        if command == "/parser_status":
            return fn(a.get("parser_id"))
    except Exception as e:
        return {"error": str(e)}
    return {"error": "unhandled"}
