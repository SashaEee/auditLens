from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import text

from .. import repository as repo
from ..adapters import fetch_decorator, search_decorator
from ..models import LoopholeRecord
from ..pii_mask import mask as pii_mask
from ...hashing import sha256_text

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent / "prompt"


def load_prompt(name: str) -> str:
    """Читает промпт из ``chat/prompt/<name>.md`` (UTF-8)."""
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


# ── READ-ONLY SQL guard ──────────────────────────────────────────────────────
_FORBIDDEN = re.compile(
    r"\b(DROP|INSERT|UPDATE|DELETE|ALTER|CREATE|TRUNCATE|GRANT|EXEC|UNION)\b",
    re.IGNORECASE,
)


def _is_read_only_select(sql: str) -> bool:
    """Проверяет, что SQL — только READ-ONLY SELECT."""
    if not sql or not sql.strip().lower().startswith("select"):
        return False
    if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql:
        return False
    if _FORBIDDEN.search(sql):
        return False
    return True


# ── web / export ───────────────────────────────────────────────────────────
def web_search(query: str, *, max_results: int = 8, _impl: Any = None) -> list[dict]:
    """Поиск в web: возвращает список {title, url, snippet, domain}."""
    return search_decorator.search(query, max_results=max_results, _impl=_impl)


def web_fetch(url: str, *, _impl: Any = None) -> dict | None:
    """Загрузка страницы: возвращает {url, final_url, title, excerpt, status, via}."""
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


# ── LLM helpers (extract_loopholes) ─────────────────────────────────────────
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


async def extract_loopholes(
    text: str,
    *,
    llm: Any = None,
) -> list[dict]:
    """Извлечение лазеек из текста через промпт 04_extract_loopholes.md.

    Перед отправкой в LLM текст маскируется через ``pii_mask.mask``.
    """
    from ...ai.llm_utils import _loose_json_loads

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


# ── db / table / export ─────────────────────────────────────────────────────
def db_query(sql: str, *, session: Any = None) -> dict:
    """READ-ONLY SQL-запрос к БД лазеек.

    Возвращает {"columns": [...], "rows": [...], "row_count": int}.
    При ошибке возвращает {"error": str}.
    """
    if not _is_read_only_select(sql):
        return {"error": "only SELECT queries are allowed"}

    normalized = " ".join(sql.split())
    if "LIMIT" not in normalized.upper():
        sql = f"{sql} LIMIT 500"

    try:
        with repo._session(session) as s:
            result = s.execute(text(sql))
            columns = list(result.keys())
            rows = result.mappings().all()
            return {
                "columns": columns,
                "rows": [list(row.values()) for row in rows],
                "row_count": len(rows),
            }
    except Exception as e:
        log.warning("[db_query] failed: %s", e)
        return {"error": str(e)}


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
    """Записи для таблицы фронта (only_loophole=True по умолчанию)."""
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


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:
        return ""


def save_loophole(
    title: str,
    url: str,
    snippet: str,
    *,
    bank_slug: str | None = None,
    keyword: str | None = None,
    raw_text: str | None = None,
    trust_score: float = 0.5,
    is_loophole: bool | None = None,
    session: Any = None,
) -> dict:
    """Сохраняет найденную лазейку в таблицу `loophole_record`.

    Дедуп по sha256 (url + snippet). Если запись уже существует — возвращает
    существующий record_id и `is_new=False`.
    """
    sha = sha256_text(url + "|" + snippet)
    rec = LoopholeRecord(
        sha256=sha,
        title=title,
        url=url,
        snippet=snippet,
        domain=_domain_of(url),
        trust_score=trust_score,
        bank_slug=bank_slug,
        keyword=keyword,
        raw_text=raw_text or snippet,
        is_loophole=is_loophole,
        status="new",
    )
    try:
        is_new = not repo.exists_sha256(sha, session=session)
        record_id = repo.insert_record(rec, session=session)
        if record_id is None:
            record_id = repo.get_record_id_by_sha256(sha, session=session)
        return {
            "record_id": record_id,
            "sha256": sha,
            "is_new": is_new,
        }
    except Exception as e:
        log.warning("[save_loophole] failed: %s", e)
        return {"error": str(e), "sha256": sha, "record_id": None, "is_new": False}


def refine_export(records: list[dict], *, format: str = "json") -> dict:
    """Подготовка записей к экспорту."""
    return {"format": format, "count": len(records), "records": records}


# ── Nanobot Tool wrappers ───────────────────────────────────────────────────
# nanobot ожидает подклассы Tool с декоратором @tool_parameters.
# Ниже — обёртки над функциями выше, чтобы регистрировать их в harness.

def _tool_name(name: str) -> str:
    """Префикс audit_ предотвращает коллизии с встроенными tools nanobot."""
    return f"audit_{name}"


