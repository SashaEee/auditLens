"""Период из текста вопроса: «за полгода», «последние 3 месяца», «с начала года»,
«в 2025 году», «во II квартале 2026», «с мая по октябрь», «с 01.03.2026 по 15.04.2026».

Зачем отдельно от выбора периода в «Аудите отзывов» (review_period): там период
приходит параметрами экрана, а здесь — словами в вопросе ИИ-помощнику. Раньше слов
никто не разбирал: отчёт брал жалобы за фиксированные 540 дней, веб-поиск шёл без
дат, и аудитор, спросивший «за полгода», получал в ответе 2024 год (обращения
октября 2026; тот же класс — «даты подменены» в своде замечаний ТБ, август 2026).

extract() — чистая функция: текст и «сегодня» → отрезок дат включительно или None.
Модель здесь не нужна: формулировок периода немного, и ошибаться в них нельзя.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
           "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
_MON_RX = (r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\w*|июн\w*|июл\w*|август\w*|"
           r"сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)")
_RU_MON_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
               "сентября", "октября", "ноября", "декабря"]
_UNITS = {"дн": 1, "ден": 1, "нед": 7, "месяц": 30, "мес": 30, "год": 365, "лет": 365, "квартал": 91}
_NUM = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
        "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "двенадцать": 12, "полтора": 1.5}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "1": 1, "2": 2, "3": 3, "4": 4,
          "перв": 1, "втор": 2, "трет": 3, "четв": 4}


@dataclass(frozen=True)
class QPeriod:
    d0: date
    d1: date                    # включительно
    phrase: str                 # как сказано в вопросе

    @property
    def days_back(self) -> int:
        """Сколько дней от начала периода до сегодня — для поиска «не старше N дней»."""
        return max(1, (date.today() - self.d0).days + 1)

    def label(self) -> str:
        f = lambda d: f"{d.day} {_RU_MON_GEN[d.month]} {d.year}"  # noqa: E731
        return f"с {f(self.d0)} по {f(self.d1)}"


def _month_no(word: str) -> int | None:
    w = word.lower()
    for stem, n in _MONTHS.items():
        if w.startswith(stem) and (stem != "ма" or re.match(r"ма[йяе]", w)):
            return n
    return None


def _month_end(y: int, m: int) -> date:
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def _year_of(m: int, year: str | None, today: date) -> int:
    """Месяц без года — ближайший прошедший: «в декабре», спрошенное в марте, — прошлый год."""
    if year:
        return int(year)
    return today.year if m <= today.month else today.year - 1


def _num(s: str) -> float | None:
    s = s.lower()
    if s.replace(",", ".").replace(".", "", 1).isdigit():
        return float(s.replace(",", "."))
    for k, v in _NUM.items():
        if s.startswith(k):
            return float(v)
    return None


def extract(text: str, today: date | None = None) -> QPeriod | None:
    t = " ".join((text or "").lower().replace("ё", "е").split())
    today = today or date.today()
    if not t:
        return None

    # 1. явные даты «с 01.03.2026 по 15.04.2026»
    m = re.search(r"(?:с|от)\s+(\d{1,2})[./](\d{1,2})[./](\d{4})\s*(?:г\.?\s*)?(?:по|до|-|—)\s*"
                  r"(\d{1,2})[./](\d{1,2})[./](\d{4})", t)
    if m:
        try:
            d0 = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            d1 = date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
            if d0 <= d1:
                return QPeriod(d0, min(d1, today), m.group(0))
        except ValueError:
            pass

    # 2. «с мая по октябрь 2026», «с января по март»
    m = re.search(rf"\bс\s+{_MON_RX}\s*(\d{{4}})?\s*(?:г\.?\s*)?(?:по|до|-|—)\s*{_MON_RX}\s*(\d{{4}})?", t)
    if m and _month_no(m.group(1)) and _month_no(m.group(3)):
        m0, m1 = _month_no(m.group(1)), _month_no(m.group(3))
        y1 = _year_of(m1, m.group(4), today)
        y0 = int(m.group(2)) if m.group(2) else (y1 if m0 <= m1 else y1 - 1)
        return QPeriod(date(y0, m0, 1), min(_month_end(y1, m1), today), m.group(0))

    # 3. квартал «во II квартале 2026», «за 3 кв. 2025», «первый квартал»
    m = re.search(r"\b(i{1,3}|iv|[1-4]|перв\w*|втор\w*|трет\w*|четв\w*)\s*(?:-?й\s*)?(?:квартал\w*|кв\.?)"
                  r"\s*(\d{4})?", t)
    if m:
        key = next((k for k in _ROMAN if m.group(1).startswith(k)), None) if not m.group(1) in _ROMAN \
            else m.group(1)
        q = _ROMAN.get(key or "")
        if q:
            y = int(m.group(2)) if m.group(2) else (today.year if 3 * q - 2 <= today.month else today.year - 1)
            return QPeriod(date(y, 3 * q - 2, 1), min(_month_end(y, 3 * q), today), m.group(0))

    # 4. полугодие «первое полугодие 2026»
    m = re.search(r"\b(перв\w*|втор\w*|[12])\s*(?:-?е\s*)?полугоди\w*\s*(\d{4})?", t)
    if m:
        h = 1 if m.group(1).startswith(("перв", "1")) else 2
        y = int(m.group(2)) if m.group(2) else today.year
        return QPeriod(date(y, 1 if h == 1 else 7, 1), min(_month_end(y, 6 if h == 1 else 12), today),
                       m.group(0))

    # 5. «с начала года / месяца / квартала»
    m = re.search(r"\bс\s+начала\s+(года|месяца|квартала)", t)
    if m:
        w = m.group(1)
        d0 = (date(today.year, 1, 1) if w == "года" else today.replace(day=1) if w == "месяца"
              else date(today.year, 3 * ((today.month - 1) // 3) + 1, 1))
        return QPeriod(d0, today, m.group(0))

    # 6. относительные: «за последние 3 месяца», «за полгода», «последнюю неделю», «за 90 дней»
    m = re.search(r"\b(?:за|в\s+течение|последн\w*)\s+(?:последн\w*\s+)?(пол\s*года|полгода|полугоди\w*)", t)
    if m:
        return QPeriod(today - timedelta(days=182), today, m.group(0))
    m = re.search(r"\b(?:за|в\s+течение|последн\w*)\s+(?:последн\w*\s+)?(\d+(?:[.,]\d+)?|"
                  r"один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|двенадцать|полтора)?"
                  r"\s*(дн\w*|ден\w*|недел\w*|нед\.|месяц\w*|мес\.|год\w*|лет|квартал\w*)\b", t)
    if m:
        n = _num(m.group(1)) if m.group(1) else 1.0
        unit = next((v for k, v in _UNITS.items() if m.group(2).startswith(k)), None)
        if n and unit and not (unit == 365 and m.group(1) is None and re.search(r"\d{4}", t)):
            days = int(round(n * unit))
            if 1 <= days <= 3660:
                return QPeriod(today - timedelta(days=days - 1), today, m.group(0))

    # 7. месяц «в октябре», «за сентябрь 2026»
    m = re.search(rf"\b(?:в|во|за)\s+{_MON_RX}\s*(\d{{4}})?", t)
    if m and _month_no(m.group(1)):
        mo = _month_no(m.group(1))
        y = _year_of(mo, m.group(2), today)
        return QPeriod(date(y, mo, 1), min(_month_end(y, mo), today), m.group(0))

    # 8. годы «в 2025 году», «за 2025–2026 годы», «2024-2025 гг»
    m = re.search(r"\b(20\d{2})\s*(?:-|—|–|по|и)\s*(20\d{2})\s*(?:гг\.?|годы|годах|года|год)?", t)
    if m and int(m.group(1)) <= int(m.group(2)) <= today.year:
        return QPeriod(date(int(m.group(1)), 1, 1), min(date(int(m.group(2)), 12, 31), today), m.group(0))
    m = re.search(r"\b(?:в|за|по\s+итогам)\s+(20\d{2})\s*(?:год\w*|г\.?)?", t)
    if m and int(m.group(1)) <= today.year:
        y = int(m.group(1))
        return QPeriod(date(y, 1, 1), min(date(y, 12, 31), today), m.group(0))
    return None


def anchor(text: str | None) -> str:
    """Строка для system-подсказки: период вопроса и как его соблюдать, или ""."""
    p = extract(text or "")
    if not p:
        return ""
    return (f"ПЕРИОД ВОПРОСА: {p.label()} (в вопросе: «{p.phrase}»). Ищи и используй материалы "
            "этого периода; в поисковые запросы добавляй годы периода. Более ранние данные — только "
            "как фон или для сравнения и с явной пометкой даты; не выдавай их за текущую практику.")
