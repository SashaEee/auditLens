"""Проверки разбора ответа модели в normalizer/enrich_llm.

Сеть и LLM здесь не нужны: проверяем ровно то, что ломалось при разработке —
потерю всех элементов батча кроме первого и доверие к пересказу вместо цифры.

Запуск:  .venv/bin/python scripts/_test_enrich_llm.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bank_audit.normalizer.enrich_llm import (_clean_item, _free_by_rule,  # noqa: E402
                                              _parse_array)

ok = fail = 0


def check(name: str, got, want) -> None:
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name}\n      получено: {got!r}\n      ожидалось: {want!r}")


print("разбор ответа модели")
# именно на этом терялись 7 продуктов из 8: _loose_json_loads возвращал
# ПЕРВЫЙ сбалансированный объект, а не весь массив
arr = '[{"i":0,"free_kind":"paid"},{"i":1,"free_kind":"unconditional"}]'
check("массив целиком", len(_parse_array(arr)), 2)
check("массив в ```-заборе", len(_parse_array("```json\n" + arr + "\n```")), 2)
check("массив с прозой вокруг", len(_parse_array("Вот ответ:\n" + arr + "\nГотово")), 2)
check("одиночный объект", len(_parse_array('{"i":0,"free_kind":"paid"}')), 1)
check("пустой ответ не падает", _parse_array(""), [])
check("мусор не падает", _parse_array("не могу ответить"), [])

# ответ мог оборваться на лимите токенов — целые объекты обязаны уцелеть
trunc = '[{"i":0,"free_kind":"paid"},{"i":1,"free_kind":"unconditional"},{"i":2,"free_ki'
check("обрезанный массив: спасаем целые", [o.get("i") for o in _parse_array(trunc)], [0, 1])
check("вложенный объект не считается отдельным",
      [o.get("i") for o in _parse_array('[{"i":0,"free_conditions":[{"type":"balance"}]},{"i":1}]')],
      [0, 1])
check("скобка внутри строки не ломает разбор",
      [o.get("i") for o in _parse_array('[{"i":0,"note":"а {это} не объект"},{"i":1}]')], [0, 1])

print("нормализация полей")
raw = {"i": 0, "free_kind": "БЕСПЛАТНО", "rate_attainability": "very narrow",
       "client_segment": "vip", "rate_requires": ["payroll", "магия", "insurance"],
       "free_conditions": [{"type": "оборот", "threshold_rub": "100000", "note": "x" * 200}],
       "cashback_max_pct": "5.5", "product_kind": None}
got = _clean_item(raw, "card_debit")
check("неизвестное значение перечисления → unknown", got["free_kind"], "unknown")
check("неизвестная достижимость → unknown", got["rate_attainability"], "unknown")
check("неизвестный сегмент → unknown", got["client_segment"], "unknown")
check("выдуманное требование выброшено", got["rate_requires"], ["insurance", "payroll"])
check("неизвестный тип условия → other", got["free_conditions"][0]["type"], "other")
check("порог приведён к числу", got["free_conditions"][0]["threshold_rub"], 100000.0)
check("примечание обрезано", len(got["free_conditions"][0]["note"]), 90)
check("кэшбэк приведён к числу", got["cashback_max_pct"], 5.5)

print("защита от пересказа вместо цифры")
# модель переносит условия из блока кэшбэка в плату за обслуживание —
# так пенсионная СберКарта получала «бесплатно при подписке СберПрайм»
cond_no_items = _clean_item({"free_kind": "conditional", "free_conditions": []}, "card_debit")
check("«при условии» без условий → unknown", cond_no_items["free_kind"], "unknown")
paid = _clean_item({"free_kind": "unconditional"}, "card_debit", fee=7000.0)
check("цена 7000 против «бесплатно всегда» → платно", paid["free_kind"], "paid")
check("конфликт помечен", paid.get("conflict"), "fee_gt0_vs_unconditional")
zero = _clean_item({"free_kind": "paid"}, "card_debit", fee=0.0)
check("цена 0 против «платно» → unknown", zero["free_kind"], "unknown")
keep = _clean_item({"free_kind": "conditional",
                    "free_conditions": [{"type": "balance", "threshold_rub": 100000}]},
                   "card_debit", fee=0.0)
check("«ноль при условии» цифрой не ломается", keep["free_kind"], "conditional")
loan = _clean_item({"free_kind": "unconditional"}, "credit")
check("у кредита платы за обслуживание нет", loan["free_kind"], "unknown")

print("детерминированный дожим по картам")
FREE = "Общие условия Выпуск карты бесплатно Обслуживание карты бесплатно Процент на остаток 11%"
COND = ("Общие условия Обслуживание карты бесплатно при неснижаемом остатке от "
        "100 000 ₽ за расчётный период, иначе 299 ₽/мес.")
YEAR = "Обслуживание карты бесплатно первый год, далее — 7 000 ₽ ежегодно"
NOISE = "Обслуживание карты бесплатно 900 ₽ — изменение даты платежа по кредиту"
check("прямое «бесплатно» при цене 0", _free_by_rule(FREE, 0.0), "unconditional")
check("оговорка «при остатке» не проходит", _free_by_rule(COND, 0.0), None)
check("«первый год, далее» не проходит", _free_by_rule(YEAR, 0.0), None)
check("чужая плата рядом не мешает", _free_by_rule(NOISE, 0.0), "unconditional")
check("цена больше нуля не проходит", _free_by_rule(FREE, 7000.0), None)
check("цена неизвестна не проходит", _free_by_rule(FREE, None), None)
check("нет строки обслуживания", _free_by_rule("Ставка 20 проц. Сумма до 1 млн", 0.0), None)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
