from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ...hashing import sha256_text
from .. import content_fetch
from .. import repository as repo
from ..adapters import fetch_decorator, search_decorator
from ..config import LoopholeSettings
from ..models import LoopholeRecord
from ..pii_mask import mask as pii_mask

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
_QUERY_SHAPE = re.compile(
    r"^\s*SELECT\s+(?P<columns>[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\s+FROM\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)(?P<tail>.*)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_QUERY_UNSAFE = re.compile(
    r"\b(JOIN|WITH|RETURNING|INTO|COPY|WINDOW|UNION|INTERSECT|EXCEPT|SELECT|FROM)\b",
    re.IGNORECASE,
)
_QUERY_LITERAL = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"")
_QUERY_WORDS = {
    "and", "asc", "between", "by", "desc", "false", "in", "is", "like", "limit",
    "not", "null", "offset", "or", "order", "true", "where",
}

_DB_QUERY_COLUMNS = {
    "loophole_record": {
        "record_id", "title", "url", "snippet", "domain", "trust_score", "fetched_at",
        "published_at", "collected_at", "bank_slug", "keyword", "is_loophole", "verdict_confidence",
        "verdict_reason", "verdict_model", "classified_at", "status",
    },
    "loophole_keyword": {
        "keyword_id", "keyword", "category", "source", "weight", "created_at", "is_active",
    },
    "loophole_workspace": {"workspace_id", "name", "created_at", "last_active_at"},
    "loophole_result": {
        "result_id", "workspace_id", "query_text", "period_from", "period_to", "bank_slugs",
        "created_at", "updated_at",
    },
    "loophole_chat_message": {
        "message_id", "workspace_id", "role", "content", "tool_name", "created_at",
    },
    "loophole_agent_task": {
        "task_id", "workspace_id", "query_text", "enriched_query", "phase", "status",
        "iterations", "created_at", "updated_at",
    },
}
_WORKSPACE_SCOPED_TABLES = {
    "loophole_workspace", "loophole_result", "loophole_chat_message", "loophole_agent_task",
}


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Доверенный server-side контекст одного запуска managed agent."""

    user_id: str | None
    workspace_id: int | None
    session: Any | None
    query: str = ""
    pending_records: list[dict] = field(default_factory=list)
    source_publication_dates: dict[str, str | None] = field(default_factory=dict)
    fetched_sources: dict[str, dict[str, Any]] = field(default_factory=dict)


_RU_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}
_RU_MONTH_PATTERN = "|".join(f"{stem}\\w*" for stem in _RU_MONTHS)
_QUERY_MONTH_WINDOW_RE = re.compile(
    rf"\bза\s+(?P<month>{_RU_MONTH_PATTERN})\s+(?P<year>(?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
_QUERY_LOWER_BOUND_RE = re.compile(
    rf"\b(?:не\s+)?(?:раньше|ранее|с)\s+(?P<month>{_RU_MONTH_PATTERN})\s+"
    rf"(?P<year>(?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)


def _publication_window(query: str) -> tuple[date, date | None] | None:
    """Возвращает строгий window даты первоисточника из понятного month/year scope."""
    text_query = str(query or "")
    match = _QUERY_MONTH_WINDOW_RE.search(text_query)
    exact = match is not None
    if match is None:
        match = _QUERY_LOWER_BOUND_RE.search(text_query)
    if match is None:
        return None
    month_word = match.group("month").lower()
    month = next(
        (number for stem, number in _RU_MONTHS.items() if month_word.startswith(stem)),
        None,
    )
    if month is None:
        return None
    year = int(match.group("year"))
    start = date(year, month, 1)
    if not exact:
        return start, None
    end_exclusive = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end_exclusive


def _source_publication_period_error(context: ToolContext | None, source_url: str) -> str | None:
    """Fail-closed проверка источника при заданном пользователем publication window."""
    if context is None:
        return None
    window = _publication_window(context.query)
    if window is None:
        return None
    raw_published_at = context.source_publication_dates.get(source_url)
    if not raw_published_at:
        return "source_publication_date_unverified"
    normalized = raw_published_at.removesuffix("Z") + (
        "+00:00" if raw_published_at.endswith("Z") else ""
    )
    try:
        published_at = datetime.fromisoformat(normalized)
    except ValueError:
        return "source_publication_date_unverified"
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        return "source_publication_date_unverified"
    start, end_exclusive = window
    published_date = published_at.date()
    if published_date < start or (end_exclusive is not None and published_date >= end_exclusive):
        return "source_outside_publication_period"
    return None


def _remember_source_publication_date(
    context: ToolContext | None,
    requested_url: str,
    result: dict | None,
) -> None:
    """Связывает canonical/original URL с timestamp, полученным при fetch."""
    if context is None or not result:
        return
    published_at = result.get("published_at")
    for source_url in (requested_url, result.get("url"), result.get("final_url")):
        if source_url:
            context.source_publication_dates[str(source_url)] = published_at
            if result.get("excerpt"):
                context.fetched_sources[str(source_url)] = {
                    "url": str(result.get("final_url") or result.get("url") or requested_url),
                    "title": str(result.get("title") or "") or None,
                    "extracted_text": str(result["excerpt"]),
                    "published_at": published_at,
                }


def _context_owns_workspace(context: ToolContext | None) -> bool:
    """Проверяет ownership workspace перед любым context-bound tool вызовом."""
    if context is None or not context.user_id:
        return False
    if not isinstance(context.workspace_id, int) or context.session is None:
        return False
    try:
        workspace = repo.get_workspace(context.workspace_id, session=context.session)
    except Exception:  # noqa: BLE001 — ошибка проверки = fail-closed
        log.warning("[tool_context] не удалось проверить ownership workspace")
        return False
    return bool(workspace and workspace.get("user_id") == context.user_id)


def _is_read_only_select(sql: str) -> bool:
    """Проверяет, что SQL — только READ-ONLY SELECT."""
    if not sql or not sql.strip().lower().startswith("select"):
        return False
    if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql:
        return False
    return not _FORBIDDEN.search(sql)


def _redact_tool_value(value: Any) -> Any:
    """Рекурсивно маскирует данные перед возвратом результата tool в LLM."""
    if isinstance(value, str):
        return repo.redact_audit_text(value, limit=10000)
    if isinstance(value, dict):
        return {key: _redact_tool_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_tool_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_tool_value(item) for item in value)
    return value


# ── web / export ───────────────────────────────────────────────────────────
def web_search(query: str, *, max_results: int = 12, _impl: Any = None) -> list[dict]:
    """Поиск в web: возвращает список {title, url, snippet, domain}."""
    return search_decorator.search(query, max_results=max_results, _impl=_impl)


def web_fetch(url: str, *, _impl: Any = None) -> dict | None:
    """Загрузка страницы с проверяемой датой публикации первоисточника."""
    # excerpt_len=4000 (вместо дефолтных 1000): механизм лазейки в длинном
    # форумном треде часто описан не в первых 1000 символов — даём extract больше.
    page = fetch_decorator.fetch_and_parse(url, excerpt_len=4000, _fetch_impl=_impl)
    if page is None:
        return None
    return _redact_tool_value({
        "url": page.url,
        "final_url": page.final_url,
        "status": page.status,
        "title": page.title,
        "excerpt": page.excerpt,
        "via": page.via,
        "published_at": getattr(page, "published_at", None),
    })


# ── LLM helpers (extract_loopholes) ─────────────────────────────────────────
def _default_llm() -> Any:
    """ChatOpenAI с теми же env, что и остальные модули loophole."""
    import os

    from langchain_openai import ChatOpenAI

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
    except Exception as e:  # noqa: BLE001 — граница LLM должна вернуть безопасный пустой список
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


def _queue_confirmed_findings(
    context: ToolContext | None,
    findings: list[dict],
    *,
    source_url: str,
    bank_slug: str | None,
    raw_text: str,
) -> None:
    """Передаёт подтверждённые находки серверному этапу сохранения.

    Инструмент остаётся read-only для модели: запись выполняется только после
    завершения managed-запуска в ``chat.graph`` с доверенной сессией.
    """
    if context is None or not source_url.startswith(("https://", "http://")):
        return
    source = context.fetched_sources.get(source_url)
    if source is None:
        return
    for finding in findings:
        if not finding.get("is_loophole"):
            continue
        title = str(finding.get("title") or "").strip()
        snippet = str(finding.get("evidence_quote") or finding.get("description") or "").strip()
        if not title or not snippet:
            continue
        context.pending_records.append({
            "title": title,
            "url": source["url"],
            "snippet": snippet,
            "bank_slug": bank_slug,
            "raw_text": source["extracted_text"],
            "source_title": source["title"],
            "published_at": source["published_at"],
            "description": str(finding.get("description") or ""),
            "category": str(finding.get("category") or "") or None,
            "severity": str(finding.get("severity") or "medium"),
            "is_loophole": True,
        })


# ── db / table / export ─────────────────────────────────────────────────────
def db_query(
    sql: str,
    *,
    session: Any = None,
    context: ToolContext | None = None,
) -> dict:
    """READ-ONLY SQL-запрос к БД лазеек.

    Возвращает {"columns": [...], "rows": [...], "row_count": int}.
    При ошибке возвращает {"error": str}.
    """
    if not _is_read_only_select(sql):
        return {"error": "only SELECT queries are allowed"}

    if context is None and isinstance(session, ToolContext):
        context = session
    if context is None:
        return {"error": "db_query_unauthorized"}
    if not context.user_id or not isinstance(context.workspace_id, int) or context.session is None:
        return {"error": "db_query_unauthorized"}

    match = _QUERY_SHAPE.match(sql)
    if match is None:
        return {"error": "db_query_not_allowlisted"}
    table = match.group("table").lower()
    columns = [column.strip().lower() for column in match.group("columns").split(",")]
    allowed_columns = _DB_QUERY_COLUMNS.get(table)
    if allowed_columns is None or any(column not in allowed_columns for column in columns):
        return {"error": "db_query_not_allowlisted"}

    tail = match.group("tail").strip()
    if _QUERY_UNSAFE.search(tail) or ";" in tail or "--" in tail or "/*" in tail:
        return {"error": "db_query_not_allowlisted"}
    limit_match = re.search(r"\bLIMIT\s+([+-]?\s*\d+)\b", tail, re.IGNORECASE)
    if re.search(r"\bLIMIT\b", tail, re.IGNORECASE) and limit_match is None:
        return {"error": "db_query_limit_exceeded"}
    if limit_match:
        raw_limit = limit_match.group(1).replace(" ", "")
        if raw_limit.startswith(("+", "-")) or int(raw_limit) > 500:
            return {"error": "db_query_limit_exceeded"}
    clean_tail = _QUERY_LITERAL.sub("", tail)
    identifiers = {
        word.lower() for word in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", clean_tail)
    }
    if identifiers - allowed_columns - _QUERY_WORDS:
        return {"error": "db_query_not_allowlisted"}
    if table in _WORKSPACE_SCOPED_TABLES and re.search(r"\bworkspace_id\b", tail, re.IGNORECASE):
        return {"error": "workspace_scope_denied"}
    if not _context_owns_workspace(context):
        return {"error": "workspace_unauthorized"}

    normalized = " ".join(sql.split())
    params = {}
    if table in _WORKSPACE_SCOPED_TABLES:
        anchor = re.search(r"\b(?:ORDER\s+BY|LIMIT|OFFSET)\b", normalized, re.IGNORECASE)
        head = normalized[:anchor.start()].rstrip() if anchor else normalized
        tail_after_head = normalized[anchor.start():] if anchor else ""
        separator = " AND " if re.search(r"\bWHERE\b", head, re.IGNORECASE) else " WHERE "
        normalized = f"{head}{separator}workspace_id = :_managed_workspace_id"
        if tail_after_head:
            normalized = f"{normalized} {tail_after_head}"
        params["_managed_workspace_id"] = context.workspace_id
    if not re.search(r"\bLIMIT\b", normalized, re.IGNORECASE):
        offset = re.search(r"\bOFFSET\b", normalized, re.IGNORECASE)
        if offset:
            normalized = (
                f"{normalized[:offset.start()].rstrip()} LIMIT 500 "
                f"{normalized[offset.start():].lstrip()}"
            )
        else:
            normalized = f"{normalized} LIMIT 500"

    try:
        with repo._session(context.session) as s:
            result = s.execute(text(normalized), params)
            columns = list(result.keys())
            rows = result.mappings().all()
            return _redact_tool_value({
                "columns": columns,
                "rows": [list(row.values()) for row in rows],
                "row_count": len(rows),
            })
    except Exception as e:  # noqa: BLE001 — ошибка БД превращается в безопасный результат tool
        log.warning("[db_query] failed: %s", e)
        return _redact_tool_value({"error": str(e)})


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
    if limit > 500:
        raise ValueError("table_load_limit_exceeded")
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
    except ValueError:
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
    settings: LoopholeSettings | None = None,
) -> dict:
    """Сохраняет найденную лазейку в таблицу `loophole_record`.

    Точка гарантии полного контента: если агент не передал raw_text (или он
    короче сниппета) — страница скачивается сервером через content_fetch.
    Запись сохраняется ВСЕГДА: при fetch_failed — со сниппетом и честным
    статусом. Дедуп по sha256 (url + snippet): повтор возвращает существующий
    record_id с is_new=False, контент существующей записи не перезаписывается
    (догрузка — через /records/backfill-content).
    """
    settings = settings or LoopholeSettings.load()
    if raw_text is None or len(raw_text) < len(snippet or ""):
        content = content_fetch.fetch_full_content(url, settings=settings)
        if content.text is None:
            # fetch_failed/empty — сохраняем со сниппетом (старое поведение),
            # статус оставляем честным для очереди backfill.
            content = content_fetch.FullContent(
                text=snippet, status=content.status,
                length=len(snippet or ""), truncated=False,
            )
    else:
        content = content_fetch.limit_content(
            raw_text, max_chars=settings.raw_text_max_chars
        )
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
        raw_text=content.text,
        content_status=content.status,
        raw_text_len=content.length,
        raw_text_truncated=content.truncated,
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
    except Exception as e:  # noqa: BLE001 — граница repository сохраняет совместимый результат
        log.warning("[save_loophole] failed: %s", e)
        return {"error": str(e), "sha256": sha, "record_id": None, "is_new": False}


def refine_export(records: list[dict], *, format: str = "json") -> dict:
    """Подготовка записей к экспорту."""
    return {"format": format, "count": len(records), "records": records}


# ── Heal-tools: диагностика и патч парсеров (использует healer) ─────────────
def _http_collector(**kwargs):
    """Фабрика HttpCollector (вынесена для моков в тестах)."""
    from ...collectors.http import HttpCollector

    return HttpCollector(**kwargs)


def fetch_target(url: str, *, timeout: float = 20.0) -> dict:
    """Самостоятельная загрузка источника для диагностики healer'ом.

    Возвращает {url, ok, status?, excerpt?} или {url, ok: False, error}.
    """
    c = _http_collector(timeout=timeout, delay_ms=0)
    try:
        status, content = c.fetch(url)
        return {
            "url": url,
            "ok": status < 400,
            "status": status,
            "excerpt": content[:4000].decode("utf-8", errors="replace"),
        }
    except Exception as e:  # noqa: BLE001 — network/tool boundary не должна бросать raw error
        return {"url": url, "ok": False, "error": str(e)}
    finally:
        c.close()


def patch_parser(parser_id: int, new_code: str, *, session=None) -> dict:
    """Валидирует (ast.parse) и атомарно заменяет файл кода парсера."""
    import ast

    try:
        ast.parse(new_code)
    except SyntaxError as e:
        return {"patched": False, "error": f"syntax error: {e}"}
    from .. import repository as repo

    row = repo.get_parser(parser_id, session=session)
    if not row or not row.get("code_path"):
        return {"patched": False, "error": "parser not found"}
    path = Path(row["code_path"])
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_code, encoding="utf-8")
    tmp.replace(path)
    return {"patched": True, "code_path": str(path)}


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
    value = _redact_tool_value(value)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, OverflowError):
        return str(value)


try:
    from nanobot.agent.tools.base import Tool, tool_parameters

    @tool_parameters({
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "max_results": {"type": "integer", "default": 12},
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

        async def execute(self, query: str, max_results: int = 12) -> str:
            return _tool_result(web_search(query, max_results=max_results))

    @tool_parameters({
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL страницы для загрузки"},
        },
        "required": ["url"],
    })
    class AuditWebFetchTool(Tool):
        requires_context = True

        def __init__(self, context: ToolContext | None = None):
            self._context = context

        @property
        def name(self) -> str:
            return _tool_name("web_fetch")

        @property
        def description(self) -> str:
            return (
                "Загружает страницу по URL и возвращает title, excerpt, status, published_at. "
                "Используй после web_search, чтобы получить детали и проверить период."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, url: str) -> str:
            result = web_fetch(url)
            _remember_source_publication_date(self._context, url, result)
            period_error = _source_publication_period_error(self._context, url)
            if period_error is not None:
                return _tool_result({
                    "url": url,
                    "published_at": result.get("published_at") if result else None,
                    "error": period_error,
                })
            return _tool_result(result)

    @tool_parameters({
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Текст для анализа"},
            "source_url": {"type": "string", "description": "URL анализируемого источника"},
            "bank_slug": {"type": "string", "description": "Slug банка (опционально)"},
        },
        "required": ["text", "source_url"],
    })
    class AuditExtractLoopholesTool(Tool):
        requires_context = True

        def __init__(self, context: ToolContext | None = None):
            self._context = context

        @property
        def name(self) -> str:
            return _tool_name("extract_loopholes")

        @property
        def description(self) -> str:
            return (
                "Анализирует текст (например, загруженной страницы) и извлекает "
                "потенциальные лазейки. Перед LLM маскирует ПДн. Передай URL "
                "того же источника в source_url."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(
            self,
            text: str,
            source_url: str,
            bank_slug: str | None = None,
        ) -> str:
            period_error = _source_publication_period_error(self._context, source_url)
            if period_error is not None:
                return _tool_result({"error": period_error})
            source = self._context.fetched_sources.get(source_url) if self._context else None
            if source is None:
                return _tool_result({"error": "source_not_fetched"})
            findings = await extract_loopholes(source["extracted_text"])
            _queue_confirmed_findings(
                self._context,
                findings,
                source_url=source_url,
                bank_slug=bank_slug,
                raw_text=text,
            )
            return _tool_result(findings)

    @tool_parameters({
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "READ-ONLY SQL SELECT запрос"},
        },
        "required": ["sql"],
    })
    class AuditDbQueryTool(Tool):
        requires_context = True

        def __init__(self, context: ToolContext | None = None):
            self._context = context

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
            return _tool_result(db_query(sql, context=self._context))

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
        requires_context = True

        def __init__(self, context: ToolContext | None = None):
            self._context = context

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
                context = self._context
                if (
                    context is None
                    or not context.user_id
                    or not isinstance(context.workspace_id, int)
                    or context.session is None
                ):
                    return _tool_result({"error": "table_load_unauthorized"})
                if not _context_owns_workspace(context):
                    return _tool_result({"error": "workspace_unauthorized"})
                window = _publication_window(context.query)
                if window is not None:
                    period_from, end_exclusive = window
                    if end_exclusive is not None:
                        period_to = end_exclusive - timedelta(days=1)
                return _tool_result(
                    table_load(
                        bank_slugs=bank_slugs,
                        period_from=period_from,
                        period_to=period_to,
                        query_text=query_text,
                        only_loophole=only_loophole,
                        status=status,
                        limit=limit,
                        session=context.session,
                    )
                )
            except Exception as e:  # noqa: BLE001 — table tool возвращает безопасный error result
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
                "Полный текст страницы сервер скачивает АВТОМАТИЧЕСКИ по url — "
                "передавать raw_text не нужно, достаточно title/url/snippet. "
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

    @tool_parameters({
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL источника для проверки"},
        },
        "required": ["url"],
    })
    class AuditFetchTargetTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("fetch_target")

        @property
        def description(self) -> str:
            return (
                "Самостоятельно загружает данные из источника парсера (HTTP). "
                "Возвращает {ok, status, excerpt} — используй, чтобы понять, "
                "доступен ли источник и как выглядит страница."
            )

        @property
        def read_only(self) -> bool:
            return True

        async def execute(self, url: str) -> str:
            return _tool_result(fetch_target(url))

    @tool_parameters({
        "type": "object",
        "properties": {
            "parser_id": {"type": "integer", "description": "ID парсера"},
            "new_code": {
                "type": "string",
                "description": "Полный исправленный Python-код парсера",
            },
        },
        "required": ["parser_id", "new_code"],
    })
    class AuditPatchParserTool(Tool):
        @property
        def name(self) -> str:
            return _tool_name("patch_parser")

        @property
        def description(self) -> str:
            return (
                "Заменяет код парсера исправленной версией (атомарно, с "
                "проверкой синтаксиса). Вызывай только после анализа причины "
                "сбоя и когда источник доступен."
            )

        @property
        def read_only(self) -> bool:
            return False

        async def execute(self, parser_id: int, new_code: str) -> str:
            return _tool_result(patch_parser(parser_id, new_code))

    NANOBOT_HEAL_TOOLS: tuple[type[Tool], ...] = (
        AuditFetchTargetTool,
        AuditPatchParserTool,
    )

    NANOBOT_TOOLS: tuple[type[Tool], ...] = (
        AuditWebSearchTool,
        AuditWebFetchTool,
        AuditExtractLoopholesTool,
        AuditSaveLoopholeTool,
        AuditDbQueryTool,
        AuditTableLoadTool,
        AuditExportTool,
    )
except Exception as _exc:  # noqa: BLE001 — nanobot является необязательной зависимостью
    NANOBOT_TOOLS: tuple[type, ...] = ()  # type: ignore[no-redef]
    NANOBOT_HEAL_TOOLS: tuple[type, ...] = ()  # type: ignore[no-redef]
    log.debug("nanobot tools not available: %s", _exc)
