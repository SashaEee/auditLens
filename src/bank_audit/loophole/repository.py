"""CRUD к loophole_* таблицам через db.session() и sqlalchemy.text().

Без ORM. Дедуп по sha256 — app-level (SELECT exists → skip), что универсально
работает и в Greenplum 6 (без UNIQUE-констрейнта), и в SQLite (тесты).
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from sqlalchemy import text

from .. import db
from . import db_schema as schema
from .models import LoopholeRecord

log = logging.getLogger(__name__)


@contextmanager
def _session(s=None) -> Iterator:
    """Использует переданную сессию или открывает новую через db.session()."""
    if s is not None:
        yield s
        return
    with db.session() as s:
        yield s


# ── keywords ────────────────────────────────────────────────────────────────
def add_keyword(
    keyword: str,
    *,
    category: str = "manual",
    source: str | None = None,
    weight: float = 1.0,
    is_active: bool = True,
    session=None,
) -> int | None:
    """Добавляет ключевое слово. Дедуп по keyword (app-level)."""
    with _session(session) as s:
        existing = s.execute(
            text(f"SELECT keyword_id FROM {schema.T_KEYWORD} WHERE keyword = :kw"),
            {"kw": keyword},
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_KEYWORD} (keyword, category, source, weight, is_active) "
                "VALUES (:kw, :cat, :src, :w, :act) RETURNING keyword_id"
            ),
            {"kw": keyword, "cat": category, "src": source, "w": weight, "act": is_active},
        ).scalar_one()
        return row


def list_keywords(*, only_active: bool = False, session=None) -> list[dict]:
    with _session(session) as s:
        sql = f"SELECT keyword_id, keyword, category, source, weight, is_active FROM {schema.T_KEYWORD}"
        if only_active:
            sql += " WHERE is_active = TRUE"
        sql += " ORDER BY keyword_id"
        return [dict(r) for r in s.execute(text(sql)).mappings().all()]


def set_keyword_active(keyword_id: int, is_active: bool, *, session=None) -> None:
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_KEYWORD} SET is_active = :act WHERE keyword_id = :id"),
            {"act": is_active, "id": keyword_id},
        )


# ── records ─────────────────────────────────────────────────────────────────
def exists_sha256(sha256: str, *, session=None) -> bool:
    with _session(session) as s:
        return s.execute(
            text(f"SELECT 1 FROM {schema.T_RECORD} WHERE sha256 = :sha LIMIT 1"),
            {"sha": sha256},
        ).scalar_one_or_none() is not None


def get_record_id_by_sha256(sha256: str, *, session=None) -> int | None:
    """Возвращает record_id по sha256, если запись существует."""
    with _session(session) as s:
        return s.execute(
            text(f"SELECT record_id FROM {schema.T_RECORD} WHERE sha256 = :sha LIMIT 1"),
            {"sha": sha256},
        ).scalar_one_or_none()


def insert_record(rec: LoopholeRecord, *, session=None) -> int | None:
    """Вставляет запись. Если sha256 уже есть — возвращает существующий record_id (дедуп)."""
    with _session(session) as s:
        existing = s.execute(
            text(f"SELECT record_id FROM {schema.T_RECORD} WHERE sha256 = :sha LIMIT 1"),
            {"sha": rec.sha256},
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_RECORD} "
                "(sha256, title, url, snippet, domain, trust_score, bank_slug, keyword, "
                "raw_text, status, is_loophole) "
                "VALUES (:sha, :title, :url, :snip, :dom, :trust, :bank, :kw, :raw, :status, :loop) "
                "RETURNING record_id"
            ),
            {
                "sha": rec.sha256, "title": rec.title, "url": rec.url,
                "snip": rec.snippet, "dom": rec.domain, "trust": rec.trust_score,
                "bank": rec.bank_slug, "kw": rec.keyword, "raw": rec.raw_text,
                "status": rec.status, "loop": rec.is_loophole,
            },
        ).scalar_one()
        return row


def update_verdict(
    record_id: int,
    *,
    is_loophole: bool,
    confidence: float,
    reason: str,
    model: str,
    session=None,
) -> None:
    with _session(session) as s:
        s.execute(
            text(
                f"UPDATE {schema.T_RECORD} SET is_loophole = :is_l, "
                "verdict_confidence = :conf, verdict_reason = :reason, "
                "verdict_model = :model, classified_at = CURRENT_TIMESTAMP, status = 'classified' "
                "WHERE record_id = :id"
            ),
            {"is_l": is_loophole, "conf": confidence, "reason": reason,
             "model": model, "id": record_id},
        )


def get_record(record_id: int, *, session=None) -> dict | None:
    with _session(session) as s:
        row = s.execute(
            text(f"SELECT * FROM {schema.T_RECORD} WHERE record_id = :id"),
            {"id": record_id},
        ).mappings().first()
        return dict(row) if row else None


def list_records(
    *,
    bank_slugs: list[str] | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
    query_text: str | None = None,
    only_loophole: bool | None = None,
    status: str | None = None,
    limit: int = 500,
    offset: int = 0,
    session=None,
) -> list[dict]:
    """Список записей loophole_record с фильтрами для таблицы в UI.

    Возвращает поля, нужные таблице + CSV-экспорту. Без only_loophole по
    умолчанию — показывает все записи (и лазейки, и не-лазейки), чтобы
    пользователь мог сам отфильтровать по вердикту.
    """
    with _session(session) as s:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if bank_slugs:
            placeholders = ", ".join(f":b{i}" for i in range(len(bank_slugs)))
            clauses.append(f"bank_slug IN ({placeholders})")
            for i, b in enumerate(bank_slugs):
                params[f"b{i}"] = b
        if period_from:
            clauses.append("collected_at >= :pf")
            params["pf"] = period_from
        if period_to:
            clauses.append("collected_at <= :pt")
            params["pt"] = period_to
        if only_loophole is True:
            clauses.append("is_loophole = TRUE")
        elif only_loophole is False:
            clauses.append("is_loophole = FALSE")
        if status:
            clauses.append("status = :st")
            params["st"] = status
        if query_text:
            clauses.append(
                "(LOWER(COALESCE(title,'')) LIKE :q "
                "OR LOWER(COALESCE(snippet,'')) LIKE :q "
                "OR LOWER(COALESCE(raw_text,'')) LIKE :q)"
            )
            params["q"] = f"%{query_text.lower()}%"
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT record_id, title, url, snippet, domain, trust_score, "
            "bank_slug, keyword, is_loophole, verdict_confidence, "
            "verdict_reason, verdict_model, status, "
            "collected_at, classified_at "
            f"FROM {schema.T_RECORD}{where} "
            "ORDER BY COALESCE(verdict_confidence, 0) DESC, collected_at DESC "
            "LIMIT :limit OFFSET :offset"
        )
        return [dict(r) for r in s.execute(text(sql), params).mappings().all()]


def list_bank_slugs(*, session=None) -> list[str]:
    """Список уникальных bank_slug из loophole_record — для фильтра в UI."""
    with _session(session) as s:
        rows = s.execute(
            text(
                f"SELECT DISTINCT bank_slug FROM {schema.T_RECORD} "
                "WHERE bank_slug IS NOT NULL ORDER BY bank_slug"
            )
        ).scalars().all()
        return list(rows)


def search_relevant(
    query_text: str,
    *,
    bank_slugs: list[str] | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
    only_loophole: bool = True,
    limit: int = 50,
    session=None,
) -> list[dict]:
    """Полнотекстовый LIKE-поиск по loophole_record. Возвращает top-N записей."""
    with _session(session) as s:
        clauses = []
        params: dict[str, Any] = {"limit": limit}
        if only_loophole:
            clauses.append("is_loophole = TRUE")
        if bank_slugs:
            placeholders = ", ".join(f":b{i}" for i in range(len(bank_slugs)))
            clauses.append(f"bank_slug IN ({placeholders})")
            for i, b in enumerate(bank_slugs):
                params[f"b{i}"] = b
        if period_from:
            clauses.append("collected_at >= :pf")
            params["pf"] = period_from
        if period_to:
            clauses.append("collected_at <= :pt")
            params["pt"] = period_to
        # Текстовый поиск по title/snippet/raw_text (кросс-БД: LOWER LIKE).
        if query_text:
            clauses.append(
                "(LOWER(COALESCE(title,'')) LIKE :q "
                "OR LOWER(COALESCE(snippet,'')) LIKE :q "
                "OR LOWER(COALESCE(raw_text,'')) LIKE :q)"
            )
            params["q"] = f"%{query_text.lower()}%"
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT record_id, title, url, snippet, domain, trust_score, "
            "bank_slug, is_loophole, verdict_confidence, verdict_reason "
            f"FROM {schema.T_RECORD}{where} "
            "ORDER BY verdict_confidence DESC, collected_at DESC LIMIT :limit"
        )
        return [dict(r) for r in s.execute(text(sql), params).mappings().all()]


# ── workspace ───────────────────────────────────────────────────────────────
def create_workspace(user_id: str, name: str | None = None, *, session=None) -> int:
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_WORKSPACE} (user_id, name, last_active_at) "
                "VALUES (:u, :n, CURRENT_TIMESTAMP) RETURNING workspace_id"
            ),
            {"u": user_id, "n": name},
        ).scalar_one()
        return row


def list_workspaces(user_id: str, *, session=None) -> list[dict]:
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT workspace_id, user_id, name, created_at, last_active_at "
                    f"FROM {schema.T_WORKSPACE} WHERE user_id = :u ORDER BY workspace_id"
                ),
                {"u": user_id},
            ).mappings().all()
        ]


def touch_workspace(workspace_id: int, *, session=None) -> None:
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_WORKSPACE} SET last_active_at = CURRENT_TIMESTAMP WHERE workspace_id = :id"),
            {"id": workspace_id},
        )


# ── chat messages ───────────────────────────────────────────────────────────
def add_chat_message(
    workspace_id: int,
    role: str,
    content: str,
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    session=None,
) -> int:
    with _session(session) as s:
        args_json = json.dumps(tool_args, ensure_ascii=False) if tool_args else None
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_CHAT_MESSAGE} "
                "(workspace_id, role, content, tool_name, tool_args) "
                "VALUES (:ws, :role, :content, :tn, :ta) RETURNING message_id"
            ),
            {"ws": workspace_id, "role": role, "content": content,
             "tn": tool_name, "ta": args_json},
        ).scalar_one()
        return row


def list_chat_history(workspace_id: int, *, limit: int = 200, session=None) -> list[dict]:
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT message_id, workspace_id, role, content, tool_name, tool_args, "
                    f"created_at FROM {schema.T_CHAT_MESSAGE} "
                    "WHERE workspace_id = :ws ORDER BY created_at LIMIT :lim"
                ),
                {"ws": workspace_id, "lim": limit},
            ).mappings().all()
        ]


# ── results ─────────────────────────────────────────────────────────────────
def save_result(
    workspace_id: int,
    query_text: str,
    *,
    period_from: date | None = None,
    period_to: date | None = None,
    bank_slugs: list[str] | None = None,
    records: list[dict] | None = None,
    session=None,
) -> int:
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_RESULT} "
                "(workspace_id, query_text, period_from, period_to, bank_slugs, records) "
                "VALUES (:ws, :q, :pf, :pt, :bs, :rec) RETURNING result_id"
            ),
            {
                "ws": workspace_id, "q": query_text, "pf": period_from, "pt": period_to,
                "bs": json.dumps(bank_slugs or [], ensure_ascii=False),
                "rec": json.dumps(records or [], ensure_ascii=False),
            },
        ).scalar_one()
        return row


# ── action log ──────────────────────────────────────────────────────────────
def log_action(
    user_id: str,
    action: str,
    *,
    workspace_id: int | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    session=None,
) -> int:
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_ACTION_LOG} "
                "(user_id, workspace_id, action, detail, ip) "
                "VALUES (:u, :ws, :act, :det, :ip) RETURNING log_id"
            ),
            {
                "u": user_id, "ws": workspace_id, "act": action,
                "det": json.dumps(detail or {}, ensure_ascii=False), "ip": ip,
            },
        ).scalar_one()
        return row


def list_actions(user_id: str, *, limit: int = 100, session=None) -> list[dict]:
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT log_id, user_id, workspace_id, action, detail, ip, created_at "
                    f"FROM {schema.T_ACTION_LOG} WHERE user_id = :u "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"u": user_id, "lim": limit},
            ).mappings().all()
        ]


# ── agent tasks ─────────────────────────────────────────────────────────────
def save_task(
    workspace_id: int,
    query_text: str,
    *,
    enriched_query: str | None = None,
    phase: str = "clarify",
    status: str = "running",
    subtasks: list | None = None,
    clarify_questions: list | None = None,
    session=None,
) -> int:
    """Создаёт агентную задачу, возвращает task_id."""
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_AGENT_TASK} "
                "(workspace_id, query_text, enriched_query, phase, status, "
                "subtasks, clarify_questions, created_at, updated_at) "
                "VALUES (:ws, :q, :eq, :ph, :st, :st_sub, :cq, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING task_id"
            ),
            {
                "ws": workspace_id, "q": query_text, "eq": enriched_query,
                "ph": phase, "st": status,
                "st_sub": json.dumps(subtasks, ensure_ascii=False) if subtasks is not None else None,
                "cq": json.dumps(clarify_questions, ensure_ascii=False)
                if clarify_questions is not None
                else None,
            },
        ).scalar_one()
        return row


def update_task(
    task_id: int,
    *,
    phase: str | None = None,
    status: str | None = None,
    subtasks: list | None = None,
    subtask_results: list | None = None,
    iterations: int | None = None,
    clarify_answers: list | None = None,
    enriched_query: str | None = None,
    session=None,
) -> None:
    """Точечно обновляет поля агентной задачи (только переданные)."""
    sets: list[str] = []
    params: dict[str, Any] = {"id": task_id}
    if phase is not None:
        sets.append("phase = :ph")
        params["ph"] = phase
    if status is not None:
        sets.append("status = :st")
        params["st"] = status
    if subtasks is not None:
        sets.append("subtasks = :st_sub")
        params["st_sub"] = json.dumps(subtasks, ensure_ascii=False)
    if subtask_results is not None:
        sets.append("subtask_results = :sr")
        params["sr"] = json.dumps(subtask_results, ensure_ascii=False)
    if iterations is not None:
        sets.append("iterations = :it")
        params["it"] = iterations
    if clarify_answers is not None:
        sets.append("clarify_answers = :ca")
        params["ca"] = json.dumps(clarify_answers, ensure_ascii=False)
    if enriched_query is not None:
        sets.append("enriched_query = :eq")
        params["eq"] = enriched_query
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_AGENT_TASK} SET {', '.join(sets)} WHERE task_id = :id"),
            params,
        )


def get_task(task_id: int, *, session=None) -> dict | None:
    with _session(session) as s:
        row = s.execute(
            text(f"SELECT * FROM {schema.T_AGENT_TASK} WHERE task_id = :id"),
            {"id": task_id},
        ).mappings().first()
        return dict(row) if row else None


# ── knowledge base: examples ────────────────────────────────────────────────
def _embedding_to_pgvector(embedding: list[float] | None) -> str | None:
    """Сериализует list[float] в строковое представление pgvector: '[0.1,0.2,...]'."""
    if embedding is None:
        return None
    return "[" + ",".join(f"{float(x):.8f}" for x in embedding) + "]"


def save_kb_example(
    title: str,
    description: str,
    *,
    category: str | None = None,
    embedding: list[float] | None = None,
    session=None,
) -> int:
    """Сохраняет пример в KB. embedding — list[float], сериализуется для pgvector."""
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_KB_EXAMPLE} "
                "(title, description, category, embedding) "
                "VALUES (:title, :desc, :cat, :emb::vector) RETURNING example_id"
            ),
            {
                "title": title, "desc": description, "cat": category,
                "emb": _embedding_to_pgvector(embedding),
            },
        ).scalar_one()
        return row


def search_kb_similar(
    embedding: list[float],
    *,
    k: int = 5,
    session=None,
) -> list[dict]:
    """KNN-поиск по pgvector (cosine distance `<=>`).

    Если pgvector недоступен (тип vector не зарегистрирован / расширение не
    установлено) — graceful fallback: лог-предупреждение и пустой список.
    Альтернативный LIKE-поиск невозможен без текстового запроса, поэтому
    возвращаем [] — вызывающая сторона должна комбинировать с текстовым поиском.
    """
    emb_str = _embedding_to_pgvector(embedding)
    with _session(session) as s:
        try:
            rows = s.execute(
                text(
                    f"SELECT example_id, title, description, category, "
                    f"(embedding <=> :emb::vector) AS distance "
                    f"FROM {schema.T_KB_EXAMPLE} "
                    "WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> :emb::vector LIMIT :k"
                ),
                {"emb": emb_str, "k": k},
            ).mappings().all()
            return [dict(r) for r in rows]
        except Exception as exc:
            # pgvector недоступен (тип не зарегистрирован, расширение не установлено,
            # или БД без поддержки vector). Graceful fallback — пустой список.
            log.warning("pgvector недоступен для search_kb_similar: %s", exc)
            return []


# ── parsers ─────────────────────────────────────────────────────────────────
def save_parser(
    workspace_id: int,
    name: str,
    code_path: str,
    *,
    config: dict | None = None,
    session=None,
) -> int:
    """Создаёт запись парсера пользовательского кода, возвращает parser_id."""
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_PARSER} "
                "(workspace_id, name, code_path, status, config) "
                "VALUES (:ws, :name, :path, 'created', :cfg) RETURNING parser_id"
            ),
            {
                "ws": workspace_id, "name": name, "path": code_path,
                "cfg": json.dumps(config, ensure_ascii=False) if config is not None else None,
            },
        ).scalar_one()
        return row


def update_parser_status(parser_id: int, status: str, *, session=None) -> None:
    """Обновляет статус парсера и last_run_at (если статус терминальный)."""
    with _session(session) as s:
        s.execute(
            text(
                f"UPDATE {schema.T_PARSER} SET status = :st, "
                "last_run_at = CURRENT_TIMESTAMP WHERE parser_id = :id"
            ),
            {"st": status, "id": parser_id},
        )


def list_parsers(workspace_id: int, *, session=None) -> list[dict]:
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT parser_id, workspace_id, name, code_path, status, "
                    f"config, created_at, last_run_at FROM {schema.T_PARSER} "
                    "WHERE workspace_id = :ws ORDER BY parser_id"
                ),
                {"ws": workspace_id},
            ).mappings().all()
        ]


def get_parser(parser_id: int, *, session=None) -> dict | None:
    with _session(session) as s:
        row = s.execute(
            text(
                f"SELECT parser_id, workspace_id, name, code_path, status, "
                f"config, created_at, last_run_at FROM {schema.T_PARSER} "
                "WHERE parser_id = :id"
            ),
            {"id": parser_id},
        ).mappings().first()
        return dict(row) if row else None
