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


# ── Гибридный поиск для витрины «База знаний» ────────────────────────────────
#
# Вектор и полнотекст промахиваются по-разному, и промахи не совпадают:
#   • «сколько стоит вести счёт» → вектор найдёт «плата за обслуживание»,
#     полнотекст не найдёт ничего (ни одного общего слова);
#   • «ПСК 24,7%», «п. 4.2», «предписание 03-45» → полнотекст найдёт точно,
#     вектор размажет редкие токены и выдаст соседние по смыслу абзацы.
# Поэтому идём двумя путями и сливаем ранги по RRF (reciprocal rank fusion):
# score = Σ 1/(K + rank). RRF не требует калибровки шкал — сравниваются места,
# а не косинусы с ts_rank, у которых нет общей единицы измерения.
RRF_K = 60

# маркеры подсветки: не HTML, чтобы фронт собирал React-узлы, а не вставлял
# сырую разметку из содержимого документов
HL_START, HL_STOP = "⟦", "⟧"      # ⟦ ⟧


def _facet_sql(where_sql: str) -> str:
    return f"""
        SELECT b.slug, b.name, count(DISTINCT d.document_id) n
          FROM document d
          JOIN document_chunk dc ON dc.document_id = d.document_id
          LEFT JOIN bank b ON b.bank_id = d.bank_id
         WHERE {where_sql}
         GROUP BY 1,2 ORDER BY n DESC
    """


