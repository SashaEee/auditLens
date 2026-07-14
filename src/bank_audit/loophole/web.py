"""FastAPI APIRouter модуля loophole: эндпоинты + SSE-чат.

Префикс /api/loophole (монтируется в web/app.py). Авторизация внешняя —
user_id из заголовка X-User-Id (fallback "anonymous").
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import date
from typing import Annotated, Any, Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .. import db
from . import repository as repo
from . import keywords as kw_mod
from . import workspace as ws_mod
from . import logging_audit
from . import refine as refine_mod
from . import collector as collector_mod
from .chat import graph as chat_graph
from .chat.state import ChatState
from .models import SearchQuery, ExportRequest, WorkspaceCreate, ChatMessage

log = logging.getLogger(__name__)

router = APIRouter()


# ── Dependencies ────────────────────────────────────────────────────────────
def get_session():
    """Yield SQLAlchemy-сессию. Переопределяется в тестах через
    app.dependency_overrides[get_session]."""
    with db.session() as s:
        yield s


def get_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    return x_user_id or "anonymous"


# ── Эндпоинты ───────────────────────────────────────────────────────────────
@router.post("/search")
def search(
    q: SearchQuery,
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    records = repo.search_relevant(
        q.query_text,
        bank_slugs=q.bank_slugs or None,
        period_from=q.period_from,
        period_to=q.period_to,
        only_loophole=True,
        session=session,
    )
    logging_audit.log_action(
        user_id, "search",
        detail={"query": q.query_text, "banks": q.bank_slugs},
        session=session,
    )
    return {"records": records, "count": len(records)}


@router.get("/keywords")
def get_keywords(session=Depends(get_session)):
    return {"keywords": repo.list_keywords(session=session)}


@router.get("/records")
def list_records(
    bank_slugs: str | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
    q: str | None = None,
    only_loophole: bool | None = None,
    status: str | None = None,
    limit: int = 500,
    offset: int = 0,
    session=Depends(get_session),
):
    """Список лазеек из БД для таблицы в основной области UI.

    bank_slugs передаётся строкой через запятую (query-param friendly):
    /records?bank_slugs=sberbank,vtb
    """
    slugs = (
        [s.strip() for s in bank_slugs.split(",") if s.strip()]
        if bank_slugs else None
    )
    records = repo.list_records(
        bank_slugs=slugs,
        period_from=period_from,
        period_to=period_to,
        query_text=q,
        only_loophole=only_loophole,
        status=status,
        limit=limit,
        offset=offset,
        session=session,
    )
    return {"records": records, "count": len(records)}


@router.get("/banks")
def list_banks(session=Depends(get_session)):
    """Уникальные bank_slug из loophole_record — для фильтра таблицы."""
    return {"banks": repo.list_bank_slugs(session=session)}


@router.get("/workspaces")
def list_workspaces(user_id: str = Depends(get_user_id), session=Depends(get_session)):
    return {"workspaces": ws_mod.list_for_user(user_id, session=session)}


@router.post("/workspace")
def create_workspace(
    body: WorkspaceCreate,
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    wid = ws_mod.create(user_id, name=body.name, session=session)
    logging_audit.log_action(
        user_id, "workspace_create", workspace_id=wid, session=session
    )
    return {"workspace_id": wid}


@router.get("/history/{workspace_id}")
def history(workspace_id: int, session=Depends(get_session)):
    return {"messages": ws_mod.history(workspace_id, session=session)}


class ChatRequest(BaseModel):
    workspace_id: int
    message: str
    history: list[dict] = []


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    """SSE-чат: стримит token/tool_call/tool_result/record события."""
    state: ChatState = {
        "query": body.message,
        "messages": body.history,
        "workspace_id": body.workspace_id,
        "user_id": user_id,
        "session": session,
    }
    # Сохраняем сообщение пользователя.
    repo.add_chat_message(body.workspace_id, "user", body.message, session=session)
    logging_audit.log_action(
        user_id, "chat", workspace_id=body.workspace_id,
        detail={"message": body.message[:200]}, session=session,
    )

    async def event_generator():
        import json as _json
        async for ev in chat_graph.stream_chat(state, session=session):
            yield {"event": ev["event"], "data": _json.dumps(ev["data"], ensure_ascii=False, default=str)}
        # Сохраняем ответ (если есть).
        try:
            if state.get("answer"):
                repo.add_chat_message(
                    body.workspace_id, "assistant", state["answer"], session=session
                )
        except Exception:
            pass

    return EventSourceResponse(event_generator())


@router.post("/export")
def export(
    body: ExportRequest,
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    records = []
    if body.records:
        for rid in body.records:
            r = repo.get_record(rid, session=session)
            if r:
                records.append(r)
    logging_audit.log_action(
        user_id, "export", detail={"format": body.format, "count": len(records)},
        session=session,
    )
    if body.format == "json":
        return JSONResponse(records)
    if body.format == "csv":
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow([
            "record_id", "title", "url", "domain", "bank_slug", "keyword",
            "trust_score", "is_loophole", "verdict_confidence",
            "verdict_reason", "verdict_model", "status",
            "collected_at", "classified_at",
        ])
        for r in records:
            writer.writerow([
                r.get("record_id"), r.get("title"), r.get("url"),
                r.get("domain"), r.get("bank_slug"), r.get("keyword"),
                r.get("trust_score"), r.get("is_loophole"),
                r.get("verdict_confidence"), r.get("verdict_reason"),
                r.get("verdict_model"), r.get("status"),
                r.get("collected_at"), r.get("classified_at"),
            ])
        # BOM для корректного открытия в Excel (Windows).
        return Response(
            content="\ufeff" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=loopholes.csv"},
        )
    # pdf — через pdf_export (Playwright); заглушка для тестов.
    return JSONResponse({"error": "pdf export requires Playwright"}, status_code=501)


class FilteredExportRequest(BaseModel):
    bank_slugs: list[str] = Field(default_factory=list)
    period_from: date | None = None
    period_to: date | None = None
    query_text: str = ""
    only_loophole: bool | None = None
    status: str | None = None


@router.post("/export/csv")
def export_csv_filtered(
    body: FilteredExportRequest,
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    """Выгрузка CSV по текущим фильтрам таблицы (без передачи ids).
    Берёт все подходящие записи (limit 10000) и формирует CSV с BOM."""
    records = repo.list_records(
        bank_slugs=body.bank_slugs or None,
        period_from=body.period_from,
        period_to=body.period_to,
        query_text=body.query_text or None,
        only_loophole=body.only_loophole,
        status=body.status,
        limit=10000,
        session=session,
    )
    logging_audit.log_action(
        user_id, "export_csv", detail={"count": len(records)}, session=session,
    )
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow([
        "record_id", "title", "url", "domain", "bank_slug", "keyword",
        "trust_score", "is_loophole", "verdict_confidence",
        "verdict_reason", "verdict_model", "status",
        "collected_at", "classified_at",
    ])
    for r in records:
        writer.writerow([
            r.get("record_id"), r.get("title"), r.get("url"),
            r.get("domain"), r.get("bank_slug"), r.get("keyword"),
            r.get("trust_score"), r.get("is_loophole"),
            r.get("verdict_confidence"), r.get("verdict_reason"),
            r.get("verdict_model"), r.get("status"),
            r.get("collected_at"), r.get("classified_at"),
        ])
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=loopholes.csv"},
    )


@router.post("/refine")
async def refine(
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    added = await refine_mod.refine_keywords(session=session)
    logging_audit.log_action(
        user_id, "refine", detail={"added": added}, session=session
    )
    return {"added": added}


@router.post("/collect/run")
async def collect_run(
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    """Ручной запуск авто-сборщика (для админа)."""
    n = await collector_mod.collect_once(session=session)
    logging_audit.log_action(
        user_id, "collect", detail={"new_records": n}, session=session
    )
    return {"new_records": n}


# ── Clarify-воронка ─────────────────────────────────────────────────────────
class ClarifyRequest(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list)


class ClarifyAnswerRequest(BaseModel):
    question: str
    answers: list[dict] = Field(default_factory=list)


@router.post("/clarify")
async def clarify(
    body: ClarifyRequest,
    user_id: str = Depends(get_user_id),
):
    """Генерация уточняющих вопросов по запросу аудитора."""
    from .chat import clarify as clarify_mod

    result = await clarify_mod.generate_clarifications(
        body.question, history=body.history
    )
    logging_audit.log_action(
        user_id, "clarify",
        detail={"question": body.question[:200], "complete": result.get("complete")},
    )
    return result


@router.post("/clarify/answer")
async def clarify_answer(
    body: ClarifyAnswerRequest,
    user_id: str = Depends(get_user_id),
):
    """Сборка обогащённого запроса из исходного вопроса и ответов воронки."""
    from .chat import clarify as clarify_mod

    enriched = await clarify_mod.build_enriched_question(body.question, body.answers)
    logging_audit.log_action(
        user_id, "clarify_answer",
        detail={"question": body.question[:200], "enriched_len": len(enriched)},
    )
    return {"enriched_question": enriched}


# ── Пользовательские парсеры ────────────────────────────────────────────────
class ParserCreateRequest(BaseModel):
    workspace_id: int
    query: str


@router.get("/parsers")
def list_parsers(workspace_id: int, session=Depends(get_session)):
    """Список парсеров workspace с runtime-статусом."""
    from .parsers import registry as parser_registry

    return {"parsers": parser_registry.list_parsers(workspace_id, session=session)}


@router.post("/parsers")
async def create_parser(
    body: ParserCreateRequest,
    user_id: str = Depends(get_user_id),
    session=Depends(get_session),
):
    """Генерация нового парсера через LLM."""
    from .parsers import generator as parser_generator

    result = await parser_generator.generate_parser(
        user_id, body.workspace_id, body.query, session=session
    )
    logging_audit.log_action(
        user_id, "parser_create",
        workspace_id=body.workspace_id,
        detail={"parser_id": result.get("parser_id"), "query": body.query[:200]},
        session=session,
    )
    return result


@router.post("/parsers/{parser_id}/run")
async def run_parser(
    parser_id: int,
    session=Depends(get_session),
):
    """Запуск парсера как subprocess. Возвращает pid."""
    from .parsers.runner import ParserRunner

    row = repo.get_parser(parser_id, session=session)
    if row is None:
        raise HTTPException(status_code=404, detail="parser not found")
    code_path = row.get("code_path")
    if not code_path:
        raise HTTPException(status_code=400, detail="parser has no code_path")
    runner = ParserRunner(
        parser_id, code_path,
        workspace_id=row.get("workspace_id"),
        session=session,
    )
    pid = await runner.start()
    return {"parser_id": parser_id, "pid": pid}


@router.post("/parsers/{parser_id}/stop")
async def stop_parser(parser_id: int):
    """Останов запущенного парсера. 404 если не running."""
    from .parsers.runner import _RUNNING

    runner = _RUNNING.get(parser_id)
    if runner is None:
        raise HTTPException(status_code=404, detail="parser not running")
    await runner.stop()
    return {"parser_id": parser_id, "stopped": True}


@router.get("/parsers/{parser_id}/status")
async def parser_status(
    parser_id: int,
    session=Depends(get_session),
):
    """Статус парсера: runtime (если running) + запись из БД."""
    from .parsers.runner import _RUNNING
    from .parsers import registry as parser_registry

    runner = _RUNNING.get(parser_id)
    if runner is not None:
        runtime = await runner.status()
    else:
        runtime = None
    row = parser_registry.get_parser(parser_id, session=session)
    if row is None and runtime is None:
        raise HTTPException(status_code=404, detail="parser not found")
    return {"parser_id": parser_id, "runtime": runtime, "parser": row}


@router.delete("/parsers/{parser_id}")
def delete_parser(parser_id: int, session=Depends(get_session)):
    """Удаление парсера (код + запись БД). 404 если не найден/running."""
    from .parsers import registry as parser_registry

    deleted = parser_registry.delete_parser(parser_id, session=session)
    if not deleted:
        raise HTTPException(status_code=404, detail="parser not found or running")
    return {"deleted": True}


# ── Загрузка таблицы по фильтрам (для агента) ───────────────────────────────
class TableLoadRequest(BaseModel):
    bank_slugs: list[str] = Field(default_factory=list)
    period_from: date | None = None
    period_to: date | None = None
    query_text: str = ""
    only_loophole: bool | None = None
    status: str | None = None
    limit: int = 500
    offset: int = 0


@router.post("/table/load")
def table_load(body: TableLoadRequest, session=Depends(get_session)):
    """Применяет фильтры и возвращает records для таблицы UI."""
    records = repo.list_records(
        bank_slugs=body.bank_slugs or None,
        period_from=body.period_from,
        period_to=body.period_to,
        query_text=body.query_text or None,
        only_loophole=body.only_loophole,
        status=body.status,
        limit=body.limit,
        offset=body.offset,
        session=session,
    )
    return {"records": records, "count": len(records)}