def _tool_result(value: Any) -> str:
    """Сериализует результат tool в JSON-строку.

    OpenAI tool result ``content`` должен быть строкой; nanobot иначе
    сохраняет list/dict в сессии как мультимодальный блок без поля ``type``,
    что приводит к ``Missing 'type' field in multimodal part`` при следующем
    запросе. ``None`` сериализуем как ``"null"``.
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


try:
    from nanobot.agent.tools.base import Tool, tool_parameters

    @tool_parameters({
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "max_results": {"type": "integer", "default": 8},
        },
        "required": ["query"],
    })
    class AuditWebSearchTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("web_search")

        @property
        def description(self) -> str:
            return (
                "Поиск в интернете по запросу пользователя. "
                "Возвращает список результатов с title, url, snippet, domain."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, query: str, max_results: int = 8) -> str:
            return _tool_result(web_search(query, max_results=max_results))

    @tool_parameters({
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL страницы для загрузки"},
        },
        "required": ["url"],
    })
    class AuditWebFetchTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("web_fetch")

        @property
        def description(self) -> str:
            return (
                "Загружает страницу по URL и возвращает title, excerpt, status. "
                "Используй после web_search, чтобы получить детали."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, url: str) -> str:
            return _tool_result(web_fetch(url))

    @tool_parameters({
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Текст для анализа"},
        },
        "required": ["text"],
    })
    class AuditExtractLoopholesTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("extract_loopholes")

        @property
        def description(self) -> str:
            return (
                "Анализирует текст (например, загруженной страницы) и извлекает "
                "потенциальные лазейки. Перед LLM маскирует ПДн."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, text: str) -> str:
            return _tool_result(await extract_loopholes(text))

    @tool_parameters({
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "READ-ONLY SQL SELECT запрос"},
        },
        "required": ["sql"],
    })
    class AuditDbQueryTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("db_query")

        @property
        def description(self) -> str:
            return (
                "Выполняет READ-ONLY SQL-запрос к базе данных лазеек. "
                "Только SELECT; любые модифицирующие команды запрещены."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, sql: str) -> str:
            return _tool_result(db_query(sql))

    @tool_parameters({
        "type": "object",
        "properties": {
            "bank_slugs": {"type": "array", "items": {"type": "string"}},
            "period_from": {"type": "string"},
            "period_to": {"type": "string"},
            "query_text": {"type": "string"},
            "only_loophole": {"type": "boolean", "default": True},
            "status": {"type": "string"},
            "limit": {"type": "integer", "default": 200},
        },
        "required": [],
    })
    class AuditTableLoadTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("table_load")

        @property
        def description(self) -> str:
            return (
                "Загружает записи из базы лазеек для отображения в таблице. "
                "READ-ONLY: не изменяет данные."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(
            self,
            bank_slugs: list[str] | None = None,
            period_from: Any = None,
            period_to: Any = None,
            query_text: str | None = None,
            only_loophole: bool = True,
            status: str | None = None,
            limit: int = 200,
        ) -> str:
            try:
                return _tool_result(
                    table_load(
                        bank_slugs=bank_slugs,
                        period_from=period_from,
                        period_to=period_to,
                        query_text=query_text,
                        only_loophole=only_loophole,
                        status=status,
                        limit=limit,
                    )
                )
            except Exception as e:
                log.warning("[table_load] failed: %s", e)
                return _tool_result({"error": str(e)})

    @tool_parameters({
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Заголовок лазейки"},
            "url": {"type": "string", "description": "URL источника"},
            "snippet": {"type": "string", "description": "Краткое описание/цитата"},
            "bank_slug": {"type": "string", "description": "slug банка (опционально)"},
            "keyword": {"type": "string", "description": "ключевое слово (опционально)"},
            "raw_text": {"type": "string", "description": "полный текст (опционально)"},
            "trust_score": {"type": "number", "default": 0.5},
            "is_loophole": {"type": "boolean", "description": "предварительный вердикт (опционально)"},
        },
        "required": ["title", "url", "snippet"],
    })
    class AuditSaveLoopholeTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("save_loophole")

        @property
        def description(self) -> str:
            return (
                "Сохраняет найденную лазейку/проблему в базу данных loophole_record. "
                "Используй после web_search/web_fetch и extract_loopholes, "
                "когда нужно запомнить результат для таблицы UI. "
                "Дедуп по sha256; при повторе возвращает существующий record_id."
            )

        @property
        def read_only(self) -> bool:
            return False

        async def execute(
            self,
            title: str,
            url: str,
            snippet: str,
            bank_slug: str | None = None,
            keyword: str | None = None,
            raw_text: str | None = None,
            trust_score: float = 0.5,
            is_loophole: bool | None = None,
        ) -> str:
            return _tool_result(
                save_loophole(
                    title=title,
                    url=url,
                    snippet=snippet,
                    bank_slug=bank_slug,
                    keyword=keyword,
                    raw_text=raw_text,
                    trust_score=trust_score,
                    is_loophole=is_loophole,
                )
            )

    @tool_parameters({
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object"}},
            "format": {"type": "string", "default": "json"},
        },
        "required": ["records"],
    })
    class AuditExportTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("export")

        @property
        def description(self) -> str:
            return "Форматирует список записей для экспорта."

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, records: list[dict], format: str = "json") -> str:
            return _tool_result(refine_export(records, format=format))

    NANOBOT_TOOLS: tuple[type[Tool], ...] = (
        AuditWebSearchTool,
        AuditWebFetchTool,
        AuditExtractLoopholesTool,
        AuditSaveLoopholeTool,
        AuditDbQueryTool,
        AuditTableLoadTool,
        AuditExportTool,
    )
except Exception as _exc:  # pragma: no cover - nanobot optional
    NANOBOT_TOOLS: tuple[type, ...] = ()  # type: ignore[no-redef]
    log.debug("nanobot tools not available: %s", _exc)
