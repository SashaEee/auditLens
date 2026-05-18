"""Нормализация черновиков офферов в нормализованную модель + SCD2 + change_history.
   Работает идемпотентно: повторный запуск без изменений данных не создаёт новых строк."""
from __future__ import annotations
import json
from decimal import Decimal
from typing import Iterable
from sqlalchemy import text
from rapidfuzz import process, fuzz
from .. import db
from ..hashing import stable_digest
from ..models import OfferDraft
from .rules import BANK_ALIASES, SBER_SLUGS, normalize_bank_key

NORMALIZE_FIELDS = (
    "rate_pct", "rate_kind", "currency",
    "amount_min", "amount_max", "term_months_min", "term_months_max",
    "fee_open", "fee_service",
    "early_withdraw", "capitalization", "replenishable",
    "conditions",
)

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
        # fuzzy
        match = process.extractOne(key, list(BANK_ALIASES.keys()), scorer=fuzz.WRatio)
        if match and match[1] >= 88:
            slug = BANK_ALIASES[match[0]]
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
          SET last_seen=now(), title=EXCLUDED.title, url=COALESCE(EXCLUDED.url, product_offer.url)
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
        diff = {k: {"from": str(prev[k]) if prev[k] is not None else None,
                    "to": str(v) if v is not None else None}
                for k, v in new_vals.items() if str(prev[k]) != str(v)}
        session.execute(text("""
            INSERT INTO change_history(offer_id, prev_terms_id, new_terms_id, diff)
            VALUES (:o,:p,:n, CAST(:d AS jsonb))
        """), {"o": offer_id, "p": cur[0], "n": new_id,
               "d": json.dumps(diff, ensure_ascii=False)})
    return offer_id, True

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
