"""Review summarizer: для каждого банка с >=20 отзывами генерим горячий summary.

Что считаем:
  • total_reviews, avg_rating
  • sentiment counts (pos/neu/neg)
  • topics: топ-N жалоб и похвал — из review_topic + review_sentiment
  • by_source: разрез по banki/sravni/bankiros (поведение разных аудиторий)
  • Sample quotes: 1-3 примера для каждой топ-темы (берём short representative)

LLM не использует — всё детерминистично, из существующих review_topic/sentiment.
LLM-обогащение можно добавить отдельным advanced job (пока не нужно).

Запускается:
  • По cron раз в день (через alerts_loop-style task)
  • Вручную: python -m bank_audit.rag.summarizer
  • API: POST /api/rag/rebuild-summaries
"""
from __future__ import annotations
import json, logging
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import text
from .. import db

log = logging.getLogger(__name__)

MIN_REVIEWS_FOR_SUMMARY = 20
PERIOD_ALL  = "all"
PERIOD_30D  = "last_30d"
PERIOD_90D  = "last_90d"


def _build_period_filter(period: str) -> str:
    if period == PERIOD_30D:
        return "AND r.posted_at > now() - interval '30 days'"
    if period == PERIOD_90D:
        return "AND r.posted_at > now() - interval '90 days'"
    return ""  # ALL


def _get_top_topics_with_quotes(
    session, bank_id: int, period: str, sentiment_label: str, top_n: int = 5
) -> list[dict]:
    """Возвращает топ-N топиков с примерами цитат (для жалоб/похвал)."""
    period_sql = _build_period_filter(period)
    rows = session.execute(text(f"""
        WITH ranked_topics AS (
          SELECT rt.topic, count(*) AS n,
                 round(avg(r.rating)::numeric, 2) AS avg_rating
            FROM review r
            JOIN review_topic rt USING(review_id)
            LEFT JOIN review_sentiment rs USING(review_id)
           WHERE r.bank_id = :bank_id
             AND rs.label = :sentiment
             {period_sql}
           GROUP BY rt.topic
           ORDER BY n DESC
           LIMIT :n
        )
        SELECT * FROM ranked_topics
    """), {"bank_id": bank_id, "sentiment": sentiment_label, "n": top_n}).mappings().all()

    result = []
    for row in rows:
        topic = row["topic"]
        # Берём 2 коротких репрезентативных цитаты для UI
        quotes_rows = session.execute(text(f"""
            SELECT left(r.text, 280) AS quote, r.source, r.rating
              FROM review r
              JOIN review_topic rt USING(review_id)
              LEFT JOIN review_sentiment rs USING(review_id)
             WHERE r.bank_id = :bank_id
               AND rt.topic = :topic
               AND rs.label = :sentiment
               AND length(r.text) BETWEEN 80 AND 600
               {period_sql}
             ORDER BY r.posted_at DESC NULLS LAST
             LIMIT 2
        """), {"bank_id": bank_id, "topic": topic,
               "sentiment": sentiment_label}).mappings().all()
        result.append({
            "topic":       topic,
            "n":           int(row["n"]),
            "avg_rating":  float(row["avg_rating"]) if row["avg_rating"] is not None else None,
            "quotes":      [dict(q) for q in quotes_rows],
        })
    return result


def _get_by_source_breakdown(session, bank_id: int, period: str) -> dict:
    """Распределение отзывов по источникам с avg rating."""
    period_sql = _build_period_filter(period)
    rows = session.execute(text(f"""
        SELECT r.source, count(*) AS n,
               round(avg(r.rating)::numeric, 2) AS avg_rating
          FROM review r
         WHERE r.bank_id = :bank_id {period_sql}
         GROUP BY r.source
    """), {"bank_id": bank_id}).mappings().all()
    return {r["source"]: {"n": int(r["n"]),
                          "avg_rating": float(r["avg_rating"]) if r["avg_rating"] else None}
            for r in rows}


