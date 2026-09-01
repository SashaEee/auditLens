#!/usr/bin/env python
"""Считает векторы для записей «Лазеек».

Эмбедим УТВЕРЖДЕНИЕ лазейки — заголовок, краткое изложение и довод
классификатора, — а не тело статьи первоисточника. Разница принципиальная:
именно тело давало мусорные попадания («эскроу» находил дробление вкладов,
потому что слово стояло в проходной фразе). Запрос аудитора всегда о сути
приёма, а суть записана в заголовке и изложении.

Идемпотентно: считает только у записей без вектора, если не передан --all.
Запуск: python scripts/backfill_loophole_vectors.py [--all] [--batch 32]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from bank_audit import db  # noqa: E402
from bank_audit.loophole import db_schema as schema  # noqa: E402
from bank_audit.rag import embedder  # noqa: E402

log = logging.getLogger("backfill_loophole")

# Больше 2000 символов утверждению не нужно: bge-m3 всё равно режет на 512
# токенах, а заголовок с изложением укладываются с запасом.
_MAX_CHARS = 2000


def claim_of(row) -> str:
    parts = [row["title"] or "", row["snippet"] or "", row["verdict_reason"] or ""]
    return " ".join(p.strip() for p in parts if p and p.strip())[:_MAX_CHARS]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="пересчитать и те, у кого вектор уже есть")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    with db.session() as s:
        try:
            s.execute(text(f"SELECT embedding FROM {schema.T_RECORD} LIMIT 0"))
        except Exception:                                     # noqa: BLE001
            log.error("колонки embedding нет — примените migrations/ensure_vector.sql")
            return 2
        where = "" if args.all else " WHERE embedding IS NULL"
        rows = s.execute(text(
            f"SELECT record_id, title, snippet, verdict_reason "
            f"FROM {schema.T_RECORD}{where} ORDER BY record_id"
        )).mappings().all()

    if not rows:
        log.info("нечего считать — все записи уже с вектором")
        return 0

    log.info("записей к обработке: %d", len(rows))
    t0 = time.time()
    done = skipped = 0
    for i in range(0, len(rows), args.batch):
        chunk = rows[i:i + args.batch]
        claims, ids = [], []
        for r in chunk:
            c = claim_of(r)
            if not c:
                skipped += 1          # пустая запись: вектор нуля был бы ложью
                continue
            claims.append(c)
            ids.append(r["record_id"])
        if not claims:
            continue
        vecs = embedder.embed_batch(claims)
        with db.session() as s:
            for rid, vec in zip(ids, vecs):
                s.execute(
                    text(f"UPDATE {schema.T_RECORD} SET embedding = CAST(:v AS vector) "
                         "WHERE record_id = :id"),
                    {"v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "id": rid},
                )
            s.commit()
        done += len(ids)
        log.info("  %d/%d", done, len(rows))

    log.info("готово: %d векторов за %.1f с; пропущено пустых: %d",
             done, time.time() - t0, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
