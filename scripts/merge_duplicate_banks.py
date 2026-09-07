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


_FK_REFS = """
    SELECT c.conrelid::regclass::text AS tbl, a.attname AS col
      FROM pg_constraint c
      JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
     WHERE c.contype = 'f' AND c.confrelid = 'bank'::regclass
"""


_OFFER_REFS = """
    SELECT c.conrelid::regclass::text AS tbl, a.attname AS col
      FROM pg_constraint c
      JOIN unnest(c.conkey) k(attnum) ON true
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
     WHERE c.contype = 'f'
       AND c.confrelid = 'product_offer'::regclass
"""


def _relink_history(s, tbl: str, col: str, rid, keep_id: int) -> None:
    """Переводит зависимые записи с удаляемой строки на её канонический двойник.

    Двойник ищется по естественному ключу (категория + внешний идентификатор),
    а не по номеру: номер у копии свой, а сама она — тот же самый оффер,
    собранный дважды под разными написаниями имени банка.
    """
    if tbl != "product_offer":
        return
    row = s.execute(text(
        "SELECT offer_id, category, external_id FROM product_offer "
        "WHERE ctid = :r"), {"r": rid}).mappings().first()
    if not row:
        return
    twin = s.execute(text(
        "SELECT offer_id FROM product_offer "
        " WHERE bank_id = :k AND category = :c AND external_id = :e"),
        {"k": keep_id, "c": row["category"], "e": row["external_id"]}).scalar()
    if not twin:
        return
    for dep_tbl, dep_col in s.execute(text(_OFFER_REFS)).all():
        s.execute(text(f"UPDATE {dep_tbl} SET {dep_col} = :t WHERE {dep_col} = :o"),
                  {"t": twin, "o": row["offer_id"]})


def _repoint_all(s, drop_id: int, keep_id: int) -> int:
    """Перевести на канонический банк ВСЕ ссылки по внешним ключам.

    Возвращает число ссылок, которые перенести не удалось (уникальные ключи:
    у канонического банка уже есть своя строка профиля или сводки). Такие
    строки остаются на месте, и тогда банк не удаляем — данные дороже чистоты
    справочника.
    """
    stuck = 0
    for tbl, col in s.execute(text(_FK_REFS)).all():
        try:
            with s.begin_nested():
                s.execute(text(f"UPDATE {tbl} SET {col} = :k WHERE {col} = :d"),
                          {"k": keep_id, "d": drop_id})
        except Exception as e:  # noqa: BLE001 — конфликт уникальности ожидаем
            # Уникальный ключ сработал = ТАКАЯ ЖЕ строка уже есть у канонического
            # банка: это тот же оффер (или отзыв), собранный дважды под разными
            # написаниями имени. Переносить нечего — дубликат удаляем поштучно,
            # иначе строка банка остаётся навечно из-за копии самой себя.
            print(f"        {tbl}.{col}: перенос не удался ({str(e).splitlines()[0][:60]})")
            moved = dropped = 0
            for (rid,) in s.execute(text(
                    f"SELECT ctid FROM {tbl} WHERE {col} = :d"), {"d": drop_id}).all():
                try:
                    with s.begin_nested():
                        s.execute(text(f"UPDATE {tbl} SET {col} = :k "
                                       f"WHERE ctid = :r"), {"k": keep_id, "r": rid})
                    moved += 1
                except Exception:
                    # Дубликат нельзя просто удалить: на оффер ссылается
                    # история изменений. Переводим историю на ту строку, что
                    # осталась у канонического банка, и лишь потом удаляем —
                    # иначе теряется след того, как менялись условия.
                    try:
                        with s.begin_nested():
                            _relink_history(s, tbl, col, rid, keep_id)
                            s.execute(text(f"DELETE FROM {tbl} WHERE ctid = :r"),
                                      {"r": rid})
                        dropped += 1
                    except Exception as e2:  # noqa: BLE001
                        print(f"          строка оставлена: "
                              f"{str(e2).splitlines()[0][:70]}")
            if moved or dropped:
                print(f"          поштучно: перенесено {moved}, "
                      f"удалено дубликатов {dropped}")
        stuck += s.execute(text(f"SELECT count(*) FROM {tbl} WHERE {col} = :d"),
                           {"d": drop_id}).scalar() or 0
    return stuck


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
    merged = moved_offers = moved_reviews = revived = 0
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
                # при столкновении перенести строку нельзя.
                moved_offers += s.execute(text("""
                    UPDATE product_offer o SET bank_id = :keep
                     WHERE o.bank_id = :drop
                       AND NOT EXISTS (SELECT 1 FROM product_offer x
                                        WHERE x.bank_id = :keep
                                          AND x.category = o.category
                                          AND x.external_id = o.external_id)
                """), {"keep": keep["bank_id"], "drop": d["bank_id"]}).rowcount
                # Столкнувшиеся: живой продукт мог остаться именно у сливаемой
                # строки, а на канонической лежать её потухшая копия (источник
                # сменил написание имени, и пять дней всё писалось в дубль).
                # Слепое гашение убило бы ЖИВЫЕ предложения — сначала поднимаем
                # канонический двойник, и только потом гасим дубль.
                revived += s.execute(text("""
                    UPDATE product_offer c
                       SET is_active = TRUE,
                           last_seen = GREATEST(c.last_seen, d.last_seen)
                      FROM product_offer d
                     WHERE c.bank_id = :keep AND d.bank_id = :drop
                       AND c.category = d.category
                       AND c.external_id = d.external_id
                       AND d.is_active AND NOT c.is_active
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
                # На банк ссылаются не только офферы и отзывы: есть профиль с
                # адресом сайта и картой страниц, признаки, сводки отзывов,
                # документы. DELETE увёл бы их каскадом, а проверка «ссылок не
                # осталось» по двум таблицам этого не заметила бы. Поэтому
                # переносим ВСЕ ссылки по внешним ключам, а удаляем строку
                # только если после этого на неё никто не ссылается.
                left = _repoint_all(s, d["bank_id"], keep["bank_id"])
                if left:
                    print(f"        строка оставлена: ссылок осталось {left}")
                else:
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
          f"отзывов {moved_reviews}, поднято потухших двойников {revived}")
    if not apply:
        print("записи не было — повторите с --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
