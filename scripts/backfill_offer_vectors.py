"""Векторы и полнотекст для предложений витрины «Рынок».

Запуск:  python scripts/backfill_offer_vectors.py [--limit N] [--all]
По умолчанию считает только то, у чего вектора ещё нет.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bank_audit.rag import embedder                       # noqa: E402
from sqlalchemy import text                               # noqa: E402


def offer_text(row) -> str:
    """Что именно ищем: банк, название, вид продукта и условия одной строкой.

    Название в отрыве от банка бесполезно — «Детская» есть у половины рынка, —
    а условия дают те слова, которых в названии нет вовсе.
    """
    parts = [row.bank_name, row.title, row.category]
    cond = (row.conditions or "")[:400]
    if cond:
        parts.append(cond)
    return " · ".join(p for p in parts if p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="сколько записей за прогон")
    ap.add_argument("--all", action="store_true", help="пересчитать и то, что уже посчитано")
    args = ap.parse_args()

    from bank_audit import db
    where = "" if args.all else "WHERE o.embedding IS NULL"
    lim = f"LIMIT {args.limit}" if args.limit else ""
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT o.offer_id, o.title, o.category, o.conditions, b.name AS bank_name
              FROM product_offer o
              LEFT JOIN bank b ON b.bank_id = o.bank_id
              {where}
             ORDER BY o.offer_id
             {lim}
        """)).all()
    if not rows:
        print("нечего считать: у всех предложений вектор уже есть")
        return 0

    print(f"предложений к обработке: {len(rows)}")
    t0 = time.time()
    texts = [offer_text(r) for r in rows]
    vectors = embedder.embed_batch(texts, batch_size=32, show_progress=True)
    done = 0
    with db.session() as s:
        for row, vec in zip(rows, vectors):
            if not vec:
                continue
            s.execute(text("""
                UPDATE product_offer
                   SET embedding = CAST(:v AS vector),
                       embedded_at = now(),
                       search_tsv = to_tsvector(CAST('russian' AS regconfig), :t)
                 WHERE offer_id = :id
            """), {"v": str(vec), "t": offer_text(row), "id": row.offer_id})
            done += 1
        s.commit()
    print(f"записано: {done} за {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
