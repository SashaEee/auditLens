"""Retriever: семантический поиск по document_chunk через pgvector.

API:
  • semantic_search(query, ...) → list of dicts с chunk + document + источник
  • Фильтры: bank_slugs, doc_types, trust_min, max_age_days
  • Возвращает топ-K с distance + trust + breadcrumb

Используется агентом из ai/analyst.py как новый tool.
"""
from __future__ import annotations
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse
from sqlalchemy import text
from .. import db
from . import embedder

log = logging.getLogger(__name__)


# «Свежий» документ — прочитан за окно ИЛИ подтверждён за окно повторным
# чтением с тем же текстом (ночной обход пишет его в document_origin как
# duplicate): тариф, не менявшийся с августа, но перечитанный вчера, — свежий
_FRESH_SQL = ("(d.fetched_at > now() - make_interval(days => :max_age) "
              "OR d.document_id IN (SELECT o.document_id FROM document_origin o "
              "WHERE o.document_id IS NOT NULL "
              "AND o.created_at > now() - make_interval(days => :max_age) "
              "AND (o.skipped_reason IS NULL OR o.skipped_reason = 'duplicate')))")


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
    wh = ["d.trust_score >= :trust_min", _NOT_EXCLUDED]
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
        wh.append(_FRESH_SQL)
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


# Фрагменты вне поиска: хвост «Элементы интерфейса» и меню агрегаторов
# (migrations/091, аудит 03.10 ДАН-04)
_NOT_EXCLUDED = ("NOT EXISTS (SELECT 1 FROM document_chunk_excluded x "
                 "WHERE x.chunk_id = dc.chunk_id)")


