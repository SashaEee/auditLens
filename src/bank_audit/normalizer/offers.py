"""Нормализация черновиков офферов в нормализованную модель + SCD2 + change_history.
   Работает идемпотентно: повторный запуск без изменений данных не создаёт новых строк."""
from __future__ import annotations
import json
import re
from decimal import Decimal
from typing import Iterable
from sqlalchemy import text
from rapidfuzz import process, fuzz
import logging
from .. import db
from ..hashing import stable_digest

log = logging.getLogger(__name__)
from ..models import OfferDraft
from .rules import BANK_ALIASES, SBER_SLUGS, normalize_bank_key

NORMALIZE_FIELDS = (
    "rate_pct", "rate_kind", "currency",
    "amount_min", "amount_max", "term_months_min", "term_months_max",
    "fee_open", "fee_service",
    "early_withdraw", "capitalization", "replenishable",
    "conditions",
)

def _fuzzy_ok(key: str, alias: str) -> bool:
    """Вето на ложные fuzzy-склейки: WRatio даёт ~90 коротким ключам-подстрокам
    («ик банк» ⊂ «норвик банк», «сбер» ⊂ «сбережений») — так Тинькофф «всасывал»
    Металлинвестбанк, а Сбер — Национальный банк сбережений (аудит 22.07.2026:
    5 банков-магнитов, 24 чужих оффера). Принимаем матч, только если токены
    одной стороны — подмножество другой («сбербанк россии» ~ «сбербанк») или
    имена похожи целиком (опечатки: «сити банк» ~ «ситибанк»)."""
    kt, at = set(key.split()), set(alias.split())
    return kt <= at or at <= kt or fuzz.ratio(key, alias) >= 85


def resolve_bank(session, raw_name: str) -> int:
    """Резолвит raw-имя банка в bank_id (создаёт строку при необходимости).
    Логика:
      1. Нормализуем имя (lower, без кавычек, без префиксов «ПАО/АО/...»)
      2. Прямой lookup в BANK_ALIASES
      3. Fuzzy-match по тем же ключам (порог 88)
      4. Иначе — slug = unknown_<digest>
    """
    raw_name = (raw_name or "").strip()
    key = normalize_bank_key(raw_name)
    slug = BANK_ALIASES.get(key)
    if not slug and key:
        # fuzzy: топ-5 кандидатов, а не единственный лучший — иначе короткий
        # ключ-подстрока («сбер») с тем же score перекрывает валидный «сбербанк»,
        # вето его режет, и «Сбербанк России» падал бы в unknown_
        for alias, score, _ in process.extract(
                key, list(BANK_ALIASES.keys()), scorer=fuzz.WRatio, limit=5):
            if score < 88:
                break
            if _fuzzy_ok(key, alias):
                slug = BANK_ALIASES[alias]
                break
    if not slug and key and re.fullmatch(r"[a-z0-9_-]+", key):
        # Источники иногда отдают латинский slug вместо имени («gazprombank»,
        # «psb») — до unknown_-фолбэка пробуем прямое совпадение со слагом уже
        # известного банка. Иначе плодятся латинские двойники (фидбек аналитиков;
        # 105 офферов были слиты миграцией 22.07.2026).
        row = session.execute(text("SELECT bank_id FROM bank WHERE slug=:s"),
                              {"s": key}).first()
        if row:
            return row[0]
    if not slug:
        # Пустое имя или "?" → bank_id из placeholder-банка "unknown_empty"
        slug_key = key if key else "_empty_"
        slug = "unknown_" + stable_digest({"n": slug_key})[:10]
    row = session.execute(text("SELECT bank_id FROM bank WHERE slug=:s"), {"s": slug}).first()
    if row:
        return row[0]
    return session.execute(text("""
        INSERT INTO bank(slug, name, is_sber)
        VALUES (:s, :n, :is_sber)
        RETURNING bank_id
    """), {"s": slug, "n": raw_name or "?", "is_sber": slug in SBER_SLUGS}).scalar_one()

