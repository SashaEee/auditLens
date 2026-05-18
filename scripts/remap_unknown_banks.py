"""Одноразовая миграция: свести bank.slug='unknown_*' к каноническим slug'ам.

Логика:
  1. Берём все bank где slug LIKE 'unknown_%'
  2. normalize_bank_key(name) → BANK_ALIASES → правильный slug
  3. Если правильный slug найден — переписываем все ссылки
     (product_offer.bank_id, review.bank_id) на bank_id канонического банка,
     старую строку удаляем.

Запуск: python -m scripts.remap_unknown_banks   (или этот файл напрямую)
Безопасен: всё в одной транзакции, при ошибке — rollback.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text
from rapidfuzz import process, fuzz

from bank_audit import db
from bank_audit.config import Settings
from bank_audit.normalizer.rules import BANK_ALIASES, normalize_bank_key, SBER_SLUGS


def main(dry_run: bool = False):
    db.init(Settings.load())
    moved = 0
    deleted = 0
    skipped = 0
    with db.session() as s:
        rows = s.execute(text("""
            SELECT bank_id, slug, name
              FROM bank
             WHERE slug LIKE 'unknown_%'
        """)).mappings().all()
        print(f"unknown banks: {len(rows)}")

        # Цикл по каждому unknown — пытаемся найти канонический slug
        for r in rows:
            raw = r["name"] or ""
            key = normalize_bank_key(raw)
            slug = BANK_ALIASES.get(key)
            if not slug and key:
                m = process.extractOne(key, list(BANK_ALIASES.keys()), scorer=fuzz.WRatio)
                if m and m[1] >= 90:
                    slug = BANK_ALIASES[m[0]]
            if not slug:
                skipped += 1
                continue
            if slug == r["slug"]:
                # Уже правильный
                skipped += 1
                continue
            # Получаем/создаём канонический bank
            target = s.execute(text("SELECT bank_id FROM bank WHERE slug=:s"),
                               {"s": slug}).first()
            if not target:
                target_id = s.execute(text("""
                    INSERT INTO bank(slug, name, is_sber) VALUES (:s,:n,:is)
                    RETURNING bank_id
                """), {"s": slug, "n": raw, "is": slug in SBER_SLUGS}).scalar_one()
            else:
                target_id = target[0]

            if target_id == r["bank_id"]:
                continue

            # 1. Сначала удаляем offers/terms-дубликаты у unknown-банка,
            #    которые уже есть в каноническом (по UNIQUE
            #    (bank_id, category, external_id)). Порядок: change_history
            #    (FK на terms) → product_terms → product_offer.
            #    CTE-выражение находит дубль-офферы один раз.
            params = {"t": target_id, "o": r["bank_id"]}
            dup_offers_sql = """
                SELECT po_old.offer_id
                  FROM product_offer po_old
                  JOIN product_offer po_new
                    ON po_new.bank_id      = :t
                   AND po_new.category     = po_old.category
                   AND po_new.external_id  = po_old.external_id
                 WHERE po_old.bank_id      = :o
            """
            s.execute(text(f"""
                DELETE FROM change_history
                 WHERE offer_id IN ({dup_offers_sql})
            """), params)
            s.execute(text(f"""
                DELETE FROM product_terms
                 WHERE offer_id IN ({dup_offers_sql})
            """), params)
            s.execute(text(f"""
                DELETE FROM product_offer
                 WHERE offer_id IN ({dup_offers_sql})
            """), params)

            # 2. Переносим оставшиеся offers и reviews
            s.execute(text("UPDATE product_offer SET bank_id=:t WHERE bank_id=:o"),
                      {"t": target_id, "o": r["bank_id"]})
            s.execute(text("UPDATE review SET bank_id=:t WHERE bank_id=:o"),
                      {"t": target_id, "o": r["bank_id"]})

            # 3. Удаляем старую строку bank
            s.execute(text("DELETE FROM bank WHERE bank_id=:o"), {"o": r["bank_id"]})
            moved += 1
            deleted += 1

        if dry_run:
            print(f"DRY RUN: would-move={moved} would-delete={deleted} skipped={skipped}")
            s.rollback()
        else:
            s.commit()
            print(f"DONE: moved={moved} deleted={deleted} skipped={skipped}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    main(dry_run=args.dry)
