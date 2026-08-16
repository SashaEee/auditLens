"""Единый разбор чисел для сверки отчёта с фактами.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Числа отчёта и числа фактов разбирались РАЗНЫМ кодом, и
разбирались по-разному: текст читался правильно («27,608%» → 27.608), а факты —
регулярным выражением, которое выбрасывало десятичную запятую вместе с
разрядными пробелами («27,608%» → 27608.0). Совпадения не было никогда, поэтому
на всём, что имеет дробную часть — ставки, ПСК, проценты, комиссии, — проверка
работала наоборот:

  • ВЕРНОЕ число регулятора объявлялось «числом без опоры», и директива ремонта
    приказывала переписывателю его убрать или заменить;
  • ВЫДУМАННОЕ «177%» совпало бы с базой (из факта «17,7%») и прошло как
    подтверждённое.

Второй источник лжи — «безопасные годы»: любое число из диапазона 1990–2049
считалось годом и уходило в проверенные, поэтому выдуманная комиссия «2 000 ₽»
объявлялась сверенной. Год отличаем ПО ЕДИНИЦЕ ИЗМЕРЕНИЯ, а не по величине.

Здесь один разбор на оба конца сверки. Правило простое: пробел (в том числе
неразрывный) внутри числа — разделитель разрядов, точка и запятая — десятичный
разделитель.
"""
from __future__ import annotations

import re

# Разрядные пробелы: обычный, неразрывный, узкий неразрывный.
_SPACES = "   "
# 1 234,56 | 1234.5 | 27,608 | 100 000
_NUM = rf"\d{{1,3}}(?:[{_SPACES}]\d{{3}})+|\d+"
_NUM_RE = re.compile(rf"({_NUM})(?:[.,](\d+))?")

# Единицы, по которым число вообще считается величиной, а не «номером дома».
_UNIT = (r"₽|руб\w*|%|процент\w*|п\.?\s?п\.?|тыс\w*|млн|млрд|"
         r"лет|год\w*|г\.|дн\w*|мес\w*|раз\w*|балл\w*")
_UNIT_RE = re.compile(rf"({_NUM})(?:[.,](\d+))?\s*({_UNIT})", re.IGNORECASE)

# Даты: из «01.10.2025» осмысленное число только год, остальное — не величина.
_DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./](\d{4})\b")

# Единица, означающая ГОД. Только с ней число трактуется как год.
_YEAR_UNIT_RE = re.compile(r"^(?:лет|год\w*|г\.)$", re.IGNORECASE)


def _to_float(whole: str, frac: str | None) -> float | None:
    raw = re.sub(rf"[{_SPACES}]", "", whole)
    try:
        return float(raw + ("." + frac if frac else ""))
    except ValueError:
        return None


def parse_with_units(text: str) -> list[tuple[float, str]]:
    """Числа С ЕДИНИЦАМИ из текста: [(значение, единица), …].

    Порядок и повторы сохраняются: счётчик «сверено N из M» обязан считать
    вхождения, а не уникальные значения.
    """
    out: list[tuple[float, str]] = []
    for m in _UNIT_RE.finditer(text or ""):
        v = _to_float(m.group(1), m.group(2))
        if v is not None:
            out.append((v, (m.group(3) or "").lower()))
    return out


def all_numbers(text: str) -> set[float]:
    """ВСЕ числа текста — база для сверки со стороны фактов.

    Единицы здесь не требуем: в условиях факта величина часто стоит голой
    («при сумме от 100 000»). База намеренно ШИРЕ, чем текст отчёта: лишнее
    число в базе делает проверку мягче, а недостающее — обвиняет отчёт во лжи.
    """
    nums: set[float] = set()
    src = text or ""
    # Дата целиком — не величина: из неё берём только год.
    for m in _DATE_RE.finditer(src):
        nums.add(float(m.group(1)))
    src = _DATE_RE.sub(" ", src)
    for m in _NUM_RE.finditer(src):
        v = _to_float(m.group(1), m.group(2))
        if v is not None:
            nums.add(v)
    return nums


def numbers_from_facts(facts) -> set[float]:
    """База сверки из фактов bundle (значение, условия, дословная цитата)."""
    nums: set[float] = set()
    for f in facts or []:
        parts = [getattr(f, "value", "") or "",
                 " ".join(getattr(f, "conditions", None) or []),
                 getattr(f, "verbatim", "") or "",
                 getattr(f, "as_of", "") or ""]
        for txt in parts:
            nums |= all_numbers(txt)
    return nums


def is_year(value: float, unit: str) -> bool:
    """Год — только когда так сказала ЕДИНИЦА измерения.

    Прежняя проверка «1990 <= n < 2050» объявляла годом рублёвую сумму, и
    выдуманная комиссия 2 000 ₽ уходила в «проверено».
    """
    return bool(_YEAR_UNIT_RE.match((unit or "").strip())) and 1900 <= value <= 2100


