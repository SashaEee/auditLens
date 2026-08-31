"""Проверки оконной выборки (этап 4).

Главные требования: таблицы не режутся, границы окон по строкам, отбор
экстрактивный (куски дословны — иначе сломается сверка цитат), служебный
хвост с элементами интерфейса остаётся в конце.

Запуск:  .venv/bin/python scripts/_test_excerpt.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bank_audit.research.v2.passive_indexer import _relevant_excerpt  # noqa: E402

ok = fail = 0


def chk(name, cond):
    global ok, fail
    ok += bool(cond); fail += not cond
    print(("  ✓ " if cond else "  ✗ ") + name)


NOISE = "\n".join(f"Раздел {i}: общая информация о банке и его истории развития." for i in range(60))
TABLE = ("# Таблицы страницы\n\n| Срок | Ставка |\n|---|---|\n"
         "| 6 мес | 13,5% |\n| 12 мес | 14,2% |")
UI = "# Элементы интерфейса (не условия продукта)\nОнлайн · Срочный · от 100000 рублей"
PAGE = (NOISE + "\nСтавка по вкладу на 6 месяцев составляет 16,5% годовых при сумме от 50 000 ₽.\n"
        + NOISE + "\n" + TABLE + "\n\n" + UI)

out = _relevant_excerpt(PAGE, "ставка вклад 6 месяцев сумма", 2000)

print("что обязано выжить")
chk("релевантная строка со ставкой попала", "16,5% годовых" in out)
chk("таблица сохранена целиком", "| 6 мес | 13,5% |" in out and "| 12 мес | 14,2% |" in out)
chk("служебный хвост на месте", "Элементы интерфейса" in out)

print("что обязано уйти")
chk("объём сокращён", len(out) < len(PAGE) / 2)
chk("часть шума отброшена", out.count("общая информация о банке") < 30)

print("дословность (иначе сломается сверка цитат)")
frag = "Ставка по вкладу на 6 месяцев составляет 16,5% годовых"
chk("фрагмент скопирован дословно", frag in out)
chk("строки не порваны посередине",
    all(len(l) == 0 or l in PAGE for l in out.split("\n") if l and l != "…"))

print("пустая подсказка не ломает")
out2 = _relevant_excerpt(PAGE, "", 1500)
chk("без подсказки возвращается текст в пределах бюджета", 0 < len(out2) <= 1600)
chk("таблица не теряется и без подсказки", "13,5%" in out2)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