def _get_basic_stats(session, bank_id: int, period: str) -> dict | None:
    period_sql = _build_period_filter(period)
    row = session.execute(text(f"""
        SELECT count(*) AS total,
               round(avg(r.rating)::numeric, 2) AS avg_rating,
               count(*) FILTER (WHERE rs.label='pos') AS pos,
               count(*) FILTER (WHERE rs.label='neg') AS neg,
               count(*) FILTER (WHERE rs.label='neu') AS neu
          FROM review r
          LEFT JOIN review_sentiment rs USING(review_id)
         WHERE r.bank_id = :bank_id {period_sql}
    """), {"bank_id": bank_id}).mappings().first()
    if not row or row["total"] < MIN_REVIEWS_FOR_SUMMARY:
        return None
    return {
        "total": int(row["total"]),
        "avg_rating": float(row["avg_rating"]) if row["avg_rating"] else None,
        "pos": int(row["pos"] or 0),
        "neg": int(row["neg"] or 0),
        "neu": int(row["neu"] or 0),
    }


def rebuild_for_bank(bank_id: int, period: str = PERIOD_ALL) -> dict:
    """Перестраивает summary для одного банка/периода. Idempotent."""
    with db.session() as s:
        stats = _get_basic_stats(s, bank_id, period)
        if not stats:
            return {"skipped": "too_few_reviews", "bank_id": bank_id, "period": period}

        complaints = _get_top_topics_with_quotes(s, bank_id, period, "neg", top_n=5)
        praise     = _get_top_topics_with_quotes(s, bank_id, period, "pos", top_n=3)
        by_src     = _get_by_source_breakdown(s, bank_id, period)

        s.execute(text("""
            INSERT INTO review_summary(
                bank_id, period, total_reviews, avg_rating,
                sentiment_pos, sentiment_neg, sentiment_neu,
                top_complaints, top_praise, by_source,
                generated_at
            )
            VALUES (:b, :p, :t, :ar, :pos, :neg, :neu,
                    CAST(:c AS jsonb), CAST(:pr AS jsonb), CAST(:bs AS jsonb),
                    now())
            ON CONFLICT (bank_id, period) DO UPDATE
              SET total_reviews  = EXCLUDED.total_reviews,
                  avg_rating     = EXCLUDED.avg_rating,
                  sentiment_pos  = EXCLUDED.sentiment_pos,
                  sentiment_neg  = EXCLUDED.sentiment_neg,
                  sentiment_neu  = EXCLUDED.sentiment_neu,
                  top_complaints = EXCLUDED.top_complaints,
                  top_praise     = EXCLUDED.top_praise,
                  by_source      = EXCLUDED.by_source,
                  generated_at   = now()
        """), {"b": bank_id, "p": period,
               "t": stats["total"], "ar": stats["avg_rating"],
               "pos": stats["pos"], "neg": stats["neg"], "neu": stats["neu"],
               "c": json.dumps(complaints, ensure_ascii=False, default=str),
               "pr": json.dumps(praise, ensure_ascii=False, default=str),
               "bs": json.dumps(by_src, ensure_ascii=False, default=str)})

    return {"ok": True, "bank_id": bank_id, "period": period,
            "total": stats["total"], "complaints": len(complaints),
            "praise": len(praise)}


def rebuild_all(period: str = PERIOD_ALL) -> dict:
    """Перестраивает summary для всех банков с достаточным числом отзывов."""
    with db.session() as s:
        bank_rows = s.execute(text(f"""
            SELECT b.bank_id, b.slug, b.name,
                   count(r.review_id) AS n_reviews
              FROM bank b
              JOIN review r USING(bank_id)
              {f"WHERE r.posted_at > now() - interval '30 days'" if period == PERIOD_30D else ""}
              {f"WHERE r.posted_at > now() - interval '90 days'" if period == PERIOD_90D else ""}
             GROUP BY b.bank_id, b.slug, b.name
             HAVING count(r.review_id) >= {MIN_REVIEWS_FOR_SUMMARY}
             ORDER BY n_reviews DESC
        """)).mappings().all()

    log.info("review_summary rebuild_all: %s банков с >=%s отзывами для периода %s",
             len(bank_rows), MIN_REVIEWS_FOR_SUMMARY, period)
    results = []
    for r in bank_rows:
        try:
            res = rebuild_for_bank(r["bank_id"], period)
            results.append({"slug": r["slug"], **res})
        except Exception as e:
            log.warning("rebuild for %s failed: %s", r["slug"], e)
            results.append({"slug": r["slug"], "error": str(e)[:200]})
    return {"period": period, "banks": len(bank_rows), "results": results}


# CLI: python -m bank_audit.rag.summarizer [period]
if __name__ == "__main__":
    import sys, logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from ..config import Settings
    db.init(Settings.load())
    period = sys.argv[1] if len(sys.argv) > 1 else PERIOD_ALL
    result = rebuild_all(period)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