# Относительный допуск нужен там, где округление законно (крупные суммы:
# «1 499 900 ₽» и «1,5 млн ₽» — одно и то же). На процентах и пунктах он лжёт:
# «30,1%» подтверждается числом 30, «8,9 п.п.» — числом 9. Для аудитора это
# РАЗНЫЕ величины, и 0,1 пп по ставке — не округление, а другая цифра.
_TOL_MIN_VALUE = 1000.0     # ниже — только точное совпадение
_REL_TOL = 0.02


def matches_fact(value: float, fact_nums: set[float], *, strict: bool = False,
                 rel_tol: float = _REL_TOL) -> bool:
    """Совпадение числа отчёта с базой фактов.

    strict=True — только точное совпадение. Используется там, где ценой ошибки
    является ЛОЖНОЕ ДОВЕРИЕ (счётчик «сверено N фактов»): показать аудитору
    зелёную плашку на числе, которого в фактах нет, хуже, чем не показать её.

    strict=False — допускается округление крупных сумм. Используется там, где
    ценой ошибки является РАЗРУШЕНИЕ: по этому решению критик объявляет число
    выдумкой, а директива ремонта приказывает писателю его убрать. Ошибиться
    здесь — значит своими руками вычистить из отчёта верную цифру.
    """
    if any(abs(value - fn) < 0.001 for fn in fact_nums):
        return True
    if strict or abs(value) < _TOL_MIN_VALUE:
        return False
    return any(fn and abs(value - fn) / abs(fn) < rel_tol for fn in fact_nums if fn)


def split_verified(pairs: list[tuple[float, str]],
                   fact_nums: set[float]) -> tuple[list[float], list[float]]:
    """Разносит числа отчёта на сверенные и несверенные (по вхождениям).

    Здесь сверка СТРОГАЯ: счётчик доверия не должен зеленеть на числе, которое
    лишь похоже на факт. Восемь процентов прежних «подтверждений» держались на
    допуске против постороннего числа.
    """
    verified: list[float] = []
    unverified: list[float] = []
    for value, unit in pairs:
        if is_year(value, unit) or matches_fact(value, fact_nums, strict=True):
            verified.append(value)
        else:
            unverified.append(value)
    return verified, unverified


# ════════════════════════════════════════════════════════════════════════
# Волна 3: единицы, множители, производные числа.
#
# Прежняя сверка сравнивала голые float: «комиссия 1,5%» Сбера считалась
# подтверждённой, потому что у другого банка есть лимит «1,5 млн ₽» — совпали
# полторашки. Ниже число носит КЛАСС ЕДИНИЦЫ, множители раскрываются
# («1,5 млн» ↔ 1 500 000), а дельты и кратные («на 8,9 п.п.», «в 3 раза»),
# которых в фактах нет по построению, не объявляются выдумкой, а ПЕРЕСЧИТЫВАЮТСЯ
# по парам фактов.
# ════════════════════════════════════════════════════════════════════════

_MULT = {"тыс": 1_000.0, "млн": 1_000_000.0, "млрд": 1_000_000_000.0}


def unit_class(unit: str) -> str:
    """Класс единицы: pct / rub / time / ratio / score, '' — неизвестно."""
    u = (unit or "").strip().lower()
    if not u:
        return ""
    if u.startswith("%") or u.startswith("процент") or u.replace(".", "").replace(" ", "") in ("пп",):
        return "pct"
    if u.startswith("₽") or u.startswith("руб"):
        return "rub"
    if _YEAR_UNIT_RE.match(u) or u.startswith("дн") or u.startswith("мес"):
        return "time"
    if u.startswith("раз"):
        return "ratio"
    if u.startswith("балл"):
        return "score"
    return ""


def _compatible(report_cls: str, fact_cls: str) -> bool:
    return report_cls == "" or fact_cls == "" or report_cls == fact_cls


