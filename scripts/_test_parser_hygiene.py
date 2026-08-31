"""Проверки гигиены парсера (этап 3).

Ловят ровно то, ради чего правки делались: не терять числа продукта, не тащить
меню и фильтры, схлопывать повторы. Все случаи — на HTML реалистичного объёма,
иначе срабатывает аварийный путь для бедных страниц и проверяется не то.

Запуск:  .venv/bin/python scripts/_test_parser_hygiene.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bank_audit.rag.parsers.html_parser import parse_html  # noqa: E402

ok = fail = 0


def chk(name, cond):
    global ok, fail
    ok += bool(cond); fail += not cond
    print(("  ✓ " if cond else "  ✗ ") + name)


# Разные абзацы: одинаковые схлопнулись бы дедупом, страница стала бы короткой
# и ушла на аварийный путь — тест проверял бы не тот код.
FILLER = "".join(
    f"<p>Абзац {i}: подробное описание условий обслуживания и начисления процентов.</p>"
    for i in range(6))

HTML = f"""<html><title>Вклады</title><body><main>
<h2>Вклад на 6 месяцев</h2>
<p>Условия по продукту.</p>
{FILLER}
<div class="tabs"><span>Ставка 16,5% годовых</span></div>
<div class="sc-generated"><span>14,8%</span></div>
<form class="filter"><label>По сумме: от 100000 рублей</label>
  <select><option>1 месяц</option><option>2 месяца</option></select></form>
<nav><a href="/a">Вклады</a><a href="/b">Кредиты</a><a href="/c">Карты</a></nav>
<ul><li>Онлайн</li><li>Срочный</li></ul>
<p>Условия по продукту.</p>
<table><tr><th>Срок</th><th>Ставка</th></tr><tr><td>6 мес</td><td>13,5%</td></tr></table>
</main></body></html>""".encode("utf-8")

doc = parse_html(HTML, "http://bank.ru/deposits")
text = doc.text or ""
main_part = text.split("# Элементы интерфейса")[0]

print("числа продукта не теряются")
chk("ставка из вкладки (селектор шума не съел блок с числом)", "16,5%" in text)
chk("ставка из карточки SPA (div-лист добран)", "14,8%" in text)
chk("таблица тарифов сохранена целиком", "| 6 мес | 13,5% |" in text)

print("мусор не попадает в основной текст")
chk("меню отсеяно", "Кредиты" not in main_part)
chk("повтор абзаца схлопнут", main_part.count("Условия по продукту.") == 1)
chk("короткий абзац сохранён (не спутан с чипом)", "Условия по продукту." in main_part)

print("отсеянное помечено, а не выброшено молча")
# <nav> вырезается целиком как чистая навигация — в хвост попадают короткие
# подписи из контента (чипы, пункты списков), которые могли бы сойти за условия.
chk("служебный хвост присутствует", "Элементы интерфейса" in text)
chk("короткие подписи лежат в хвосте, а не в тексте",
    "Онлайн" in text.split("# Элементы интерфейса")[-1])

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
