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


print("принадлежность отзыва берётся из корпуса, а не угадывается")
from bank_audit.research.gptr import reviews as _rev  # noqa: E402
from bank_audit.research.gptr import runstate as _rs  # noqa: E402
_rs.new_run()
_recs = [{"bank": "Т-Банк", "subject": "tinkoff", "date": "2026-07-16",
          "product": "Инвестиции", "url": "https://banki.ru/r/13230337",
          "text": "с меня списывают комиссию почти в 10 раз больше"}]
_pages = _rev.as_pages(_recs)
chk("в текст страницы попадает ТОЛЬКО слова клиента",
    _pages["https://banki.ru/r/13230337"]
    == "с меня списывают комиссию почти в 10 раз больше")
chk("служебная строка не подмешана в текст",
    "Отзыв клиента" not in _pages["https://banki.ru/r/13230337"])
chk("подсказка о принадлежности отдана отдельно",
    _rev.subject_hints() == {"https://banki.ru/r/13230337": "tinkoff"})
_prev = _rs.new_run()          # новый прогон — чужие метаданные не видны
chk("состояние прогона не течёт в соседний вопрос",
    _rev.subject_hints() == {})
_rs.new_run(); _rev.as_pages(_recs)
_reg3 = FactRegistry()
_reg3.add(subject="tinkoff", attribute="комиссия", value="в 10 раз больше",
          unit="", verbatim="списывают комиссию почти в 10 раз больше",
          url="https://banki.ru/r/13230337", stance="observed")
_rev.stamp_dates(_reg3)
chk("дата отзыва проставлена из корпуса", _reg3.facts[0].date == "2026-07-16")

print("отбор страниц: порог по теме един для всех трёх сторон")
from bank_audit.research.gptr.facts import _fit_page, select_pages  # noqa: E402
_pages = {"https://sberbank.ru/a": "ставка 16% годовых по вкладу " * 40,
          "https://cbr.ru/n": "указание банка россии о раскрытии " * 40,
          "https://banki.ru/otzyv": "жалоба клиента на комиссию по вкладу " * 40,
          "https://hh.ru/vac": "вакансия продакт менеджер обязанности офис " * 40}
_sel = select_pages(_pages, ["ставка по вкладу", "жалобы клиентов", "нормы ЦБ"],
                    {"sberbank": "sberbank.ru"}, 3)
chk("мусор не по теме отсеян даже из наблюдаемой стороны",
    "https://hh.ru/vac" not in _sel)
chk("все три стороны представлены",
    {"https://sberbank.ru/a", "https://cbr.ru/n", "https://banki.ru/otzyv"}
    <= set(_sel))

print("бюджет страницы режется с двух концов")
_long = "НАЧАЛО " * 200 + "СЕРЕДИНА " * 3000 + "# Таблицы страницы | 6 мес | 16,5% |"
_fit = _fit_page(_long)
chk("начало страницы сохранено", _fit.startswith("НАЧАЛО"))
chk("таблица тарифов из хвоста сохранена",
    "Таблицы страницы" in _fit and "16,5%" in _fit)
chk("бюджет соблюдён", len(_fit) < 15000)

print("потоковая перенумерация якорей")
from bank_audit.research.gptr.citations import StreamRenumberer  # noqa: E402
_sr = FactRegistry()
_sr.add(subject="sberbank", attribute="ставка", value="16", unit="%",
        verbatim="ставка 16% годовых", url="https://sberbank.ru/a",
        stance="declared")
_sr.add(subject="sberbank", attribute="жалобы", value="отказ", unit="",
        verbatim="получил необоснованный отказ", url="https://banki.ru/r",
        stance="observed")
_rn = StreamRenumberer(_sr)
# самый опасный случай: якорь разорван между кусками потока
_out = "".join(_rn.feed(p) for p in
               ["Ставка 16% ", "[f:", "1] и жалоба ", "[f:2]", " конец"])
_out += _rn.finish()
chk("разорванный между кусками якорь собран",
    _out == "Ставка 16% [1] и жалоба [2] конец")
chk("источники пронумерованы по первому упоминанию",
    [x["url"] for x in _rn.sources()] == ["https://sberbank.ru/a",
                                          "https://banki.ru/r"])
chk("счётчик цитирований совпадает", _rn.stats()["цитирований"] == 2)
_rn2 = StreamRenumberer(_sr)
_o2 = _rn2.feed("текст [f:99] хвост") + _rn2.finish()
chk("якорь на несуществующий факт убирается", "[f:99]" not in _o2
    and _rn2.stats()["якорей_в_никуда"] == 1)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
