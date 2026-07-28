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


def exists_text_sha256(sha: str, *, session=None) -> bool:
    with _session(session) as s:
        return s.execute(
            text(f"SELECT 1 FROM {schema.T_RECORD} WHERE text_sha256 = :s LIMIT 1"),
            {"s": sha},
        ).scalar_one_or_none() is not None


def exists_url(url: str, *, session=None) -> bool:
    with _session(session) as s:
        return s.execute(
            text(f"SELECT 1 FROM {schema.T_RECORD} WHERE url = :u LIMIT 1"),
            {"u": url},
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
                "raw_text, status, is_loophole, parser_id, text_sha256, "
                "content_status, raw_text_len, raw_text_truncated) "
                "VALUES (:sha, :title, :url, :snip, :dom, :trust, :bank, :kw, :raw, "
                ":status, :loop, :pid, :tsha, :cs, :rlen, :rtrunc) "
                "RETURNING record_id"
            ),
            {
                "sha": rec.sha256, "title": rec.title, "url": rec.url,
                "snip": rec.snippet, "dom": rec.domain, "trust": rec.trust_score,
                "bank": rec.bank_slug, "kw": rec.keyword, "raw": rec.raw_text,
                "status": rec.status, "loop": rec.is_loophole,
                "pid": rec.parser_id, "tsha": rec.text_sha256,
                "cs": rec.content_status, "rlen": rec.raw_text_len,
                "rtrunc": rec.raw_text_truncated,
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


def update_content(
    record_id: int,
    *,
    raw_text: str | None,
    content_status: str,
    raw_text_len: int | None,
    truncated: bool,
    session=None,
) -> None:
    """Обновляет полный контент записи (backfill / догрузка).

    raw_text=None НЕ затирает сохранённый текст (COALESCE) — случай,
    когда повторный fetch снова упал, а сниппет терять нельзя.
    """
    with _session(session) as s:
        s.execute(
            text(
                f"UPDATE {schema.T_RECORD} SET "
                "raw_text = COALESCE(:raw, raw_text), "
                "content_status = :cs, raw_text_len = :rlen, "
                "raw_text_truncated = :tr, fetched_at = CURRENT_TIMESTAMP "
                "WHERE record_id = :id"
            ),
            {"raw": raw_text, "cs": content_status, "rlen": raw_text_len,
             "tr": truncated, "id": record_id},
        )


_BACKFILL_WHERE = (
    "(content_status IN ('legacy', 'fetch_failed', 'empty') "
    "OR content_status IS NULL) AND url IS NOT NULL"
)


def list_records_needing_content(*, limit: int = 100, session=None) -> list[dict]:
    """Записи без полного контента — очередь backfill (свежие первыми)."""
    with _session(session) as s:
        sql = (
            f"SELECT record_id, url FROM {schema.T_RECORD} "
            f"WHERE {_BACKFILL_WHERE} "
            "ORDER BY collected_at DESC LIMIT :limit"
        )
        return [dict(r) for r in s.execute(text(sql), {"limit": limit}).mappings().all()]


def count_records_needing_content(*, session=None) -> int:
    """Сколько записей ещё ждут догрузки контента."""
    with _session(session) as s:
        return s.execute(
            text(f"SELECT COUNT(*) FROM {schema.T_RECORD} WHERE {_BACKFILL_WHERE}")
        ).scalar_one()


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
    include_content: bool = False,
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
        columns = (
            "record_id, title, url, snippet, domain, trust_score, "
            "bank_slug, keyword, is_loophole, verdict_confidence, "
            "verdict_reason, verdict_model, status, "
            "collected_at, classified_at, content_status, raw_text_len"
        )
        if include_content:
            columns += ", raw_text, raw_text_truncated"
        sql = (
            f"SELECT {columns} "
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
    record_id: int | None = None,
    session=None,
) -> int:
    """Сохраняет пример в KB. embedding — list[float], сериализуется для pgvector.

    record_id связывает пример с записью loophole_record (ручная маркировка:
    дедуп и откат). Без embedding колонка опускается — кросс-БД (SQLite-тесты
    не понимают каст CAST(... AS vector)).
    """
    with _session(session) as s:
        if embedding is None:
            row = s.execute(
                text(
                    f"INSERT INTO {schema.T_KB_EXAMPLE} "
                    "(title, description, category, record_id) "
                    "VALUES (:title, :desc, :cat, :rid) RETURNING example_id"
                ),
                {"title": title, "desc": description, "cat": category, "rid": record_id},
            ).scalar_one()
        else:
            row = s.execute(
                text(
                    f"INSERT INTO {schema.T_KB_EXAMPLE} "
                    "(title, description, category, embedding, record_id) "
                    "VALUES (:title, :desc, :cat, CAST(:emb AS vector), :rid) "
                    "RETURNING example_id"
                ),
                {
                    "title": title, "desc": description, "cat": category,
                    "emb": _embedding_to_pgvector(embedding), "rid": record_id,
                },
            ).scalar_one()
        return row


def get_kb_example_by_record(record_id: int, *, session=None) -> dict | None:
    """Пример KB, привязанный к записи (дедуп ручной маркировки)."""
    with _session(session) as s:
        row = s.execute(
            text(
                f"SELECT example_id, title, description, category, record_id, "
                f"created_at FROM {schema.T_KB_EXAMPLE} "
                "WHERE record_id = :rid LIMIT 1"
            ),
            {"rid": record_id},
        ).mappings().first()
        return dict(row) if row else None


def delete_kb_example_by_record(record_id: int, *, session=None) -> int:
    """Удаляет примеры KB записи (откат ручной маркировки). Возвращает число удалённых."""
    with _session(session) as s:
        result = s.execute(
            text(f"DELETE FROM {schema.T_KB_EXAMPLE} WHERE record_id = :rid"),
            {"rid": record_id},
        )
        return result.rowcount


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
                    f"(embedding <=> CAST(:emb AS vector)) AS distance "
                    f"FROM {schema.T_KB_EXAMPLE} "
                    "WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:emb AS vector) LIMIT :k"
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
_PARSER_COLS = (
    "parser_id, workspace_id, name, code_path, status, config, created_at, "
    "last_run_at, created_by, last_edited_by, cron_expr, auto_enabled, "
    "next_run_at, source_keys, heal_attempts"
)


def _dt_str(value: datetime | str | None) -> str | None:
    """datetime → ISO-строка для хранения (SQLite/PG-совместимо)."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def save_parser(
    workspace_id: int,
    name: str,
    code_path: str,
    *,
    config: dict | None = None,
    created_by: str | None = None,
    source_keys: list[str] | None = None,
    session=None,
) -> int:
    """Создаёт запись парсера, возвращает parser_id."""
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_PARSER} "
                "(workspace_id, name, code_path, status, config, created_by, source_keys) "
                "VALUES (:ws, :name, :path, 'created', :cfg, :cb, :sk) RETURNING parser_id"
            ),
            {
                "ws": workspace_id, "name": name, "path": code_path,
                "cfg": json.dumps(config, ensure_ascii=False) if config is not None else None,
                "cb": created_by,
                "sk": json.dumps(source_keys, ensure_ascii=False) if source_keys is not None else None,
            },
        ).scalar_one()
        return row


def update_parser_code_path(parser_id: int, code_path: str, *, session=None) -> None:
    """Обновляет путь к сгенерированному коду парсера."""
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_PARSER} SET code_path = :p WHERE parser_id = :id"),
            {"p": code_path, "id": parser_id},
        )


def update_parser_schedule(
    parser_id: int,
    *,
    cron_expr: str | None,
    auto_enabled: bool,
    next_run_at: datetime | str | None,
    last_edited_by: str,
    name: str | None = None,
    session=None,
) -> None:
    """Атомарный PATCH расписания/автозапуска (+опционально имени)."""
    sets = "cron_expr = :c, auto_enabled = :a, next_run_at = :n, last_edited_by = :u"
    params: dict = {
        "c": cron_expr, "a": auto_enabled, "n": _dt_str(next_run_at),
        "u": last_edited_by, "id": parser_id,
    }
    if name is not None:
        sets += ", name = :name"
        params["name"] = name
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_PARSER} SET {sets} WHERE parser_id = :id"),
            params,
        )


def update_parser_next_run(
    parser_id: int, next_run_at: datetime | str | None, *, session=None,
) -> None:
    """Обновляет next_run_at (None — сброс расписания)."""
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_PARSER} SET next_run_at = :n WHERE parser_id = :id"),
            {"n": _dt_str(next_run_at), "id": parser_id},
        )


def set_heal_attempts(parser_id: int, attempts: int, *, session=None) -> None:
    """Устанавливает счётчик попыток самовосстановления парсера."""
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_PARSER} SET heal_attempts = :n WHERE parser_id = :id"),
            {"n": attempts, "id": parser_id},
        )


def disable_auto(parser_id: int, *, session=None) -> None:
    """Отключает автозапуск парсера (auto_enabled = FALSE)."""
    with _session(session) as s:
        s.execute(
            text(f"UPDATE {schema.T_PARSER} SET auto_enabled = FALSE WHERE parser_id = :id"),
            {"id": parser_id},
        )


def update_parser_status(parser_id: int, status: str, *, session=None) -> None:
    """Обновляет статус парсера и last_run_at."""
    with _session(session) as s:
        s.execute(
            text(
                f"UPDATE {schema.T_PARSER} SET status = :st, "
                "last_run_at = CURRENT_TIMESTAMP WHERE parser_id = :id"
            ),
            {"st": status, "id": parser_id},
        )


def list_parsers(workspace_id: int, *, session=None) -> list[dict]:
    """Устаревший workspace-листинг (обратная совместимость)."""
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT {_PARSER_COLS} FROM {schema.T_PARSER} "
                    "WHERE workspace_id = :ws ORDER BY parser_id"
                ),
                {"ws": workspace_id},
            ).mappings().all()
        ]


def list_all_parsers(*, session=None) -> list[dict]:
    """Общий каталог: все парсеры без фильтра workspace."""
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(f"SELECT {_PARSER_COLS} FROM {schema.T_PARSER} ORDER BY parser_id")
            ).mappings().all()
        ]


def list_parsers_with_source_keys(*, session=None) -> list[dict]:
    """Парсеры с заполненными source_keys (для карты ключей источников)."""
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT parser_id, name, source_keys FROM {schema.T_PARSER} "
                    "WHERE source_keys IS NOT NULL"
                )
            ).mappings().all()
        ]


def list_auto_parsers(*, session=None) -> list[dict]:
    """Парсеры с включённым автозапуском и заданным cron."""
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT {_PARSER_COLS} FROM {schema.T_PARSER} "
                    "WHERE auto_enabled = TRUE AND cron_expr IS NOT NULL"
                )
            ).mappings().all()
        ]


def get_parser(parser_id: int, *, session=None) -> dict | None:
    with _session(session) as s:
        row = s.execute(
            text(
                f"SELECT {_PARSER_COLS} FROM {schema.T_PARSER} WHERE parser_id = :id"
            ),
            {"id": parser_id},
        ).mappings().first()
        return dict(row) if row else None


def count_records_by_parser(parser_id: int, *, session=None) -> int:
    """Количество записей, собранных данным парсером."""
    with _session(session) as s:
        return s.execute(
            text(f"SELECT count(*) FROM {schema.T_RECORD} WHERE parser_id = :id"),
            {"id": parser_id},
        ).scalar_one()


# ── parser runs ─────────────────────────────────────────────────────────────
def create_run(parser_id: int, trigger: str, *, session=None) -> int:
    """Открывает запись запуска (status='running'), возвращает run_id."""
    with _session(session) as s:
        return s.execute(
            text(
                f"INSERT INTO {schema.T_PARSER_RUN} (parser_id, run_trigger, status) "
                "VALUES (:p, :t, 'running') RETURNING run_id"
            ),
            {"p": parser_id, "t": trigger},
        ).scalar_one()


def finish_run(
    run_id: int,
    status: str,
    *,
    items_found: int = 0,
    items_new: int = 0,
    items_dup: int = 0,
    error_text: str | None = None,
    log_tail: str | None = None,
    heal_report: str | None = None,
    session=None,
) -> None:
    """Завершает запуск: статус, счётчики items, ошибка, хвост лога, heal-отчёт."""
    with _session(session) as s:
        s.execute(
            text(
                f"UPDATE {schema.T_PARSER_RUN} SET status = :st, "
                "finished_at = CURRENT_TIMESTAMP, items_found = :f, items_new = :n, "
                "items_dup = :d, error_text = :e, log_tail = :l, heal_report = :h "
                "WHERE run_id = :id"
            ),
            {"st": status, "f": items_found, "n": items_new, "d": items_dup,
             "e": error_text, "l": log_tail, "h": heal_report, "id": run_id},
        )


def get_run(run_id: int, *, session=None) -> dict | None:
    """Возвращает запуск по run_id или None."""
    with _session(session) as s:
        row = s.execute(
            text(f"SELECT * FROM {schema.T_PARSER_RUN} WHERE run_id = :id"),
            {"id": run_id},
        ).mappings().first()
        return dict(row) if row else None


def list_runs(parser_id: int, *, limit: int = 20, session=None) -> list[dict]:
    """История запусков парсера, новые первыми."""
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT * FROM {schema.T_PARSER_RUN} WHERE parser_id = :p "
                    "ORDER BY run_id DESC LIMIT :lim"
                ),
                {"p": parser_id, "lim": limit},
            ).mappings().all()
        ]


def last_run(parser_id: int, *, session=None) -> dict | None:
    """Последний запуск парсера или None, если запусков не было."""
    rows = list_runs(parser_id, limit=1, session=session)
    return rows[0] if rows else None


def reap_stale_runs(*, session=None) -> int:
    """При старте приложения: зависшие 'running' → 'error'. Возвращает кол-во."""
    with _session(session) as s:
        res = s.execute(
            text(
                f"UPDATE {schema.T_PARSER_RUN} SET status = 'error', "
                "error_text = 'server restart', finished_at = CURRENT_TIMESTAMP "
                "WHERE status = 'running'"
            )
        )
        return res.rowcount or 0
