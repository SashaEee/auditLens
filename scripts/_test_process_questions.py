"""Проверки для вопросов про ПРОЦЕСС, а не про тарифы (замечание владельца).

«Как оформить карту, где проще» — цифр мало, ценность в шагах, требованиях и
оговорках. Ранняя версия отбора ставила числа выше всего и вычищала именно
такой текст. Здесь ловим этот перекос.

Запуск:  .venv/bin/python scripts/_test_process_questions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bank_audit.rag.parsers.html_parser import parse_html          # noqa: E402
from bank_audit.research.v2.passive_indexer import _relevant_excerpt  # noqa: E402

ok = fail = 0


def chk(name, cond):
    global ok, fail
    ok += bool(cond); fail += not cond
    print(("  ✓ " if cond else "  ✗ ") + name)


TARIFFS = "\n".join(
    f"Тариф {i}: ставка {10 + i},5% годовых при сумме от {i}00 000 ₽ на 12 месяцев."
    for i in range(40))
PROCESS = (
    "Как оформить карту\n"
    "Нужен паспорт гражданина РФ.\n"
    "Заявка подаётся онлайн или в офисе банка.\n"
    "Решение принимается в течение рабочего дня.\n"
    "Доставка курьером или самовывоз в отделении.\n"
    "Активация происходит в мобильном приложении.\n"
    "Отказ возможен без объяснения причин.\n")
PAGE = TARIFFS + "\n" + PROCESS + "\n" + TARIFFS

print("вопрос про ПРОЦЕСС — шаги важнее цифр")
out = _relevant_excerpt(PAGE, "как оформить карту какие документы нужны где проще", 2500)
chk("шаг «нужен паспорт» сохранён", "паспорт" in out)
chk("канал подачи заявки сохранён", "онлайн или в офисе" in out)
chk("срок решения сохранён", "в течение рабочего дня" in out)
chk("активация сохранена", "Активация" in out)
chk("объём всё же сокращён", len(out) < len(PAGE) / 2)

print("вопрос про ТАРИФЫ — цифры важнее шагов")
out2 = _relevant_excerpt(PAGE, "сравни ставки по вкладам и минимальные суммы", 2500)
chk("тарифные строки отобраны", out2.count("ставка") >= 5)

print("парсер: короткие строки процесса не считаются мусором")
html = ("<html><body><main><h2>Оформление</h2>"
        + "".join(f"<p>Абзац {i}: подробное описание порядка обслуживания клиентов.</p>"
                  for i in range(6))
        + "<ul><li>Нужен паспорт</li><li>Заявка онлайн</li>"
        "<li>Решение за день</li><li>Вклады</li><li>Карты</li></ul>"
        "</main></body></html>").encode()
text = parse_html(html, "http://bank.ru/cards").text
main_part = text.split("# Элементы интерфейса")[0]
chk("«Нужен паспорт» в основном тексте", "Нужен паспорт" in main_part)
chk("«Заявка онлайн» в основном тексте", "Заявка онлайн" in main_part)
chk("пункт меню «Карты» отсеян", "Карты" not in main_part)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
