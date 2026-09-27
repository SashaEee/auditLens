"""Суть записи — пересказ механизма находки моделью для карточки «Уязвимостей».

Правило владельца: суть составляется ТОЛЬКО для уязвимостей и мошеннических
схем. 99% базы — «не подтверждено»; для них модель не вызывается, в карточке
показывается короткий комментарий классификатора, пришедший в том же вызове,
что и вердикт. Суть сохраняется в loophole_record.summary: лениво при первом
открытии записи и разовым проходом по уже найденным находкам
(``python -m bank_audit.loophole.summary --limit 400``).

Текст перед отправкой в модель маскируется (pii_mask) — как весь модуль.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from . import repository as repo
from .classify import _default_llm
from .config import LoopholeSettings
from .pii_mask import mask as pii_mask

log = logging.getLogger(__name__)

POSITIVE = {"vulnerability", "fraud_scheme"}
_TEXT_LIMIT = 6000
_SUMMARY_LIMIT = 700

SYSTEM_PROMPT = """Ты — аналитик внутреннего аудита банка. По записи (обсуждение на форуме, страница сайта банка или найденный агентом материал) опиши аудитору суть найденной уязвимости или мошеннической схемы.

Два-три предложения по-русски:
1) механизм: что именно делают и через какой продукт, канал или условие;
2) чем это выгодно злоумышленнику и где теряет банк или клиент.

Опирайся только на текст записи, ничего не выдумывай. Не пересказывай текст дословно, не приводи персональные данные, ссылки и цитаты. Без вступлений, заголовков, списков и markdown."""

# Одна генерация на запись в процессе: два одновременных открытия карточки
# не должны вызывать модель дважды.
_inflight: dict[int, asyncio.Lock] = {}


def is_finding(record: dict) -> bool:
    """Суть нужна только уязвимостям и мошенническим схемам."""
    kind = record.get("classification") or (
        "vulnerability" if record.get("is_loophole") is True else None
    )
    return kind in POSITIVE


def _source_text(record: dict) -> str:
    parts = [record.get("title"), record.get("verdict_reason"),
             record.get("snippet"), (record.get("raw_text") or "")[:_TEXT_LIMIT]]
    text = "\n\n".join(str(p).strip() for p in parts if p and str(p).strip())
    masked, _ = pii_mask(text)
    return masked


def _clean(raw: str) -> str:
    text = " ".join(str(raw or "").replace("**", "").split())
    if len(text) > _SUMMARY_LIMIT:
        cut = text[:_SUMMARY_LIMIT]
        text = (cut.rsplit(". ", 1)[0] + ".") if ". " in cut else cut.rstrip() + "…"
    return text


async def summarize_record(record_id: int, *, llm: Any = None, session=None) -> dict:
    """Возвращает суть записи, при необходимости составив её.

    ``{"summary": str | None, "generated": bool, "reason": str | None}``;
    reason — почему сути нет: ``not_found``, ``not_finding``, ``empty``, ``llm_error``.
    """
    record = repo.get_record_detail(record_id, session=session)
    if record is None:
        return {"summary": None, "generated": False, "reason": "not_found"}
    if record.get("summary"):
        return {"summary": record["summary"], "generated": False, "reason": None}
    if not is_finding(record):
        return {"summary": None, "generated": False, "reason": "not_finding"}
    lock = _inflight.setdefault(record_id, asyncio.Lock())
    try:
        async with lock:
            fresh = repo.get_record_detail(record_id, session=session)
            if fresh and fresh.get("summary"):
                return {"summary": fresh["summary"], "generated": False, "reason": None}
            text = _source_text(record)
            if not text:
                return {"summary": None, "generated": False, "reason": "empty"}
            if llm is None:
                llm = _default_llm()
            try:
                from langchain_core.messages import HumanMessage, SystemMessage
                messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=text)]
            except Exception:
                messages = [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": text}]
            try:
                response = await llm.ainvoke(messages)
                summary = _clean(getattr(response, "content", None) or str(response))
            except Exception as exc:  # сбой модели не ломает карточку
                log.warning("[summary] запись %s: модель недоступна: %s", record_id, exc)
                return {"summary": None, "generated": False, "reason": "llm_error"}
            if not summary:
                return {"summary": None, "generated": False, "reason": "empty"}
            repo.set_record_summary(
                record_id, summary, LoopholeSettings.load().effective_classify_model(),
                session=session,
            )
            return {"summary": summary, "generated": True, "reason": None}
    finally:
        if not lock.locked():
            _inflight.pop(record_id, None)


async def backfill(limit: int = 400, *, session=None) -> dict:
    """Разовый проход: суть для уже найденных уязвимостей и схем без сути."""
    ids = repo.list_records_needing_summary(limit=limit, session=session)
    done = failed = 0
    for record_id in ids:
        result = await summarize_record(record_id, session=session)
        if result.get("generated"):
            done += 1
        elif result.get("reason") == "llm_error":
            failed += 1
        if session is not None:
            session.commit()
    return {"candidates": len(ids), "generated": done, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Суть для найденных уязвимостей и схем")
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args()
    from .. import db
    db.init()
    with db.session() as session:
        print(asyncio.run(backfill(args.limit, session=session)))


if __name__ == "__main__":
    main()