def _digest(d: OfferDraft) -> str:
    payload = {f: getattr(d, f) for f in NORMALIZE_FIELDS}
    return stable_digest(payload)

def upsert_offer(session, d: OfferDraft, snapshot_id: int | None,
                 source_page_id: int | None) -> tuple[int, bool]:
    bank_id = resolve_bank(session, d.bank_name_raw)
    row = session.execute(text("""
        INSERT INTO product_offer(bank_id, category, external_id, primary_source, title, url)
        VALUES (:b,:c,:e,:s,:t,:u)
        ON CONFLICT (bank_id, category, external_id) DO UPDATE
          SET last_seen=now(), title=EXCLUDED.title,
              url=COALESCE(EXCLUDED.url, product_offer.url),
              is_active=true    -- вернувшийся из протухания оффер оживает
        RETURNING offer_id
    """), {"b": bank_id, "c": d.category, "e": d.external_id,
           "s": "sravni_aggregator", "t": d.title, "u": d.url}).scalar_one()
    offer_id = row

    new_digest = _digest(d)
    cur = session.execute(text("""
        SELECT terms_id, digest FROM product_terms
         WHERE offer_id=:o AND valid_to IS NULL
         ORDER BY valid_from DESC LIMIT 1
    """), {"o": offer_id}).first()

    if cur and cur[1] == new_digest:
        return offer_id, False  # без изменений

    # закрываем текущую версию
    if cur:
        session.execute(text("UPDATE product_terms SET valid_to=now() WHERE terms_id=:t"),
                        {"t": cur[0]})

    new_id = session.execute(text("""
        INSERT INTO product_terms(
            offer_id, rate_pct, rate_kind, currency,
            amount_min, amount_max, term_months_min, term_months_max,
            fee_open, fee_service, early_withdraw, capitalization, replenishable,
            conditions, raw, source_snapshot_id, filter_context_id, digest)
        VALUES (:o,:r,:rk,:cur,:amn,:amx,:tmn,:tmx,:fo,:fs,:ew,:cap,:rep,
                :cond, CAST(:raw AS jsonb), :ssid, :fid, :dg)
        RETURNING terms_id
    """), {
        "o": offer_id, "r": d.rate_pct, "rk": d.rate_kind, "cur": d.currency,
        "amn": d.amount_min, "amx": d.amount_max,
        "tmn": d.term_months_min, "tmx": d.term_months_max,
        "fo": d.fee_open, "fs": d.fee_service,
        "ew": d.early_withdraw, "cap": d.capitalization, "rep": d.replenishable,
        "cond": d.conditions, "raw": json.dumps(d.raw, ensure_ascii=False, default=str),
        "ssid": snapshot_id, "fid": source_page_id, "dg": new_digest,
    }).scalar_one()

    if cur:
        # diff
        prev = session.execute(text("""
            SELECT rate_pct, rate_kind, currency, amount_min, amount_max,
                   term_months_min, term_months_max, fee_open, fee_service,
                   early_withdraw, capitalization, replenishable, conditions
              FROM product_terms WHERE terms_id=:t
        """), {"t": cur[0]}).mappings().one()
        new_vals = {
            "rate_pct": d.rate_pct, "rate_kind": d.rate_kind, "currency": d.currency,
            "amount_min": d.amount_min, "amount_max": d.amount_max,
            "term_months_min": d.term_months_min, "term_months_max": d.term_months_max,
            "fee_open": d.fee_open, "fee_service": d.fee_service,
            "early_withdraw": d.early_withdraw, "capitalization": d.capitalization,
            "replenishable": d.replenishable, "conditions": d.conditions,
        }
        # Порог значимости: дрожь расчётных ставок в 3-4-м знаке (3.8544→3.8549)
        # — не событие; она давала ~12k мусорных строк/нед («14 тыс. изменений»
        # из фидбека аналитиков). Числовые поля сравниваем с допуском.
        _num_eps = {"rate_pct": 0.01, "fee_open": 0.5, "fee_service": 0.5,
                    "amount_min": 1.0, "amount_max": 1.0}

        def _same(k, a, b):
            if a is None or b is None:
                return a is b
            eps = _num_eps.get(k)
            if eps is not None:
                try:
                    return abs(float(a) - float(b)) < eps
                except (TypeError, ValueError):
                    pass
            return str(a) == str(b)

        diff = {k: {"from": str(prev[k]) if prev[k] is not None else None,
                    "to": str(v) if v is not None else None}
                for k, v in new_vals.items() if not _same(k, prev[k], v)}
        if diff:                       # пустой дифф (только шум) — не событие
            session.execute(text("""
                INSERT INTO change_history(offer_id, prev_terms_id, new_terms_id, diff)
                VALUES (:o,:p,:n, CAST(:d AS jsonb))
            """), {"o": offer_id, "p": cur[0], "n": new_id,
                   "d": json.dumps(diff, ensure_ascii=False)})
    return offer_id, True

