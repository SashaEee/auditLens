"""Проверки слоя фактов (этап 1).

Главное, что здесь ловится: факт без дословной опоры в источнике не должен
проходить ни при каких обстоятельствах — иначе весь смысл слоя теряется.
Остальное — нормализация (одна и та же цитата в HTML и в ответе модели
выглядит по-разному) и структурное определение стороны доказательства.

Запуск:  .venv/bin/python scripts/_test_facts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bank_audit.research.gptr.facts import (  # noqa: E402
    FactRegistry, stance_for, verbatim_found)

ok = fail = 0


def chk(name, cond):
    global ok, fail
    ok += bool(cond); fail += not cond
    print(("  ✓ " if cond else "  ✗ ") + name)


PAGE = ("Дебетовая карта СберКарта Мир\n"
        "Ставка на остаток — 16,5% годовых при сумме от 100 000 ₽.\n"
        "Невостребованная карта ждёт клиента в банке четыре месяца.\n"
        "Доставка курьером в тот же день для моментальной карты.\n")

print("слой извлечения ничего не отбраковывает — это дело критика")
chk("точная цитата опознаётся как дословная",
    verbatim_found("Ставка на остаток — 16,5% годовых", PAGE))
chk("выдуманное число дословным не считается",
    not verbatim_found("Ставка на остаток — 18,9% годовых", PAGE))

print("нормализация: тот же текст, другая запись")
chk("неразрывный пробел",
    verbatim_found("при сумме от 100 000 ₽", PAGE))
chk("ё вместо е",
    verbatim_found("Невостребованная карта ждет клиента", PAGE))
chk("рубли словом вместо знака",
    verbatim_found("при сумме от 100 000 рублей", PAGE))
chk("другое тире",
    verbatim_found("Ставка на остаток – 16,5% годовых", PAGE))

print("сторона доказательства — по владельцу домена, а не по словам")
DOM = {"sberbank": "sberbank.ru", "tinkoff": "tbank.ru"}
chk("сайт банка = заявлено",
    stance_for("https://www.sberbank.ru/ru/person/debit", "sberbank", DOM) == "declared")
chk("поддомен банка = заявлено",
    stance_for("https://online.sberbank.ru/x", "sberbank", DOM) == "declared")
chk("жалоба на агрегаторе = наблюдается",
    stance_for("https://www.banki.ru/services/responses/1", "sberbank", DOM) == "observed")
chk("сторонний разбор = наблюдается",
    stance_for("https://brobank.ru/obzor", "sberbank", DOM) == "observed")
chk("чужой банк про наш субъект = наблюдается",
    stance_for("https://www.tbank.ru/x", "sberbank", DOM) == "observed")
chk("сайт ЦБ = норма регулятора",
    stance_for("https://cbr.ru/press/1", "sberbank", DOM) == "regulatory")
chk("любой gov.ru = норма регулятора",
    stance_for("https://minfin.gov.ru/a", "sberbank", DOM) == "regulatory")

print("реестр: якоря, матрица, реально использованные источники")
reg = FactRegistry()
reg.add(subject="sberbank", attribute="ставка", value="16,5", unit="%",
        verbatim="Ставка на остаток — 16,5% годовых", stance="declared",
        url="https://sberbank.ru/a")
reg.add(subject="sberbank", attribute="жалобы", value="списания без ведома",
        unit="", verbatim="списывая деньги с кредиток без ведома",
        stance="observed", url="https://banki.ru/r/1")
reg.add(subject="tinkoff", attribute="ставка", value="14,8", unit="%",
        verbatim="ставка 14,8% годовых", stance="declared",
        url="https://tbank.ru/b")
chk("якоря сквозные и уникальные", [f.id for f in reg.facts] == [1, 2, 3])
reg.add(subject="tinkoff", attribute="жалобы", value="сбой приложения",
        unit="", verbatim="приложение не открывается за границей",
        stance="observed", url="https://brobank.ru/x")
chk("факт со слабой цитатой ВСЁ РАВНО в реестре (не отбрасываем)",
    len(reg.facts) == 4)
chk("матрица субъект×атрибут собирается",
    {("sberbank", "ставка"), ("sberbank", "жалобы"),
     ("tinkoff", "ставка")} <= set(reg.by_cell()))
chk("источники — только те, что дали факты", len(reg.urls()) == 4)
ctx = reg.render_for_writer({"sberbank": "Сбербанк", "tinkoff": "Т-Банк"})
chk("в контексте писателя есть якорь [f:2]", "[f:2]" in ctx)
chk("обе стороны различимы в контексте",
    "заявлено" in ctx and "наблюдается" in ctx)
chk("цитата доезжает до писателя дословно",
    "Ставка на остаток — 16,5% годовых" in ctx)


print("ранжирование считается по контракту, а не сочиняется")
from types import SimpleNamespace as _NS  # noqa: E402
from bank_audit.research.gptr import ranking as _rank  # noqa: E402
_r = FactRegistry()
for _s, _a, _st in [("sberbank", "ставка", "declared"),
                    ("sberbank", "жалобы", "observed"),
                    ("sberbank", "нормы", "regulatory"),
                    ("tinkoff", "ставка", "declared")]:
    _r.add(subject=_s, attribute=_a, value="x", unit="",
           verbatim="достаточно длинная цитата", url=f"https://{_s}.ru",
           stance=_st)
_rows = _rank.build(_NS(subjects=["sberbank", "tinkoff"],
                        subject_labels={"sberbank": "Сбербанк",
                                        "tinkoff": "Т-Банк"}),
                    _r, ["ставка", "жалобы", "нормы"])
chk("полнее раскрывший идёт первым", _rows[0].subject == "sberbank")
chk("считается доля закрытых характеристик",
    _rows[0].closed == 3 and _rows[1].closed == 1)
chk("стороны доказательства разнесены",
    _rows[0].regulatory == 1 and _rows[0].observed == 1)
chk("в таблице для писателя есть критерий",
    "ПОЛНОТА РАСКРЫТИЯ" in _rank.render(_rows))

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
