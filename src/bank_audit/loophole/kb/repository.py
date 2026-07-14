"""CRUD базы знаний модуля loophole (примеры лазеек + RAG-документы).

Тонкая прослойка над ``loophole.repository``: эмбеддит текст через
``rag.embedder.embed_one`` и делегирует сохранение/поиск в репозиторий.
SQL — через ``sqlalchemy.text()``, диалект Greenplum 6 (без PK/UNIQUE).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from .. import repository as repo
from .. import db_schema as schema
from ...rag import embedder

log = logging.getLogger(__name__)


# ── examples ────────────────────────────────────────────────────────────────
def add_example(
    title: str,
    description: str,
    *,
    category: str = "general",
    session: Any = None,
) -> int:
    """Добавляет пример лазейки: эмбеддит description и делегирует в repo."""
    embedding = embedder.embed_one(description)
    return repo.save_kb_example(
        title,
        description,
        category=category,
        embedding=embedding,
        session=session,
    )


def search_similar(
    query: str,
    *,
    k: int = 5,
    session: Any = None,
) -> list[dict]:
    """Семантический поиск по текстовому запросу: embed → search_kb_similar."""
    embedding = embedder.embed_one(query)
    return repo.search_kb_similar(embedding, k=k, session=session)


def list_examples(
    *,
    category: str | None = None,
    limit: int = 100,
    session: Any = None,
) -> list[dict]:
    """Список примеров лазеек с опциональным фильтром по category."""
    with repo._session(session) as s:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if category is not None:
            clauses.append("category = :cat")
            params["cat"] = category
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT example_id, title, description, category, created_at "
            f"FROM {schema.T_KB_EXAMPLE}{where} "
            "ORDER BY example_id LIMIT :limit"
        )
        return [dict(r) for r in s.execute(text(sql), params).mappings().all()]


def count_examples(*, session: Any = None) -> int:
    """Количество примеров в базе знаний."""
    with repo._session(session) as s:
        return int(
            s.execute(text(f"SELECT count(*) FROM {schema.T_KB_EXAMPLE}")).scalar_one()
        )


# ── docs ────────────────────────────────────────────────────────────────────
def add_doc(
    source: str,
    content: str,
    *,
    session: Any = None,
) -> int:
    """Добавляет RAG-документ: эмбеддит content, сохраняет в loophole_kb_doc."""
    embedding = embedder.embed_one(content)
    emb_str = repo._embedding_to_pgvector(embedding)
    with repo._session(session) as s:
        row = s.execute(
            text(
                f"INSERT INTO {schema.T_KB_DOC} "
                "(source, content, embedding) "
                "VALUES (:src, :content, :emb::vector) RETURNING doc_id"
            ),
            {"source": source, "content": content, "emb": emb_str},
        ).scalar_one()
        return row
