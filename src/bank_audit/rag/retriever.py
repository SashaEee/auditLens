"""Retriever: семантический поиск по document_chunk через pgvector.

API:
  • semantic_search(query, ...) → list of dicts с chunk + document + источник
  • Фильтры: bank_slugs, doc_types, trust_min, max_age_days
  • Возвращает топ-K с distance + trust + breadcrumb

Используется агентом из ai/analyst.py как новый tool.
"""
from __future__ import annotations
import logging
from typing import Any
from sqlalchemy import text
from .. import db
from . import embedder

log = logging.getLogger(__name__)


def semantic_search(
    query: str,
    *,
    top_k: int = 8,
    bank_slugs: list[str] | None = None,
    doc_types: list[str] | None = None,
    trust_min: float = 0.5,
    max_age_days: int | None = None,
    exclude_sponsored: bool = True,
) -> list[dict]:
    """Векторный поиск + фильтры. Возвращает топ-K chunk'ов с метаданными.

    Каждый результат:
      {
        chunk_id, text, headings_path, document_id, idx,
        bank_slug, bank_name, source_kind, source_domain,
        url, doc_type, trust_score, fetched_at,
        distance,        — cosine distance (0 = точное совпадение, 2 = противоположно)
        relevance,       — 1 - distance/2 (нормализовано в 0..1)
      }
    """
    if not query or not query.strip():
        return []

    qvec = embedder.embed_one(query)

    # Собираем WHERE clauses динамически
    wh = ["d.trust_score >= :trust_min"]
    params: dict[str, Any] = {
        "qvec": str(qvec),    # pgvector принимает '[0.1,0.2,...]' формат
        "trust_min": trust_min,
        "top_k": top_k,
    }
    if exclude_sponsored:
        wh.append("d.is_sponsored = FALSE")
    if bank_slugs:
        wh.append("b.slug = ANY(:bank_slugs)")
        params["bank_slugs"] = bank_slugs
    if doc_types:
        wh.append("d.doc_type::text = ANY(:doc_types)")
        params["doc_types"] = doc_types
    if max_age_days:
        wh.append("d.fetched_at > now() - make_interval(days => :max_age)")
        params["max_age"] = max_age_days

    where_sql = " AND ".join(wh)

    sql = f"""
        SELECT
            dc.chunk_id, dc.text, dc.headings_path, dc.idx,
            d.document_id, d.url, d.doc_type::text AS doc_type, d.title,
            d.trust_score, d.is_sponsored, d.fetched_at,
            b.slug AS bank_slug, b.name AS bank_name,
            st.kind AS source_kind, st.domain AS source_domain,
            (dc.embedding <=> CAST(:qvec AS vector)) AS distance
          FROM document_chunk dc
          JOIN document d         ON d.document_id = dc.document_id
          LEFT JOIN bank b        ON b.bank_id = d.bank_id
          LEFT JOIN source_trust st ON st.source_id = d.source_id
         WHERE {where_sql}
         ORDER BY dc.embedding <=> CAST(:qvec AS vector)
         LIMIT :top_k
    """

    with db.session() as s:
        rows = s.execute(text(sql), params).mappings().all()

    out = []
    for r in rows:
        d = dict(r)
        d["relevance"] = max(0.0, 1.0 - float(d["distance"]) / 2.0)
        out.append(d)
    return out
