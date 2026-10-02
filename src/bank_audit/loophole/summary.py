"""Суть записи — карточка находки от модели для «Уязвимостей».

Правило владельца: модель вызывается ТОЛЬКО для уязвимостей и мошеннических
схем. 99% базы — «не подтверждено»; для них модель не вызывается, в карточке
показывается короткий комментарий классификатора, пришедший в том же вызове,
что и вердикт.

Один вызов на запись возвращает JSON: суть (механизм и кто теряет), заголовок
находки вместо названия ветки форума, банк из текста (ставится, только если
сборщик банк не определил) и сомнение модели — «похоже на рекламу / новость /
жалобу», чтобы эксперт ЦК КС видел вероятный шум. Результат сохраняется в
loophole_record: лениво при первом открытии записи и разовым проходом
(``python -m bank_audit.loophole.summary --limit 400 [--refresh]``).

Текст перед отправкой в модель маскируется (pii_mask) — как весь модуль.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from typing import Any

from . import repository as repo
from .classify import _default_llm
from .config import LoopholeSettings
from .pii_mask import mask as pii_mask

log = logging.getLogger(__name__)

POSITIVE = {"vulnerability", "fraud_scheme"}
_TEXT_LIMIT = 6000
_SUMMARY_LIMIT = 700
_HEADLINE_LIMIT = 110
_DOUBT_LIMIT = 160

SYSTEM_PROMPT = """Ты — аналитик внутреннего аудита банка. Классификатор отметил запись (обсуждение на форуме, пост в соцсети, новость или страница сайта банка) как возможную лазейку или мошенническую схему. Подготовь для аудитора карточку находки.

Верни СТРОГО JSON без markdown и пояснений:
{"headline": "...", "summary": "...", "bank": "..." или null, "doubt": "..." или null}

headline — заголовок находки до 90 знаков: что делают и в каком продукте, например «Кэшбэк за переводы между своими картами через СБП». Не название ветки форума, без точки в конце.
summary — два-три предложения по-русски: механизм (что делают, через какой продукт, канал или условие) и кто теряет — банк или клиент. Клиента, который пользуется условиями продукта без обмана, называй клиентом. «Злоумышленник» — только для обмана, подделки, обналичивания, чужих данных и счетов.
bank — банк, о продукте которого речь, как его называют в тексте («Сбербанк», «ВТБ», «Т-Банк»); null, если банк не назван или банков несколько.
doubt — если лазейки или схемы в тексте на самом деле нет, коротко почему: «реклама карты по партнёрской ссылке», «новость без описания приёма», «жалоба клиента», «обычное использование продукта». Иначе null. Не придумывай механизм и ущерб, которых в тексте нет.

