"""Названия отчётов, сохранённых до 29.09, когда названием было начало вопроса.

Быстрая модель по цепочке вопросов беседы и началу ответа; модель не ответила —
эвристика. Беседа получает название своего первого отчёта, если её не
переименовывали вручную.

    python -m bank_audit.ai.report_title_backfill --dry-run --limit 20
    python -m bank_audit.ai.report_title_backfill            # все, по 4 одновременно
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import text

from .. import db
from ..web import userdata
from . import report_title as rt

log = logging.getLogger(__name__)


def _todo(limit: int | None) -> list[dict]:
    with db.session() as s:
        rows = s.execute(text("""
            SELECT report_id, session_id, question, title, left(body, 1800) AS body, created_at
              FROM report ORDER BY report_id
        """)).mappings().all()
    todo = [dict(r) for r in rows if rt.is_default_title(r["title"], r["question"])]
    return todo[:limit] if limit else todo


def _prior(session_id: int | None, before) -> list[str]:
    if not session_id:
        return []
    with db.session() as s:
        rows = s.execute(text("""
            SELECT content FROM chat_message
             WHERE session_id = :s AND role = 'user' AND created_at < :t
             ORDER BY created_at
        """), {"s": session_id, "t": before}).all()
    return [r[0] for r in rows if r[0]]


async def run(*, dry: bool, limit: int | None, concurrency: int) -> dict:
    todo = await asyncio.to_thread(_todo, limit)
    client = rt._client()
    sem = asyncio.Semaphore(concurrency)
    done: list[tuple[int, str, bool]] = []

    async def one(r: dict) -> None:
        async with sem:
            prior = await asyncio.to_thread(_prior, r["session_id"], r["created_at"])
            prior = [q for q in prior if " ".join(q.split()) != " ".join(str(r["question"]).split())]
            t = await rt.llm_title(r["question"], r["body"] or "", prior, client=client)
            by_model = bool(t)
            if not t:
                base = r["question"]
                if rt._SHORT_REPLY.match(str(base or "").strip()) and prior:
                    base = prior[0]
                t = rt.heuristic_title(base)
            if not dry:
                await asyncio.to_thread(userdata.set_report_title, r["report_id"], t)
            done.append((r["report_id"], t, by_model))
            if len(done) % 25 == 0:
                log.info("названия отчётов: %d из %d", len(done), len(todo))

    await asyncio.gather(*(one(r) for r in todo))
    sessions = 0
    if not dry:
        # беседа — по своему первому отчёту
        with db.session() as s:
            firsts = s.execute(text("""
                SELECT DISTINCT ON (session_id) session_id, title FROM report
                 WHERE session_id IS NOT NULL ORDER BY session_id, report_id
            """)).all()
        for sid, title in firsts:
            if title and await asyncio.to_thread(userdata.title_session_from_report, sid, title):
                sessions += 1
    return {"reports": len(done), "by_model": sum(1 for d in done if d[2]),
            "sessions": sessions, "sample": sorted(done)[-15:]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    res = asyncio.run(run(dry=a.dry_run, limit=a.limit, concurrency=a.concurrency))
    print(f"отчётов: {res['reports']}, названий от модели: {res['by_model']}, бесед: {res['sessions']}")
    for rid, t, m in res["sample"]:
        print(f"  {rid}: {t}{'' if m else '  (эвристика)'}")


if __name__ == "__main__":
    main()
