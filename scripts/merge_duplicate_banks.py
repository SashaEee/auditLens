"""Слияние банков, заведённых дважды под разными написаниями.

ЗАЧЕМ. Источники пишут название по-своему: «ОТП Банк», «ОТП БАНК», «АО "ОТП
Банк"». Нормализатор узнаёт только те написания, что есть в BANK_ALIASES, а
остальные заводит новой строкой со slug вида unknown_<хеш>. Один банк двумя
строками — это не косметика: его предложения расщепляются (в каждой строке своё
«лучшее»), а на витрине «Рынок» он считается двумя игроками, то есть искажает
знаменатель ранга Сбера.

ЧТО ДЕЛАЕТ. Группирует банки по имени, очищенному от всего, кроме букв и цифр.
В группе выбирает КАНОНИЧЕСКУЮ строку (со slug без префикса unknown_, при
равенстве — с большим числом активных предложений), переносит на неё
product_offer и review, забирает алиасы и признак Сбера, удаляет пустышку.

БЕЗОПАСНОСТЬ. По умолчанию — сухой прогон, только отчёт. Запись включается
флагом --apply и идёт одной транзакцией. Строку банка удаляем только после
того, как на ней не осталось ни одной ссылки.

  .venv/bin/python scripts/merge_duplicate_banks.py            # показать
  .venv/bin/python scripts/merge_duplicate_banks.py --apply    # слить
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import text  # noqa: E402

from bank_audit import db  # noqa: E402
from bank_audit.config import Settings  # noqa: E402
from bank_audit.normalizer.offers import bank_slug_for  # noqa: E402

_GROUPS = """
    SELECT lower(regexp_replace(name, '[^[:alnum:]]', '', 'g')) AS key,
           array_agg(bank_id ORDER BY bank_id) AS ids
      FROM bank
     WHERE length(regexp_replace(name, '[^[:alnum:]]', '', 'g')) > 2
     GROUP BY 1
    HAVING count(*) > 1
"""

_ROW = """
    SELECT b.bank_id, b.slug, b.name, b.is_sber, b.aliases,
           (SELECT count(*) FROM product_offer o
             WHERE o.bank_id = b.bank_id AND o.is_active) AS offers,
           (SELECT count(*) FROM review r WHERE r.bank_id = b.bank_id) AS reviews
      FROM bank b WHERE b.bank_id = ANY(:ids)
"""


def _rank(r, target: str | None) -> tuple:
    """Канонический — прежде всего ТОТ, КУДА БУДЕТ ПИСАТЬ СЛЕДУЮЩИЙ СБОР.

    Иначе слияние бессмысленно: нормализатор заводит строку по своему правилу
    (алиасы + fuzzy + unknown_<хеш ключа>), и уже завтра дубль воскресает под
    тем же именем. При прочих равных берём читаемый slug и больше данных.
    """
    return (1 if r["slug"] == target else 0,
            0 if str(r["slug"]).startswith("unknown_") else 1,
            r["offers"], r["reviews"])


def main(apply: bool = False) -> int:
    db.init(Settings.load())
    merged = moved_offers = moved_reviews = 0
    with db.session() as s:
        groups = s.execute(text(_GROUPS)).mappings().all()
        print(f"групп с одинаковым названием: {len(groups)}")
        for g in groups:
            rows = s.execute(text(_ROW), {"ids": list(g["ids"])}).mappings().all()
            # куда попадёт следующий сбор с таким именем
            targets = {bank_slug_for(s, r["name"]) for r in rows}
            target = next(iter(targets)) if len(targets) == 1 else None
            keep = max(rows, key=lambda r: _rank(r, target))
            drop = [r for r in rows if r["bank_id"] != keep["bank_id"]]
            note = ""
            if target and target != keep["slug"]:
                # Целевого slug нет ни у одной строки (например «Дальневосточный
                # Банк» лежит под чужим slug vostochny). Переименовываем ту,
                # что оставляем, — иначе завтра появится третья.
                note = f" · slug → {target}"
            print(f"  {keep['name']}  ← оставляем {keep['slug']} "
                  f"({keep['offers']} офферов, {keep['reviews']} отзывов){note}")
            for d in drop:
                print(f"      сливаем {d['slug']}: {d['offers']} офферов, "
                      f"{d['reviews']} отзывов")
                if not apply:
                    continue
                # Уникальный ключ оффера — (bank_id, category, external_id):
                # при столкновении переносить нельзя, дубль гасим как неактивный.
                moved_offers += s.execute(text("""
                    UPDATE product_offer o SET bank_id = :keep
                     WHERE o.bank_id = :drop
                       AND NOT EXISTS (SELECT 1 FROM product_offer x
                                        WHERE x.bank_id = :keep
                                          AND x.category = o.category
                                          AND x.external_id = o.external_id)
                """), {"keep": keep["bank_id"], "drop": d["bank_id"]}).rowcount
                s.execute(text("UPDATE product_offer SET is_active = FALSE "
                               "WHERE bank_id = :drop"), {"drop": d["bank_id"]})
                moved_reviews += s.execute(text(
                    "UPDATE review SET bank_id = :keep WHERE bank_id = :drop"),
                    {"keep": keep["bank_id"], "drop": d["bank_id"]}).rowcount
                # написание сливаемой строки сохраняем алиасом: следующий сбор
                # с тем же названием попадёт сразу в канонический банк
                s.execute(text("""
                    UPDATE bank SET aliases = (
                        SELECT array_agg(DISTINCT a) FROM unnest(
                            aliases || CAST(:add AS text[]) ) a),
                        is_sber = is_sber OR :sber
                     WHERE bank_id = :keep
                """), {"keep": keep["bank_id"], "sber": bool(d["is_sber"]),
                       "add": list({d["name"], *(d["aliases"] or [])})})
                left = s.execute(text("""
                    SELECT (SELECT count(*) FROM product_offer WHERE bank_id = :d)
                         + (SELECT count(*) FROM review WHERE bank_id = :d)
                """), {"d": d["bank_id"]}).scalar()
                if left == 0:
                    s.execute(text("DELETE FROM bank WHERE bank_id = :d"),
                              {"d": d["bank_id"]})
                merged += 1
            if apply and target and target != keep["slug"]:
                free = s.execute(text("SELECT 1 FROM bank WHERE slug = :s"),
                                 {"s": target}).first() is None
                if free:
                    s.execute(text("UPDATE bank SET slug = :s WHERE bank_id = :b"),
                              {"s": target, "b": keep["bank_id"]})
    print(f"\n{'СЛИТО' if apply else 'СУХОЙ ПРОГОН'}: групп {len(groups)}, "
          f"строк слито {merged}, перенесено офферов {moved_offers}, "
          f"отзывов {moved_reviews}")
    if not apply:
        print("записи не было — повторите с --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