def hybrid_search(
    query: str,
    *,
    limit: int = 20,
    per_doc: int = 3,
    bank_slugs: list[str] | None = None,
    doc_types: list[str] | None = None,
    max_age_days: int | None = None,
    trust_min: float = 0.5,
    pool: int = 60,
) -> dict:
    """Вектор + полнотекст, слияние по RRF, группировка по документу.

    Возвращает {"groups": [...], "total": n, "modes": {...}} — каждая группа это
    документ со списком совпавших фрагментов. Аудитору важен документ целиком
    (на него он сошлётся в рабочем файле), а фрагменты — доказательство, почему
    документ подошёл.
    """
    if not query or not query.strip():
        return {"groups": [], "total": 0, "modes": {"vector": 0, "text": 0}}
    query = query.strip()

    wh = ["d.trust_score >= :trust_min", "d.is_sponsored = FALSE"]
    params: dict[str, Any] = {"trust_min": trust_min, "pool": pool, "q": query}
    if doc_types:
        wh.append("d.doc_type::text = ANY(:doc_types)")
        params["doc_types"] = doc_types
    if max_age_days:
        wh.append("d.fetched_at > now() - make_interval(days => :max_age)")
        params["max_age"] = max_age_days
    # счётчики банков считаем ДО фильтра по банку — иначе в списке остался бы
    # ровно один пункт, тот же, что уже выбран, и переключиться было бы некуда
    facet_where_sql = " AND ".join(wh)
    if bank_slugs:
        wh.append("b.slug = ANY(:bank_slugs)")
        params["bank_slugs"] = bank_slugs
    where_sql = " AND ".join(wh)

    params["qvec"] = str(embedder.embed_one(query))

    # Полнотекст: websearch_to_tsquery понимает кавычки и «-минус», как привык
    # пользователь поисковика. Заголовок документа весит больше тела (0.6 vs 0.3):
    # попадание в название — более сильный сигнал, чем упоминание в абзаце.
    sql = f"""
        WITH vec AS (
            SELECT dc.chunk_id,
                   row_number() OVER (ORDER BY dc.embedding <=> CAST(:qvec AS vector)) rk,
                   1.0 - (dc.embedding <=> CAST(:qvec AS vector)) / 2.0 AS rel
              FROM document_chunk dc
              JOIN document d ON d.document_id = dc.document_id
              LEFT JOIN bank b ON b.bank_id = d.bank_id
             WHERE {where_sql}
             ORDER BY dc.embedding <=> CAST(:qvec AS vector)
             LIMIT :pool
        ),
        txt AS (
            SELECT dc.chunk_id,
                   row_number() OVER (ORDER BY
                       ts_rank_cd(dc.tsv, websearch_to_tsquery(CAST('russian' AS regconfig), :q)) * 0.3
                     + ts_rank_cd(d.title_tsv, websearch_to_tsquery(CAST('russian' AS regconfig), :q)) * 0.6
                     DESC) rk
              FROM document_chunk dc
              JOIN document d ON d.document_id = dc.document_id
              LEFT JOIN bank b ON b.bank_id = d.bank_id
             WHERE {where_sql}
               AND (dc.tsv @@ websearch_to_tsquery(CAST('russian' AS regconfig), :q)
                 OR d.title_tsv @@ websearch_to_tsquery(CAST('russian' AS regconfig), :q))
             ORDER BY rk LIMIT :pool
        ),
        fused AS (
            SELECT COALESCE(v.chunk_id, t.chunk_id) chunk_id,
                   COALESCE(1.0/({RRF_K} + v.rk), 0) + COALESCE(1.0/({RRF_K} + t.rk), 0) AS score,
                   v.rel AS vec_rel,
                   (v.chunk_id IS NOT NULL) AS via_vec,
                   (t.chunk_id IS NOT NULL) AS via_txt
              FROM vec v FULL OUTER JOIN txt t ON t.chunk_id = v.chunk_id
        )
        SELECT f.chunk_id, f.score, f.vec_rel, f.via_vec, f.via_txt,
               dc.idx, dc.headings_path, dc.text,
               ts_headline(CAST('russian' AS regconfig), dc.text,
                           websearch_to_tsquery(CAST('russian' AS regconfig), :q),
                           'StartSel={HL_START}, StopSel={HL_STOP}, MaxFragments=2,'
                           'MaxWords=30, MinWords=14, FragmentDelimiter= … ') AS snippet,
               d.document_id, d.url, d.title, d.doc_type::text doc_type,
               -- у выгрузок ЦБ и PDF без метаданных в title лежит сам URL, а в
               -- headings_path — «Страница 71». Тогда единственное осмысленное
               -- название документа — его первая строка. Служебные маркеры
               -- разбивки PDF («## Страница 12») выкидываем: в названии они шум.
               btrim(left(regexp_replace(
                   regexp_replace(d.content_text, '#+\\s*Страница\\s+\\d+', ' ', 'g'),
                   '\\s+', ' ', 'g'), 110)) AS text_head,
               d.trust_score, d.fetched_at,
               b.slug bank_slug, b.name bank_name,
               st.kind source_kind, st.domain source_domain
          FROM fused f
          JOIN document_chunk dc ON dc.chunk_id = f.chunk_id
          JOIN document d ON d.document_id = dc.document_id
          LEFT JOIN bank b ON b.bank_id = d.bank_id
          LEFT JOIN source_trust st ON st.source_id = d.source_id
         ORDER BY f.score DESC
         LIMIT :pool
    """

    with db.session() as s:
        rows = s.execute(text(sql), params).mappings().all()
        facets = s.execute(text(_facet_sql(facet_where_sql)),
                           {k: v for k, v in params.items()
                            if k not in ("qvec", "q", "pool", "bank_slugs")}
                           ).mappings().all()

    # Группируем: документ — единица выдачи, фрагменты внутри — доказательства
    groups: dict[int, dict] = {}
    n_vec = n_txt = 0
    for r in rows:
        n_vec += bool(r["via_vec"])
        n_txt += bool(r["via_txt"])
        g = groups.get(r["document_id"])
        if g is None:
            g = groups[r["document_id"]] = {
                "document_id": r["document_id"], "url": r["url"],
                "title": r["title"], "text_head": r["text_head"],
                "doc_type": r["doc_type"],
                "trust_score": float(r["trust_score"] or 0),
                "fetched_at": r["fetched_at"],
                "bank_slug": r["bank_slug"], "bank_name": r["bank_name"],
                "source_kind": r["source_kind"], "source_domain": r["source_domain"],
                "score": 0.0, "hits": [],
            }
        g["score"] += float(r["score"])
        if len(g["hits"]) < per_doc:
            g["hits"].append({
                "idx": r["idx"],
                "headings_path": r["headings_path"],
                "snippet": (r["snippet"] or r["text"][:280]).strip(),
                "relevance": round(float(r["vec_rel"]), 3) if r["vec_rel"] is not None else None,
                # «почему нашлось» — аудитору важно отличать точное совпадение
                # слов от смыслового: у первого другой вес в доказательстве
                "via": "точное совпадение" if r["via_txt"] and not r["via_vec"]
                       else "по смыслу" if r["via_vec"] and not r["via_txt"]
                       else "по смыслу и словам",
            })

    # Один и тот же текст встречается под разными URL (зеркала consultant.ru,
    # печатная версия страницы, ?utm-хвосты). По sha они разные документы, для
    # аудитора — один. Схлопываем по «банк + заголовок», оставляя лучший.
    seen: dict[tuple, dict] = {}
    for g in sorted(groups.values(), key=lambda x: -x["score"]):
        k = (g["bank_slug"], (g["title"] or "").strip().lower())
        if k in seen:
            seen[k]["duplicates"] = seen[k].get("duplicates", 0) + 1
            continue
        seen[k] = g
    out = list(seen.values())[:limit]
    for g in out:
        g["n_hits"] = len(g["hits"])
    return {
        "groups": out,
        "total": len(groups),
        "modes": {"vector": n_vec, "text": n_txt},
        "facets": {
            "banks": [{"slug": f["slug"], "name": f["name"], "n": f["n"]}
                      for f in facets if f["slug"]],
        },
    }