def _facet_sql(where_sql: str) -> str:
    # страницы, а не версии одной страницы
    return f"""
        SELECT b.slug, b.name, count(DISTINCT d.url) n
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

    wh = ["d.trust_score >= :trust_min", "d.is_sponsored = FALSE", _NOT_EXCLUDED]
    params: dict[str, Any] = {"trust_min": trust_min, "pool": pool, "q": query}
    if doc_types:
        wh.append("d.doc_type::text = ANY(:doc_types)")
        params["doc_types"] = doc_types
    if max_age_days:
        wh.append(_FRESH_SQL)
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

    n_vec = sum(bool(r["via_vec"]) for r in rows)
    n_txt = sum(bool(r["via_txt"]) for r in rows)
    out, total = _group_rows(rows, per_doc=per_doc)
    out = out[:limit]
    return {
        "groups": out,
        "total": total,
        "modes": {"vector": n_vec, "text": n_txt},
        "facets": {
            "banks": [{"slug": f["slug"], "name": f["name"], "n": f["n"]}
                      for f in facets if f["slug"]],
        },
    }


# ── Ранг документа (аудит 03.10, ДАН-04) ──────────────────────────────────────
# Очки документа были суммой RRF его фрагментов: страница, где меню с
# «ипотекой» повторялось в трёх фрагментах, обгоняла тариф банка. Три
# адреса banki.ru занимали 7 мест выдачи, официальный документ Сбера в топ-20
# был один. Теперь: лучший фрагмент + четверть второго, вес вида источника,
# одна страница — одно место (свежая версия), зеркала по заголовку и началу
# текста. Ранг по-прежнему считает код, а не модель.
SOURCE_WEIGHT = {"regulator": 1.25, "bank_official": 1.25, "government": 1.15,
                 "legal_db": 1.15, "press": 0.95, "analyst": 0.95,
                 "aggregator": 0.8, "forum": 0.7, "blog": 0.7}
SECOND_HIT_W = 0.25
_TRACK_RE = re.compile(r"^(utm_|yclid$|gclid$|fbclid$|_openstat$|from$|ref$|erid$)", re.I)


def norm_url(url: str | None) -> str:
    """Адрес без www, меток рекламы, хвостового «/» и якоря."""
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", p.path or "/").rstrip("/") or "/"
    qs = [(k, v) for k, v in parse_qsl(p.query) if not _TRACK_RE.match(k)]
    return host + path + ("?" + urlencode(sorted(qs)) if qs else "")


def _group_rows(rows, *, per_doc: int = 3) -> tuple[list[dict], int]:
    """Строки фрагментов → группы-страницы. Возвращает (группы, число страниц)."""
    docs: dict[int, dict] = {}
    for r in rows:
        g = docs.get(r["document_id"])
        if g is None:
            g = docs[r["document_id"]] = {
                "document_id": r["document_id"], "url": r["url"],
                "title": r["title"], "text_head": r.get("text_head"),
                "doc_type": r.get("doc_type"),
                "trust_score": float(r.get("trust_score") or 0),
                "fetched_at": r.get("fetched_at"),
                "bank_slug": r.get("bank_slug"), "bank_name": r.get("bank_name"),
                "source_kind": r.get("source_kind"), "source_domain": r.get("source_domain"),
                "_sc": [], "_sn": set(), "hits": []}
        g["_sc"].append(float(r["score"]))
        snip = (r.get("snippet") or (r.get("text") or "")[:280]).strip()
        # перекрытие соседних фрагментов давало два одинаковых сниппета
        if len(g["hits"]) < per_doc and snip not in g["_sn"]:
            g["_sn"].add(snip)
            g["hits"].append({
                "idx": r.get("idx"), "headings_path": r.get("headings_path"),
                "snippet": snip,
                "relevance": round(float(r["vec_rel"]), 3) if r.get("vec_rel") is not None else None,
                # «почему нашлось» — точное совпадение слов весит в доказательстве иначе
                "via": "точное совпадение" if r.get("via_txt") and not r.get("via_vec")
                       else "по смыслу" if r.get("via_vec") and not r.get("via_txt")
                       else "по смыслу и словам"})
    for g in docs.values():
        sc = sorted(g.pop("_sc"), reverse=True)
        g.pop("_sn")
        w = SOURCE_WEIGHT.get(g["source_kind"] or "", 1.0)
        g["score"] = (sc[0] + SECOND_HIT_W * (sc[1] if len(sc) > 1 else 0.0)) * w
    # одна страница — одно место: побеждает свежая версия, очки — лучшие
    pages: dict[str, dict] = {}
    for g in sorted(docs.values(), key=lambda x: -x["score"]):
        k = norm_url(g["url"])
        cur = pages.get(k)
        if cur is None:
            pages[k] = {**g, "versions": 0}
            continue
        cur["versions"] += 1
        if g.get("fetched_at") and cur.get("fetched_at") and g["fetched_at"] > cur["fetched_at"]:
            pages[k] = {**g, "score": cur["score"], "versions": cur["versions"]}
    # зеркала: тот же домен, тот же длинный заголовок и то же начало текста
    seen: dict[tuple, dict] = {}
    for g in sorted(pages.values(), key=lambda x: -x["score"]):
        t = (g["title"] or "").strip().lower()
        head = (g.get("text_head") or "").strip().lower()[:60]
        k = ((g["source_domain"], t, head) if len(t) >= 20 and not t.startswith("http")
             else ("u", norm_url(g["url"])))
        if k in seen:
            cur = seen[k]
            mirrors = cur.get("mirrors", 0) + 1
            # как у версий: в ответ идёт свежая копия, очки — лучшие (иначе
            # прошлогодний тариф уезжал в отчёт как текущий)
            if g.get("fetched_at") and cur.get("fetched_at") and g["fetched_at"] > cur["fetched_at"]:
                seen[k] = {**g, "score": cur["score"], "versions": cur["versions"] + g["versions"],
                           "mirrors": mirrors}
            else:
                cur["mirrors"] = mirrors
            continue
        seen[k] = g
    out = list(seen.values())
    for g in out:
        g["n_hits"] = len(g["hits"])
        g.setdefault("mirrors", 0)
        # прежнее поле карточки: «ещё N копий»
        g["duplicates"] = g["versions"] + g["mirrors"]
    return out, len(pages)

