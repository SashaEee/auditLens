"""Дата публикации первоисточника должна доезжать из адаптера в запись."""
import pathlib, sys, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from bank_audit.loophole import collector as C
from bank_audit.loophole.adapters import fetch_decorator as FD

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1
        print("  ПРОВАЛ:", name)

# 1. Разбор ISO-строки
d = C._to_dt("2024-03-11T09:30:00+03:00")
check("ISO разобран", isinstance(d, datetime.datetime) and d.year == 2024 and d.month == 3)
check("пусто → None", C._to_dt(None) is None and C._to_dt("") is None)
check("мусор → None", C._to_dt("вчера") is None)

# 2. Извлечение даты из разметки страницы
html = b'<html><head><meta property="article:published_time" content="2024-03-11T09:30:00+03:00"></head><body>x</body></html>'
got = FD._exact_published_at(html)
check("дата извлечена из meta", got is not None and got.startswith("2024-03-11"))
check("naive дата отвергнута",
      FD._exact_published_at(b'<meta property="article:published_time" content="2024-03-11">') is None)

# 3. Запись действительно принимает поле
from bank_audit.loophole.models import LoopholeRecord
rec = LoopholeRecord(sha256="x", published_at=C._to_dt(got))
check("запись хранит дату", rec.published_at is not None and rec.published_at.year == 2024)

# 4. Сборка записи в коллекторе передаёт дату (проверка по исходнику: поле есть
#    в конструкторе LoopholeRecord и присваивается из page.published_at)
src = pathlib.Path(C.__file__).read_text()
check("коллектор читает page.published_at", "_to_dt(page.published_at)" in src)
check("коллектор кладёт дату в запись", "published_at=published," in src)
check("при неудачной загрузке дата пуста", "published = None" in src)

print(f"\n{ok} проверок пройдено, провалов: {fail}")
sys.exit(1 if fail else 0)