def expand_pairs(pairs: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Раскрывает множители: (1.5, «млн») → (1_500_000, ''). Прочие — как есть."""
    out: list[tuple[float, str]] = []
    for v, u in pairs:
        m = _MULT.get((u or "").strip().lower())
        out.append((v * m, "") if m else (v, u))
    return out


def fact_base(facts) -> dict[float, set[str]]:
    """База сверки: значение → классы единиц, в которых оно встречалось.

    Числа без единицы получают класс '' (совместим с любым): база должна быть
    ШИРЕ отчёта — недостающее в базе обвиняет отчёт во лжи.
    """
    base: dict[float, set[str]] = {}

    def _add(v: float, cls: str) -> None:
        base.setdefault(round(v, 3), set()).add(cls)

    for f in facts or []:
        parts = [getattr(f, "value", "") or "",
                 " ".join(getattr(f, "conditions", None) or []),
                 getattr(f, "verbatim", "") or "",
                 getattr(f, "as_of", "") or ""]
        for txt in parts:
            for v, u in parse_with_units(txt):
                cls = unit_class(u)
                m = _MULT.get((u or "").strip().lower())
                if m:
                    _add(v * m, "")
                    _add(v, "")
                else:
                    _add(v, cls)
            for v in all_numbers(txt):
                _add(v, "")
    return base


def _match_base(value: float, cls: str, base: dict[float, set[str]],
                *, strict: bool) -> bool:
    for fv, classes in base.items():
        ok_cls = any(_compatible(cls, fc) for fc in classes)
        if not ok_cls:
            continue
        if abs(value - fv) < 0.001:
            return True
        if (not strict and abs(value) >= _TOL_MIN_VALUE and fv
                and abs(value - fv) / abs(fv) < _REL_TOL):
            return True
    return False


# Производные числа: «в N раз», «на X п.п.», «на X ₽» — писатель ОБЯЗАН их
# приводить (так требует промпт), в фактах их нет по построению.
_NUMG = rf"(?:{_NUM})(?:[.,]\d+)?"
_DERIVED_RES = [
    (re.compile(rf"в\s+({_NUMG})\s+раза?\b", re.IGNORECASE), "ratio"),
    (re.compile(rf"на\s+({_NUMG})\s*п\.?\s?п\.?(?![а-яё])", re.IGNORECASE), "pct"),
    (re.compile(rf"на\s+({_NUMG})\s*(?:тыс\w*|млн|млрд)?\s*(?:₽|руб\w*)", re.IGNORECASE), "rub"),
]


def derived_numbers(text: str) -> dict[float, str]:
    """Числа-производные из текста отчёта: {значение: вид}."""
    out: dict[float, str] = {}
    for rx, kind in _DERIVED_RES:
        for m in rx.finditer(text or ""):
            raw = m.group(1)
            nm = _NUM_RE.match(raw)
            if not nm:
                continue
            v = _to_float(nm.group(1), nm.group(2))
            if v is not None:
                out[round(v, 3)] = kind
    return out


def _recompute_derived(value: float, kind: str,
                       base: dict[float, set[str]]) -> bool:
    """Дельта/кратное подтверждается ПАРОЙ фактов, а не одним числом."""
    want_cls = {"pct": "pct", "rub": "rub"}.get(kind, "")
    vals = [fv for fv, cls in base.items()
            if any(_compatible(want_cls, fc) for fc in cls)]
    if kind == "ratio":
        for a in vals:
            for b in vals:
                if b and a != b and abs(a / b - value) <= max(0.06, value * 0.05):
                    return True
        return False
    tol = 0.06 if kind == "pct" else max(1.0, value * 0.01)
    for a in vals:
        for b in vals:
            if a > b and abs((a - b) - value) <= tol:
                return True
    return False


def audit_report_numbers(report_text: str, facts) -> dict:
    """Единая сверка чисел отчёта с фактами (для критика и оркестратора).

    Возвращает вхождения по корзинам:
      verified          — совпало с фактом (значение + класс единицы) или год
      derived_ok        — дельта/кратное, подтверждённое парой фактов
      derived_unchecked — производное, для которого пары не нашлось: НЕ выдумка
                          (пересчитать нельзя ≠ неверно), но и не зелёная плашка —
                          отдаётся в ручную проверку
      unverified        — не совпало строго (счётчик доверия)
      removal_candidates— не совпало даже с допуском округления крупных сумм:
                          только их можно приказывать убрать при ремонте
    """
    base = fact_base(facts)
    derived = derived_numbers(report_text)
    verified: list[float] = []
    derived_ok: list[float] = []
    derived_unchecked: list[tuple[float, str]] = []
    unverified: list[float] = []
    removal: list[float] = []
    for value, unit in expand_pairs(parse_with_units(report_text)):
        cls = unit_class(unit)
        if is_year(value, unit):
            verified.append(value)
            continue
        kind = derived.get(round(value, 3))
        if kind and cls in ("", kind, "ratio"):
            if _match_base(value, cls, base, strict=True) \
                    or _recompute_derived(value, kind, base):
                derived_ok.append(value)
            else:
                derived_unchecked.append((value, kind))
            continue
        if _match_base(value, cls, base, strict=True):
            verified.append(value)
        else:
            unverified.append(value)
            if not _match_base(value, cls, base, strict=False):
                removal.append(value)
    return {"verified": verified, "derived_ok": derived_ok,
            "derived_unchecked": derived_unchecked,
            "unverified": unverified, "removal_candidates": removal,
            "checked": len(verified) + len(derived_ok)
                       + len(derived_unchecked) + len(unverified)}