# Категории ежедневного sravni-сбора: только для них применимо протухание
# (bank_rating/npf/invest_broker собираются другими источниками и редко)
_DAILY_CATEGORIES = ("deposit", "credit", "mortgage", "card_credit",
                     "card_debit", "auto_loan", "metals", "microloan")


def expire_stale_offers(days: int = 3) -> int:
    """Деактивирует офферы, пропавшие из выдачи источника (аудит 22.07.2026:
    394 «вечно живых» вклада и весь metals с данными от 10 июня висели в
    витрине как актуальные). Вернувшийся оффер оживает в upsert_offer."""
    with db.session() as s:
        n = s.execute(text("""
            UPDATE product_offer SET is_active = false
             WHERE is_active
               AND category = ANY(CAST(:cats AS product_category[]))
               AND last_seen < now() - make_interval(days => :d)
        """), {"cats": list(_DAILY_CATEGORIES), "d": days}).rowcount
    log.info("[expire] деактивировано протухших офферов: %d", n)
    return n


def validate_offer_urls(limit: int = 80) -> dict:
    """HEAD/GET-проба ссылок активных офферов (случайная ротация — за пару дней
    прочёсывается весь пул): 404/410 → url=NULL, фронт покажет оффер без ссылки.
    Фикс жалобы аналитиков: клик по офферу (кейс ПСБ) вёл на 404."""
    import httpx
    from sqlalchemy import text as _t
    rows = []
    with db.session() as s:
        rows = [dict(r) for r in s.execute(_t("""
            SELECT offer_id, url FROM product_offer
            WHERE url IS NOT NULL AND is_active
            ORDER BY random() LIMIT :l"""), {"l": limit}).mappings().all()]
    bad: list[int] = []
    checked = 0
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
    with httpx.Client(timeout=6.0, follow_redirects=True, headers=ua) as c:
        for r in rows:
            checked += 1
            try:
                resp = c.head(r["url"])
                if resp.status_code in (403, 405):     # HEAD не любят — добиваем GET
                    resp = c.get(r["url"])
                if resp.status_code in (404, 410):
                    bad.append(r["offer_id"])
            except Exception:  # noqa: BLE001 — сетевой флак ≠ битая ссылка
                continue
    if bad:
        with db.session() as s:
            s.execute(_t("UPDATE product_offer SET url = NULL "
                         "WHERE offer_id = ANY(:ids)"), {"ids": bad})
    log.info("[url-check] проверено %d, битых %d", checked, len(bad))
    return {"checked": checked, "dead": len(bad)}


def normalize_batch(drafts: Iterable[OfferDraft], snapshot_id: int | None,
                    source_page_id: int | None) -> dict:
    written = 0
    seen = 0
    with db.session() as s:
        for d in drafts:
            seen += 1
            _, changed = upsert_offer(s, d, snapshot_id, source_page_id)
            if changed:
                written += 1
    return {"seen": seen, "written": written}
