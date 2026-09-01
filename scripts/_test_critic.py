"""Проверки критика (этап 1 обратной связи ТБ).

Главное, что здесь ловится: выдуманное утверждение НЕ должно доходить до
отчёта, а честный пересказ — не должен выбрасываться. Плюс два структурных
правила: норма обязана приходить от регулятора, число значения — встречаться
в источнике.

Запуск:  .venv/bin/python scripts/_test_critic.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bank_audit.research.gptr.critic import (  # noqa: E402
    CLOSE, EXACT, UNSUPPORTED, review)
from bank_audit.research.gptr.facts import FactRegistry  # noqa: E402

ok = fail = 0


def chk(name, cond):
    global ok, fail
    ok += bool(cond); fail += not cond
    print(("  ✓ " if cond else "  ✗ ") + name)


PAGE = ("Ставка по вкладу составляет 16,5% годовых при сумме от 100 000 рублей. "
        "Невостребованная карта ждёт клиента в банке четыре месяца. "
        "Доставка курьером осуществляется в тот же день. ") * 12
CBR = ("Полная стоимость кредита размещается в квадратных рамках в правом "
       "верхнем углу первой страницы договора. ") * 12
PAGES = {"https://sberbank.ru/p": PAGE, "https://cbr.ru/doc": CBR,
         "https://banki.ru/otzyv": "клиент жалуется на списание 40 рублей"}


def one(**kw):
    r = FactRegistry()
    r.add(**{"subject": "sberbank", "attribute": "ставка", "value": "16,5",
             "unit": "%", "stance": "declared", **kw})
    return r


print("выдумка не доходит до отчёта")
r = one(verbatim="Указание Банка России № 6543-У устанавливает предел ПСК",
        url="https://sberbank.ru/p")
v = review(r, PAGES)
chk("несуществующая цитата снята", v.cut == 1 and not r.facts)
chk("снятое посчитано и объяснено", bool(v.notes) and v.cut_ids)

print("честный пересказ остаётся")
r = one(verbatim="годовых 16,5% составляет по вкладу ставка",
        url="https://sberbank.ru/p")
v = review(r, PAGES)
chk("перестановка слов — не выдумка", v.close == 1 and len(r.facts) == 1)
chk("помечена как «близко к тексту»", r.facts[0].support == CLOSE)

print("дословная цитата проходит как есть")
r = one(verbatim="Ставка по вкладу составляет 16,5% годовых",
        url="https://sberbank.ru/p")
v = review(r, PAGES)
chk("вердикт «дословно»", v.exact == 1 and r.facts[0].support == EXACT)

print("сумма с разрядами не считается выдуманной")
r = one(value="100000", unit="₽",
        verbatim="при сумме от 100 000 рублей", url="https://sberbank.ru/p")
v = review(r, PAGES)
chk("«100 000» в тексте = «100000» в значении", v.invented_numbers == 0)

print("число, подставленное рядом с подлинной цитатой")
r = one(value="18,9", verbatim="Ставка по вкладу составляет 16,5% годовых",
        url="https://sberbank.ru/p")
v = review(r, PAGES)
chk("чужое число помечено", v.invented_numbers == 1)
chk("но факт не выброшен — цитата подлинная", len(r.facts) == 1)

print("норма обязана приходить от регулятора")
r = one(attribute="норма", value="ПСК", unit="", stance="regulatory",
        verbatim="Полная стоимость кредита размещается в квадратных рамках",
        url="https://cbr.ru/doc")
v = review(r, PAGES)
chk("с cbr.ru — остаётся нормой",
    v.mislabeled == 0 and r.facts[0].stance == "regulatory")
r = one(attribute="норма", value="ПСК", unit="", stance="regulatory",
        verbatim="Ставка по вкладу составляет 16,5% годовых",
        url="https://sberbank.ru/p")
v = review(r, PAGES)
chk("с сайта банка — не норма, а взгляд со стороны",
    v.mislabeled == 1 and r.facts[0].stance == "observed")

print("короткая цитата ничего не подтверждает")
r = one(verbatim="16,5%", url="https://sberbank.ru/p")
v = review(r, PAGES)
chk("«16,5%» найдётся везде — снято", v.cut == 1)

print("критик не замедляет прогон")
big = FactRegistry()
for i in range(500):
    big.add(subject="sberbank", attribute="ставка", value="16,5", unit="%",
            verbatim=("Ставка по вкладу составляет 16,5% годовых" if i % 3
                      else "Указание № 999-У вводит новый предел"),
            url="https://sberbank.ru/p", stance="declared")
t0 = time.perf_counter()
v = review(big, PAGES)
dt = time.perf_counter() - t0
chk(f"500 фактов за {dt*1000:.0f} мс (порог 500 мс)", dt < 0.5)
chk("на большом объёме выдумки тоже сняты", v.cut > 100)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
