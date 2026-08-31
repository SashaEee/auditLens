"""CRUD к loophole_* таблицам через db.session() и sqlalchemy.text().

Без ORM. Дедуп по sha256 — app-level (SELECT exists → skip), что универсально
работает и в Greenplum 6 (без UNIQUE-констрейнта), и в SQLite (тесты).
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from .. import db
from . import db_schema as schema
from .models import LoopholeRecord
from .pii_mask import mask as pii_mask

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
                "content_status, raw_text_len, raw_text_truncated, published_at, "
                "verdict_confidence, verdict_reason, verdict_model) "
                "VALUES (:sha, :title, :url, :snip, :dom, :trust, :bank, :kw, :raw, "
                ":status, :loop, :pid, :tsha, :cs, :rlen, :rtrunc, :published, "
                ":confidence, :reason, :model) "
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
                "published": rec.published_at,
                "confidence": rec.verdict_confidence,
                "reason": rec.verdict_reason,
                "model": rec.verdict_model,
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


def _record_dict(row) -> dict:
    """Нормализует DB-типы записи для одинакового JSON в PostgreSQL и SQLite."""
    record = dict(row)
    if record.get("is_loophole") is not None:
        record["is_loophole"] = bool(record["is_loophole"])
    return record


def get_record(record_id: int, *, session=None) -> dict | None:
    with _session(session) as s:
        row = s.execute(
            text(f"SELECT * FROM {schema.T_RECORD} WHERE record_id = :id"),
            {"id": record_id},
        ).mappings().first()
        return _record_dict(row) if row else None


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
            clauses.append("published_at >= :pf")
            params["pf"] = period_from
        if period_to:
            clauses.append("published_at < :pt_exclusive")
            params["pt_exclusive"] = period_to + timedelta(days=1)
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
            "published_at, collected_at, classified_at, content_status, raw_text_len"
        )
        if include_content:
            columns += ", raw_text, raw_text_truncated"
        sql = (
            f"SELECT {columns} "
            f"FROM {schema.T_RECORD}{where} "
            "ORDER BY COALESCE(verdict_confidence, 0) DESC, collected_at DESC "
            "LIMIT :limit OFFSET :offset"
        )
        return [_record_dict(r) for r in s.execute(text(sql), params).mappings().all()]


def list_published_cases(*, limit: int = 500, session=None) -> list[dict]:
    """Возвращает только финально опубликованные подтверждённые кейсы."""
    with _session(session) as s:
        rows = s.execute(
            text(
                f"SELECT record_id, title, url, snippet, domain, trust_score, bank_slug, "
                f"keyword, is_loophole, verdict_confidence, verdict_reason, verdict_model, "
                f"status, published_at, collected_at, classified_at FROM {schema.T_RECORD} "
                "WHERE status = 'published' AND is_loophole = TRUE "
                "ORDER BY collected_at DESC, record_id DESC LIMIT :limit"
            ),
            {"limit": limit},
        ).mappings().all()
        return [_record_dict(row) for row in rows]


def list_catalog_cases(
    *,
    bank_slugs: list[str] | None = None,
    period_from: date | None = None,
    period_to: date | None = None,
    query_text: str | None = None,
    verification_status: str = "all",
    limit: int = 500,
    offset: int = 0,
    session=None,
) -> list[dict]:
    """Общая база: подтверждённые и предварительные подозрения.

    ``verified`` показывает только опубликованные положительные решения;
    ``pending`` — только предварительные записи, ещё ожидающие ЦК КС.
    """
    if verification_status not in {"all", "verified", "pending"}:
        raise ValueError("Неизвестный статус верификации")
    with _session(session) as s:
        clauses = ["record.is_loophole = TRUE", "record.status IN ('published', 'preliminary')"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if verification_status == "verified":
            clauses.append("record.status = 'published'")
        elif verification_status == "pending":
            clauses.append("record.status = 'preliminary'")
        if bank_slugs:
            placeholders = ", ".join(f":b{i}" for i in range(len(bank_slugs)))
            clauses.append(f"record.bank_slug IN ({placeholders})")
            params.update({f"b{i}": value for i, value in enumerate(bank_slugs)})
        if period_from:
            clauses.append("record.published_at >= :period_from")
            params["period_from"] = period_from
        if period_to:
            clauses.append("record.published_at < :period_to")
            params["period_to"] = period_to + timedelta(days=1)
        if query_text:
            clauses.append(
                "(LOWER(COALESCE(record.title, '')) LIKE :query "
                "OR LOWER(COALESCE(record.snippet, '')) LIKE :query)"
            )
            params["query"] = f"%{query_text.lower()}%"
        rows = s.execute(
            text(
                "SELECT record.record_id, record.title, record.url, record.snippet, record.domain, "
                "record.trust_score, record.bank_slug, record.keyword, record.is_loophole, "
                "record.verdict_confidence, record.verdict_reason, record.verdict_model, record.status, "
                "record.published_at, record.collected_at, record.classified_at, "
                "record.content_status, record.raw_text_len, imported.research_id AS provenance_research_id, "
                "imported.source_id AS provenance_source_id, imported.imported_at AS provenance_imported_at "
                f"FROM {schema.T_RECORD} AS record "
                "LEFT JOIN loophole_preliminary_import AS imported ON imported.record_id = record.record_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY record.collected_at DESC, record.record_id DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).mappings().all()
        catalog: list[dict] = []
        for row in rows:
            record = _record_dict(row)
            research_id = record.pop("provenance_research_id", None)
            source_id = record.pop("provenance_source_id", None)
            imported_at = record.pop("provenance_imported_at", None)
            if research_id is not None:
                record["provenance"] = {
                    "research_id": research_id,
                    "source_id": source_id,
                    "imported_at": str(imported_at) if imported_at is not None else None,
                }
            else:
                record["provenance"] = None
            catalog.append(record)
        return catalog


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
            clauses.append("published_at >= :pf")
            params["pf"] = period_from
        if period_to:
            clauses.append("published_at < :pt_exclusive")
            params["pt_exclusive"] = period_to + timedelta(days=1)
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


def get_workspace(workspace_id: int, *, session=None) -> dict | None:
    """Workspace по id — для server-side проверки ownership."""
    with _session(session) as s:
        row = s.execute(
            text(
                f"SELECT workspace_id, user_id, name, created_at, last_active_at "
                f"FROM {schema.T_WORKSPACE} WHERE workspace_id = :id"
            ),
            {"id": workspace_id},
        ).mappings().first()
        return dict(row) if row else None


def list_verification_queue(*, limit: int = 200, session=None) -> list[dict]:
    """Очередь верификации ЦК КС: записи, помеченные лазейкой (LLM/сборщиком),
    по которым ещё нет ручного вердикта (verdict_model != 'manual').

    Вызывается только после server-side проверки роли ccks_expert. raw_text
    не отдаётся (payload) — как и в list_records.
    """
    with _session(session) as s:
        sql = (
            f"SELECT record_id, title, url, snippet, domain, trust_score, "
            "bank_slug, keyword, verdict_confidence, verdict_reason, status, "
            "published_at, collected_at, classified_at "
            f"FROM {schema.T_RECORD} "
            "WHERE is_loophole = TRUE "
            "AND (verdict_model IS NULL OR verdict_model != 'manual') "
            "ORDER BY collected_at DESC LIMIT :limit"
        )
        return [dict(r) for r in s.execute(text(sql), {"limit": limit}).mappings().all()]


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


def create_parser_development_request(
    *,
    workspace_id: int,
    url: str,
    domain: str,
    description: str,
    user_id: str,
    session=None,
) -> int:
    """Сохраняет заявку на разработку парсера и её audit в одной транзакции."""
    with _session(session) as s:
        proposal_id = s.execute(
            text(
                "INSERT INTO source_proposal "
                "(purpose, url, domain, reason, proposed_by, status) "
                "VALUES (:purpose, :url, :domain, :reason, :user_id, :status) "
                "RETURNING proposal_id"
            ),
            {
                "purpose": "loophole_parser",
                "url": url,
                "domain": domain,
                "reason": description,
                "user_id": user_id,
                "status": "pending",
            },
        ).scalar_one()
        s.execute(
            text(
                f"INSERT INTO {schema.T_ACTION_LOG} "
                "(user_id, workspace_id, action, detail) "
                "VALUES (:user_id, :workspace_id, :action, :detail)"
            ),
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "action": "parser_development_request_create",
                "detail": json.dumps(
                    {"proposal_id": proposal_id, "domain": domain}, ensure_ascii=False
                ),
            },
        )
        return proposal_id


_SECRET_VALUE = re.compile(
    r"""
    (?:
        ["']?authorization["']?\s*[:=]\s*["']?(?:bearer|basic)\s+[^"',;\s}]+["']?
        |["']?(?:bearer|jwt)["']?\s*[:=]\s*(?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*'|[^,;\s}]+)
        |["']?(?:api[_-]?(?:key|token)|access[_-]?(?:key|token)|refresh[_-]?token)["']?
          \s*[:=]\s*(?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*'|[^,;\s}]+)
        |["']?cloud[_-]?(?:access[_-]?)?(?:api[_-]?)?(?:key|token|secret)["']?
          \s*[:=]\s*(?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*'|[^,;\s}]+)
        |["']?(?:client[_-]?secret|credential(?:s)?|password|secret|token|private[_-]?key)["']?
          \s*[:=]\s*(?:"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*'|[^,;\s}]+)
        |eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+
        |(?:sk|rk|gsk|gh[pousr]|xox[baprs]|hf|AIza|ya29)[_-][A-Za-z0-9._~-]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _redact_audit_text(value: Any, *, limit: int = 2000) -> str:
    """Маскирует ПДн и типовые секреты перед записью в audit log."""
    masked, _ = pii_mask(str(value or ""))
    return _SECRET_VALUE.sub("[SECRET]", masked)[:limit]


def redact_audit_text(value: Any, *, limit: int = 200) -> str:
    """Публичный helper для redacted detail во всех audit sinks."""
    return _redact_audit_text(value, limit=limit)


def create_agent_audit(
    *,
    run_id: str,
    user_id: str,
    workspace_id: int | None,
    query: str,
    tools_used: list[str] | tuple[str, ...],
    duration_ms: int,
    result: str,
    status: str,
    error_code: str | None = None,
    session=None,
) -> int:
    """Сохраняет только redacted метаданные запуска управляемого агента."""
    names = [name for name in dict.fromkeys(tools_used) if isinstance(name, str)]
    with _session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_AGENT_AUDIT_LOG} "
                "(run_id, user_id, workspace_id, query_redacted, tools_used, "
                "duration_ms, result_redacted, status, error_code) "
                "VALUES (:run, :user, :ws, :query, :tools, :duration, :result, :status, :error) "
                "RETURNING audit_id"
            ),
            {
                "run": run_id,
                "user": user_id,
                "ws": workspace_id,
                "query": _redact_audit_text(query),
                "tools": json.dumps(names, ensure_ascii=False),
                "duration": max(0, int(duration_ms)),
                "result": _redact_audit_text(result),
                "status": status,
                "error": error_code,
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


def list_telegram_targets(*, session=None) -> list[dict]:
    """Статус Telegram-целей (source_keys вида «t.me/<name>») для админ-экрана
    (story 1.5): цель + операционный статус парсера. Технические payload
    (config, code_path) и обычные web-источники в поверхность не попадают."""
    with _session(session) as s:
        rows = s.execute(
            text(
                f"SELECT parser_id, name, status, last_run_at, source_keys "
                f"FROM {schema.T_PARSER} WHERE CAST(source_keys AS TEXT) LIKE '%t.me/%' "
                "ORDER BY parser_id"
            )
        ).mappings().all()
    targets: list[dict] = []
    for row in rows:
        try:
            keys = json.loads(row["source_keys"] or "[]")
        except (TypeError, ValueError):
            keys = []
        for key in keys:
            if isinstance(key, str) and key.startswith("t.me/"):
                targets.append({
                    "target": key,
                    "parser_id": row["parser_id"],
                    "parser_name": row["name"],
                    "status": row["status"],
                    "last_run_at": _dt_str(row["last_run_at"]),
                })
    return targets


def list_auto_parsers(*, session=None) -> list[dict]:
    """Парсеры с включённым автозапуском и заданным cron."""
    with _session(session) as s:
        return [
            dict(r) for r in s.execute(
                text(
                    f"SELECT {_PARSER_COLS} FROM {schema.T_PARSER} "
                    "WHERE auto_enabled = TRUE AND cron_expr IS NOT NULL AND status = 'ready'"
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
