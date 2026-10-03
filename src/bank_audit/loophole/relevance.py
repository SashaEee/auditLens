"""Отметка «не о банках» для базы «Аудита уязвимостей» — без модели.

Зачем (аудит 03.10, УЯЗ-01): в базу идёт поток ~700–2 000 записей в день,
большей частью не о банках (новостные ленты, посты органов власти, отзывы о
заводах без ссылки на источник), и все они попадают в счётчики и списки
«Все»/«Не подтверждено». Правило детерминированное: в заголовке, фрагменте
и тексте нет ни одной основы банковской лексики. Освобождены страницы банков
(официальные сайты и сообщества банков в соцсетях).

Отметка — отдельная таблица (migrations/092). Скрываются только записи «не
подтверждено» без ручного решения и не из исследований (repository.
_OFFTOPIC_SQL); находки не скрываются никогда, даже если их пометить.

    python -m bank_audit.loophole.relevance --dry-run   # пробный прогон
    python -m bank_audit.loophole.relevance             # дозаливка отметок

Функция offtopic_reason() — чистая: её можно вызвать во внешнем сборщике до
разметки моделью, тогда отсев сэкономит и вызовы модели.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

from sqlalchemy import text

log = logging.getLogger(__name__)

NO_BANK_TERMS = "no_bank_terms"
RULE_VERSION = 1
# Основы банковской лексики. Ошибка в сторону «о банках» безопасна (запись
# просто не скрывается), пропущенная основа — нет, поэтому словарь широкий.
BANK_TERMS = (
    "банк(?!рот)", "сбер", "втб", "тиньк", "т-банк", "тбанк", "альфа", "газпромбанк",
    "совком", "райффайзен", "россельхоз", "уралсиб", "промсвязь", "псб", "озон банк",
    "вклад", "депозит", "кредит", "займ", "заём", "заем", "микрофин", "мфо", "ипотек",
    "рассрочк", "карт[аеуыо]", "карточк", "дебетов", "овердрафт", "кэшб[эе]к", "кешб[эе]к",
    "cashback", "сбп", "быстрых платеж", "перевод", "платеж", "платёж", "оплат", "эквайр",
    "mcc", "мсс", "комисси", "обнал", "дроп", "мошенн", "антифрод", "161-фз", "115-фз",
    "самозапрет", "эскроу", "брокер", "облигац", "иис", "инвест", "сч[её]т", "наличн",
    "списан", "банкомат", "qr", "p2p", "крипт", "usdt", "visa", "mastercard", "мир pay",
    "юmoney", "юмани", "qiwi", "киви", "коллектор", "просроч", "долг", "чарджб",
    "chargeback", "госуслуг", "лицензи", "цб рф", "центробанк", "банк росси", "процент",
    "бонус", "лимит", "страхов",
    # деньги и мошенничество без слова «банк»: «аферисты выманили 2 млн рублей»,
    # «списали деньги без ведома», «перевёл незнакомцу» — короткие посты
    # решаются почти только по заголовку
    "деньг", "денеж", "рубл", "₽", "списал", "списыва", "перев[её]л", "перечисл", "афер",
    "обман", "выман", "развод", "украл", "похит", "кража", "(?<![а-яё])цб(?![а-яё])",
    "(?<![а-яё])ставк", "финанс", "выплат", "пенси", "зарплат", "налог", "кошел",
    "bank", "sber", "tinkoff", "vtb", "alfa", "raiffeisen", "card", "credit", "loan",
    "deposit", "payment", "money", "fraud", "scam",
    # мошенничество и платежи: «назвал код из СМС», «подозрительная транзакция»,
    # «Apple Pay не работает», «заблокировали по 161», «СБОЛ не открывается»
    "транзакц", r"служб\w* безопасност", "смс", "sms", "код из", "продиктова", "фишинг",
    "(?<![a-z])pay(?![a-z])", "пэй", "плат[иеяю]", "сбол", "халв", "нспк",
    "(?<![0-9])161(?![0-9])", r"115[- ]?фз", "бирж", "вывод средств", r"личн\w* кабинет",
)
_BANK_RE = re.compile("|".join(BANK_TERMS), re.IGNORECASE)
# Сообщества банков в соцсетях: «ok.ru/sber», «vk.com/vtb» — о банке по определению
_SOCIAL = ("ok.ru", "vk.com", "vk.ru", "t.me", "dzen.ru")
_BANK_PAGES = re.compile(
    r"^(sber|sberbank|vtb|alfa|alfabank|tinkoff|tbank|gazprombank|gpb|atb|atb\.su|psb|"
    r"psbank|rshb|sovcombank|raiffeisen|otp|otpbank|mts|mtsbank|ozon|ozonbank|yandexbank|"
    r"pochtabank|domrf|rosbank|uralsib|akbars|mkb|homecredit|rencredit|bspb|open|"
    r"otkritie|unicredit|lokobank|sinara)", re.I)


def _bank_page(url: str | None) -> bool:
    if not url:
        return False
    try:
        from ..rag.trust import is_bank_official
        if is_bank_official(url)[0]:
            return True
    except Exception:  # noqa: BLE001
        pass
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host in _SOCIAL:
        parts = (p.path or "/").strip("/").split("/")
        # t.me/s/<канал> — веб-витрина канала Telegram
        first = parts[1] if host == "t.me" and parts[0] == "s" and len(parts) > 1 else parts[0]
        return bool(_BANK_PAGES.match(first))
    return False


def offtopic_reason(*, title, snippet, raw_text, url) -> str | None:
    """'no_bank_terms' — в тексте нет ни одной банковской основы; None — о банках."""
    if _bank_page(url):
        return None
    hay = " ".join(str(p)[:20000] for p in (title, snippet, raw_text) if p)
    return NO_BANK_TERMS if _BANK_RE.search(hay) is None else None


def _rows(s, *, after_id: int, limit: int, only_new: bool):
    cond = "r.record_id > :after"
    if only_new:
        # новые и размеченные прошлой версией правила (после правки словаря
        # поднять RULE_VERSION — и дозаливка пересчитает старые отметки)
        cond += (" AND NOT EXISTS (SELECT 1 FROM loophole_record_topic t "
                 "WHERE t.record_id = r.record_id AND t.rule_version >= :v)")
    return s.execute(text(
        "SELECT r.record_id, r.title, r.snippet, SUBSTR(r.raw_text, 1, 20000) AS raw_text, "
        "r.url, r.domain, r.is_loophole, r.classification "
        f"FROM loophole_record r WHERE {cond} ORDER BY r.record_id LIMIT :limit"),
        {"after": after_id, "limit": limit, "v": RULE_VERSION}).mappings().all()


def tag_batch(s, *, after_id: int = 0, limit: int = 2000, dry_run: bool = False,
              only_new: bool = True) -> tuple[int | None, list[tuple]]:
    rows = _rows(s, after_id=after_id, limit=limit, only_new=only_new)
    marks = [(r["record_id"], offtopic_reason(title=r["title"], snippet=r["snippet"],
                                              raw_text=r["raw_text"], url=r["url"]), r)
             for r in rows]
    if marks and not dry_run:
        s.execute(text(
            "INSERT INTO loophole_record_topic (record_id, offtopic_reason, rule_version) "
            "VALUES (:id, :r, :v) ON CONFLICT (record_id) DO UPDATE SET "
            "offtopic_reason = excluded.offtopic_reason, rule_version = excluded.rule_version, "
            "checked_at = CURRENT_TIMESTAMP"),
            [{"id": rid, "r": reason, "v": RULE_VERSION} for rid, reason, _r in marks])
    return (rows[-1]["record_id"] if rows else None), marks


def tag_all(*, dry_run: bool = False, batch: int = 2000) -> dict:
    from .. import db
    by_reason, by_domain, findings_no_terms, sample = Counter(), Counter(), [], []
    after, n = 0, 0
    while True:
        with db.session() as s:
            last, marks = tag_batch(s, after_id=after, limit=batch, dry_run=dry_run,
                                    only_new=not dry_run)
        if last is None:
            break
        after = last
        for _rid, reason, r in marks:
            n += 1
            by_reason[reason or "about_bank"] += 1
            if reason:
                by_domain[r.get("domain") or "(без ссылки)"] += 1
                if dry_run and r["record_id"] % 53 == 0 and len(sample) < 80:
                    sample.append(f'{r.get("domain") or "-"} · {(r.get("title") or "")[:90]}')
                kind = r.get("classification") or ("vulnerability" if r.get("is_loophole") else None)
                if kind in ("vulnerability", "fraud_scheme") and len(findings_no_terms) < 30:
                    findings_no_terms.append({"record_id": r["record_id"],
                                              "title": (r.get("title") or "")[:100]})
    return {"checked": n, "by_reason": dict(by_reason),
            "offtopic_by_domain": dict(by_domain.most_common(25)),
            "findings_without_bank_terms": findings_no_terms, "sample_offtopic": sample,
            "dry_run": dry_run}


TAGGER_S = int(os.getenv("LOOPHOLE_TOPIC_TAGGER_S", "600"))
TAGGER_ENABLED = os.getenv("LOOPHOLE_TOPIC_TAGGER", "1") not in ("0", "false", "no")


def _tag_new() -> int:
    """Новые записи — только после ручной дозаливки: пока таблица пуста, цикл
    ничего не пишет (отметки появляются после пробного прогона и согласования)."""
    from .. import db
    with db.session() as s:
        if not s.execute(text("SELECT 1 FROM loophole_record_topic LIMIT 1")).first():
            return 0
        start = s.execute(text(
            "SELECT COALESCE(max(record_id), 0) FROM loophole_record_topic")).scalar() or 0
    n = 0
    for _ in range(5):
        with db.session() as s:
            last, marks = tag_batch(s, after_id=max(0, int(start) - 50000), limit=2000)
        n += len(marks)
        if last is None or len(marks) < 2000:
            break
    return n


async def topic_tagger_loop():
    """Раз в 10 минут размечает новые записи внешнего сборщика."""
    await asyncio.sleep(120)
    while True:
        try:
            n = await asyncio.to_thread(_tag_new)
            if n:
                log.info("уязвимости: отмечено записей «о банках/не о банках»: %d", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — разметка не роняет приложение
            log.warning("уязвимости: разметка тем не удалась: %s", e)
        await asyncio.sleep(TAGGER_S)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args(argv)
    from .. import db
    from ..config import Settings
    db.init(Settings.load())
    print(json.dumps(tag_all(dry_run=args.dry_run, batch=args.batch),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
