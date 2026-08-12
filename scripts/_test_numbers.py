"""Проверки единого разбора чисел (research/v2/numbers.py).

Ловят ровно то, из-за чего сверка работала наоборот: потерю десятичной запятой
в фактах и «безопасные годы», под которые попадала рублёвая сумма.

Запуск:  .venv/bin/python scripts/_test_numbers.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bank_audit.research.v2 import numbers as N  # noqa: E402

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name}\n      получено: {got!r}\n      ожидалось: {want!r}")


class F:                                   # заглушка факта bundle
    def __init__(self, value="", conditions=None, verbatim="", as_of=""):
        self.value, self.conditions = value, conditions or []
        self.verbatim, self.as_of = verbatim, as_of


print("числа из фактов (тут и ломалось)")
check("дробь через запятую сохраняется", 27.608 in N.all_numbers("ПСК 27,608% годовых"), True)
check("старое поведение (27608) больше не появляется",
      27608.0 in N.all_numbers("ПСК 27,608% годовых"), False)
check("разряды пробелом", N.all_numbers("2 000 ₽") == {2000.0}, True)
check("диапазон даёт обе границы",
      {23.305, 41.062} <= N.all_numbers("ПСК 23,305–41,062%"), True)
check("дробь через точку", 17.7 in N.all_numbers("ставка 17.7%"), True)
check("из даты берём только год",
      N.all_numbers("действует с 01.10.2025") == {2025.0}, True)
check("голая величина в условиях", 100000.0 in N.all_numbers("при сумме от 100 000"), True)

facts = [F(value="27,608%", verbatim="предельное 41,062% годовых"),
         F(value="0,5%, макс 1500 ₽", conditions=["при сумме от 100 000"])]
base = N.numbers_from_facts(facts)
check("база собрана из значения, цитаты и условий",
      {27.608, 41.062, 0.5, 1500.0, 100000.0} <= base, True)

print("числа из текста отчёта")
pairs = N.parse_with_units("ПСК 27,608% против потолка 41,062%. Комиссия 2 000 ₽. Снова 27,608%.")
check("порядок и повторы сохранены", [p[0] for p in pairs],
      [27.608, 41.062, 2000.0, 27.608])
check("единица распознана", pairs[2][1], "₽")

print("сверка отчёта с фактами")
verified, unverified = N.split_verified(pairs, base)
check("верные числа ЦБ теперь подтверждаются", sorted(set(verified)), [27.608, 41.062])
check("выдуманная сумма больше не проходит как «год»", unverified, [2000.0])
check("повтор считается вхождением, а не схлопывается", len(verified), 3)

print("годы")
check("«с 2025 года» — год", N.is_year(2025.0, "года"), True)
check("«2 000 ₽» — не год", N.is_year(2000.0, "₽"), False)
check("«2025 ₽» — не год", N.is_year(2025.0, "₽"), False)
y_pairs = N.parse_with_units("с 2025 года ставка 12,5%")
vy, uy = N.split_verified(y_pairs, {12.5})
check("год не попадает в несверенные", uy, [])

print("устойчивость")
check("пустой текст", N.all_numbers(""), set())
check("текст без чисел", N.parse_with_units("никаких цифр"), [])
check("число без единицы в отчёт не идёт", N.parse_with_units("просто 42"), [])

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