Опирайся только на текст записи. Не пересказывай его дословно, не приводи персональные данные, ссылки и цитаты."""

# Название банка из ответа модели → код bank_slug системы. Неизвестный банк
# сохраняется названием: интерфейс показывает код как есть.
_BANK_ALIASES = {
    "sberbank": ("сбербанк", "сбер", "сбер банк", "пао сбербанк", "сбербанк россии"),
    "vtb": ("втб", "банк втб"),
    "alfabank": ("альфа-банк", "альфа банк", "альфабанк", "альфа"),
    "tbank": ("т-банк", "т банк", "тинькофф", "тинькофф банк", "тбанк"),
    "gazprombank": ("газпромбанк",),
    "raiffeisen": ("райффайзенбанк", "райффайзен", "райфайзен", "райффайзен банк"),
    "rosbank": ("росбанк",),
    "sovcombank": ("совкомбанк",),
    "mtsbank": ("мтс банк", "мтс-банк", "мтс деньги", "мтс"),
    "pochtabank": ("почта банк",),
    "otkritie": ("открытие", "банк открытие"),
    "psb": ("псб", "промсвязьбанк"),
    "rshb": ("россельхозбанк", "рсхб"),
    "domrf": ("банк дом.рф", "дом.рф", "дом рф"),
    "ozonbank": ("озон банк", "ozon банк"),
    "yandex": ("яндекс банк", "яндекс пэй"),
    "uralsib": ("уралсиб",),
    "akbars": ("ак барс", "ак барс банк"),
    "mkb": ("мкб", "московский кредитный банк"),
    "homecredit": ("хоум банк", "хоум кредит", "хоум кредит банк"),
    "renaissance": ("ренессанс банк", "ренессанс кредит"),
}
_BANK_BY_NAME = {name: slug for slug, names in _BANK_ALIASES.items() for name in names}

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


def _clip(raw: Any, limit: int) -> str:
    text = " ".join(str(raw or "").replace("**", "").split())
    if len(text) > limit:
        cut = text[:limit]
        text = (cut.rsplit(". ", 1)[0] + ".") if ". " in cut else cut.rstrip() + "…"
    return text


def _clean(raw: str) -> str:
    return _clip(raw, _SUMMARY_LIMIT)


def normalize_bank(name: Any) -> str | None:
    """Название банка из ответа модели → код системы (или само название)."""
    value = " ".join(str(name or "").replace("«", "").replace("»", "").split()).strip(" .")
    if not value or value.lower() in {"null", "none", "нет", "не указан", "несколько"}:
        return None
    return _BANK_BY_NAME.get(value.lower(), value[:60])


def parse_card(raw: str) -> dict:
    """Ответ модели → {summary, headline, bank, doubt}. Не JSON — весь текст суть."""
    text = str(raw or "").strip()
    fenced = re.search(r"\{.*\}", text, re.S)
    data: Any = None
    if fenced:
        try:
            data = json.loads(fenced.group(0))
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return {"summary": _clean(text), "headline": None, "bank": None, "doubt": None}
    headline = _clip(data.get("headline"), _HEADLINE_LIMIT).rstrip(".") or None
    doubt = _clip(data.get("doubt"), _DOUBT_LIMIT) or None
    if doubt and doubt.lower() in {"null", "none", "нет"}:
        doubt = None
    return {"summary": _clean(data.get("summary")), "headline": headline,
            "bank": normalize_bank(data.get("bank")), "doubt": doubt}


def _cached(record: dict) -> dict:
    return {"summary": record.get("summary"), "headline": record.get("headline"),
            "doubt": record.get("summary_doubt"), "bank": record.get("bank_slug"),
            "generated": False, "reason": None}


def _refused(reason: str) -> dict:
    return {"summary": None, "headline": None, "doubt": None, "bank": None,
            "generated": False, "reason": reason}


async def summarize_record(
    record_id: int, *, llm: Any = None, refresh: bool = False, session=None,
) -> dict:
    """Возвращает карточку находки, при необходимости составив её.

    ``{"summary", "headline", "doubt", "bank", "generated", "reason"}``; reason —
    почему сути нет: ``not_found``, ``not_finding``, ``empty``, ``llm_error``.
    refresh=True пересоставляет суть, составленную до заголовков (headline пуст).
    """
    def ready(rec: dict | None) -> bool:
        return bool(rec and rec.get("summary") and (rec.get("headline") or not refresh))

    record = repo.get_record_detail(record_id, session=session)
    if record is None:
        return _refused("not_found")
    if ready(record):
        return _cached(record)
    if not is_finding(record):
        return _refused("not_finding")
    lock = _inflight.setdefault(record_id, asyncio.Lock())
    try:
        async with lock:
            fresh = repo.get_record_detail(record_id, session=session)
            if ready(fresh):
                return _cached(fresh)
            text = _source_text(record)
            if not text:
                return _refused("empty")
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
                card = parse_card(getattr(response, "content", None) or str(response))
            except Exception as exc:  # сбой модели не ломает карточку
                log.warning("[summary] запись %s: модель недоступна: %s", record_id, exc)
                return _refused("llm_error")
            if not card["summary"]:
                return _refused("empty")
            repo.set_record_summary(
                record_id, card["summary"], LoopholeSettings.load().effective_classify_model(),
                headline=card["headline"], doubt=card["doubt"], bank=card["bank"],
                session=session,
            )
            return {**card, "generated": True, "reason": None}
    finally:
        if not lock.locked():
            _inflight.pop(record_id, None)


async def backfill(limit: int = 400, *, refresh: bool = False, session=None) -> dict:
    """Разовый проход: суть для находок без сути; refresh — и для составленных
    до заголовков, банка и сомнения."""
    ids = repo.list_records_needing_summary(limit=limit, refresh=refresh, session=session)
    done = failed = 0
    for record_id in ids:
        result = await summarize_record(record_id, refresh=refresh, session=session)
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
    parser.add_argument("--refresh", action="store_true",
                        help="пересоставить суть, составленную до заголовков и сомнения")
    args = parser.parse_args()
    from .. import db
    db.init()
    with db.session() as session:
        print(asyncio.run(backfill(args.limit, refresh=args.refresh, session=session)))


if __name__ == "__main__":
    main()
