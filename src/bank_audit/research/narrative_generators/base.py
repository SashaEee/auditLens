"""Базовые утилиты для narrative-генераторов.

Главный фокус — АНТИГАЛЛЮЦИНАЦИОННЫЕ guard-функции:

1) verify_numbers_in_text(text, facts):
   После генерации проверяет что КАЖДОЕ число в тексте имеет соответствие
   в фактах. Если число не найдено — флаг для отбраковки.

2) enforce_citations(text, allowed_indices):
   Проверяет что каждое предложение с числом имеет [N] цитирование.
   Удаляет «голые» утверждения (sentence без [N]) если они с цифрами.

3) parse_json_object(raw) / strip_markdown_fences(raw):
   Безопасный JSON-парсер с обработкой ```json fences```.

NarrativeContext — общий контекст для всех генераторов:
  • client, model — для LLM-вызовов
  • entities, facts, sources_index — данные
  • question — оригинальный вопрос аудитора
"""
from __future__ import annotations
import json, logging, re
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from ..entity_extractor import Entity
from ..fact import Fact

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# CONTEXT
# ════════════════════════════════════════════════════════════════════


@dataclass
class NarrativeContext:
    """Общий контекст для всех narrative-генераторов."""
    client: AsyncOpenAI
    model: str
    question: str
    entities: list[Entity]
    facts: list[Fact]
    sources_index: list[dict]
    has_regulatory: bool = False
    # Аналитический меморандум (research_brief) — единый «мозг» отчёта.
    # type: ResearchBrief | None (без импорта во избежание цикла)
    brief: object = None

    def brief_block(self, kind: str = "") -> str:
        """Блок меморандума + директива секции для подмешивания в промпт."""
        if not self.brief:
            return ""
        try:
            ctx = self.brief.brief_context()
            d = self.brief.directive(kind) if kind else ""
            parts = []
            if ctx:
                parts.append("# АНАЛИТИЧЕСКИЙ МЕМОРАНДУМ (опирайся на него)\n" + ctx)
            if d:
                parts.append(f"# ЗАДАЧА ЭТОЙ СЕКЦИИ\n{d}")
            return "\n\n".join(parts)
        except Exception:
            return ""

    def excerpts_block(self, max_n: int = 6, per: int = 500) -> str:
        """Сырые выдержки источников — живой язык тарифов для глубины (#3)."""
        ranked = sorted(self.sources_index or [],
                         key=lambda s: -(s.get("trust_score") or 0))
        out = []
        for s in ranked[:max_n]:
            exc = " ".join(s.get("excerpts") or [])[:per].strip()
            if exc:
                out.append(f"[{s.get('n')}] {s.get('domain','')}: {exc}")
        return "\n".join(out)


# ════════════════════════════════════════════════════════════════════
# JSON PARSING
# ════════════════════════════════════════════════════════════════════


def strip_markdown_fences(raw: str) -> str:
    """Убирает ```json/```/``` обёртки."""
    if not raw:
        return ""
    t = raw.strip()
    t = re.sub(r"^```(?:json|markdown|md)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_json_object(raw: str) -> dict | None:
    """Извлекает первый валидный JSON-объект из строки."""
    if not raw:
        return None
    t = strip_markdown_fences(raw)
    # Ищем первую открывающую {
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    cand = t[start:end] if end > 0 else t[start:].rstrip().rstrip(",") + "}"
    try:
        return json.loads(cand)
    except Exception:
        pass
    try:
        return json.loads(re.sub(r",\s*([\]}])", r"\1", cand))
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════
# ANTI-HALLUCINATION GUARDS
# ════════════════════════════════════════════════════════════════════


# Все варианты пробелов которые LLM может использовать как thousands separator:
#   ' '  обычный (U+0020), non-breaking (U+00A0),
#   narrow no-break (U+202F), thin (U+2009), hair (U+200A), punctuation (U+2008)
_THOUSAND_SEPS = "      "
_SEP_TRANSLATE = str.maketrans({c: "" for c in _THOUSAND_SEPS})

# Извлечение числовых значений из текста (₽, %, дни, штуки, etc.)
_NUM_PATTERN = re.compile(
    r"(?<![A-Za-zА-Яа-я0-9_])"
    r"(\d{1,3}(?:[" + _THOUSAND_SEPS + r"]\d{3})+|\d+)"
    r"(?:[.,](\d+))?"
)


def _extract_numbers(text: str) -> list[float]:
    """Извлекает все числа из текста как float. Понимает все unicode-пробелы."""
    nums = []
    for m in _NUM_PATTERN.finditer(text):
        raw_int = m.group(1).translate(_SEP_TRANSLATE)
        frac = m.group(2)
        try:
            val = float(raw_int + ("." + frac if frac else ""))
            nums.append(val)
        except ValueError:
            continue
    return nums


def _facts_numbers(facts: list[Fact]) -> set[float]:
    """Собирает все числа которые упоминаются в фактах
    (value_numeric + любые числа в value/conditions/exceptions/verbatim_quote)."""
    nums: set[float] = set()
    for f in facts:
        if f.value_numeric is not None:
            nums.add(round(float(f.value_numeric), 4))
        # Парсим числа из value (может быть «от 6 до 22»)
        for n in _extract_numbers(f.value):
            nums.add(round(n, 4))
        for cond in f.conditions:
            for n in _extract_numbers(cond):
                nums.add(round(n, 4))
        for exc in f.exceptions:
            for n in _extract_numbers(exc):
                nums.add(round(n, 4))
        if f.verbatim_quote:
            for n in _extract_numbers(f.verbatim_quote):
                nums.add(round(n, 4))
        if f.qualifications:
            for n in _extract_numbers(f.qualifications):
                nums.add(round(n, 4))
    return nums


# Регекс для извлечения числа С ЕДИНИЦЕЙ ИЗМЕРЕНИЯ.
# Только такие числа CRITICAL для verify — без юнита это обычно дата/№ статьи/счётчик.
_NUM_WITH_UNIT = re.compile(
    r"(?<![A-Za-zА-Яа-я0-9_])"
    r"(\d{1,3}(?:[" + _THOUSAND_SEPS + r"]\d{3})+|\d+)"
    r"(?:[.,](\d+))?"
    r"\s*(?:₽|руб|р\.|%|процент|тыс|млн|млрд|лет|год|дн[ея]|дней|"
    r"мес|месяц|шт|раз|долл|евро|usd|eur|usd|rub)",
    re.IGNORECASE,
)


def _extract_unit_numbers(text: str) -> list[float]:
    """Извлекает ТОЛЬКО числа с единицей измерения (₽, %, лет, дней и т.д.)."""
    nums = []
    for m in _NUM_WITH_UNIT.finditer(text):
        raw_int = m.group(1).translate(_SEP_TRANSLATE)
        frac = m.group(2)
        try:
            nums.append(float(raw_int + ("." + frac if frac else "")))
        except ValueError:
            continue
    return nums


def verify_numbers_in_text(text: str, facts: list[Fact],
                             tolerance: float = 0.02,
                             strict: bool = False) -> tuple[bool, list[float]]:
    """Проверяет что КАЖДОЕ число в тексте есть в фактах.

    Возвращает (ok, list_of_hallucinated_numbers).
    Если ok=False — значит LLM «придумала» числа, нужно отбраковать предложение.

    tolerance — допустимая относительная погрешность для приближённого сравнения
    (LLM может округлить 1500 ₽ → 1500₽ — это ок).

    strict=False (default): проверяем ТОЛЬКО числа-с-единицей-измерения (₽/%/лет/...)
    — числа без единицы (это обычно даты, номера статей, порядковые) пропускаем,
    чтобы избежать false positives.

    strict=True: жёсткая проверка всех чисел (для key_findings/pricing где
    каждое число должно быть выверено).
    """
    # В strict mode проверяем ВСЕ числа; в default — только с единицей
    text_nums = (_extract_numbers(text) if strict
                  else _extract_unit_numbers(text))
    if not text_nums:
        return True, []
    fact_nums = _facts_numbers(facts)

    # Безопасные числа: даты годов (1990-2050), порядковые 1-100
    # (часто появляются в названиях ФЗ/НПА/статей/дат)
    safe_years = {float(y) for y in range(1990, 2050)}
    safe_small = {float(x) for x in range(1, 101)}
    safe = safe_years | safe_small

    # «Юнит-числа» для контекста: позволяет различать «50%» от «50 (без юнита)»
    unit_nums_in_text = set(_extract_unit_numbers(text))

    hallucinated = []
    for n in text_nums:
        # Годы 1990-2050 ВСЕГДА safe (даже если «2024 года» захватился как unit)
        if n in safe_years:
            continue
        # Малые числа 1-100 safe только если БЕЗ юнита
        # (иначе «50%» был бы пропущен — это критичная цифра)
        if n in safe_small and n not in unit_nums_in_text:
            continue
        # Точное совпадение
        if any(abs(n - fn) < 0.001 for fn in fact_nums):
            continue
        # Приближённое (например ставка 6.5% против 6.50%)
        rel_ok = False
        for fn in fact_nums:
            if fn == 0:
                continue
            if abs(n - fn) / abs(fn) < tolerance:
                rel_ok = True
                break
        # Кратное проверяем: 50000 vs 50 (тысяч) — корректно
        if not rel_ok:
            for fn in fact_nums:
                if fn == 0:
                    continue
                # 50000 / 50 = 1000 (1 тыс — корректное преобразование)
                ratio = n / fn
                if 990 < ratio < 1010 or 0.00099 < ratio < 0.00101:
                    rel_ok = True
                    break
        # ПРОИЗВОДНЫЕ сравнения (claim-level): «в 2 раза дороже», «разница 1290 ₽»,
        # «на 30% выше» — число вычислено из пары фактов, это аналитика, не выдумка.
        if not rel_ok and _is_derived_number(n, fact_nums, tolerance):
            rel_ok = True
        if not rel_ok:
            hallucinated.append(n)
    return (len(hallucinated) == 0), hallucinated


def _is_derived_number(n: float, fact_nums: set[float], tol: float = 0.02) -> bool:
    """True, если n — правдоподобный РЕЗУЛЬТАТ сравнения пары чисел-фактов:
    разность |a-b|, отношение a/b (кратность), процентная разница (a-b)/b*100.
    Позволяет нарративу делать сравнительные выводы («в 2 раза», «на 30%»,
    «на 1290 ₽ дороже»), не помечая их галлюцинацией.

    ВАЖНО: большие АБСОЛЮТНЫЕ значения (>100 000) НЕ считаем производными — при
    десятках фактов почти любое 6-7-значное число случайно попадает в diff/ratio
    какой-то пары (ложные «переплаты ~1 200 000 ₽»). Такие суммы обязаны быть
    реальными фактами, а не вычислением модели."""
    if abs(n) > 100_000:
        return False
    nums = [x for x in fact_nums if x]
    nums = sorted(nums, key=lambda x: -abs(x))[:60]   # ограничим O(n^2)

    def _close(a: float, b: float) -> bool:
        if b == 0:
            return abs(a) < 0.001
        return abs(a - b) < 0.05 or abs(a - b) / abs(b) < tol

    for a in nums:
        for b in nums:
            if a == b:
                continue
            if _close(n, abs(a - b)):           # разность
                return True
            if b and _close(n, a / b):          # кратность (в N раз)
                return True
            if b and _close(n, abs(a - b) / abs(b) * 100):  # процентная разница
                return True
    return False


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[А-ЯA-Z])")
_CITATION_PATTERN = re.compile(r"\[(\d+)\]")


def enforce_citations(text: str, allowed_indices: set[int],
                       require_for_numbers: bool = True) -> str:
    """Проходит по тексту:
       • Если в предложении есть число И НЕТ [N] — помечаем [?] (auditor warning)
       • Если [N] есть но N не в allowed_indices — удаляем цитату

    require_for_numbers=False → не требуем цитат вообще, только чистим невалидные.
    """
    if not text:
        return text
    # Сначала чистим невалидные цитаты по всему тексту
    def _replace(m):
        n = int(m.group(1))
        return m.group(0) if n in allowed_indices else ""
    cleaned = _CITATION_PATTERN.sub(_replace, text)

    # Числа в narrative УЖЕ проверены verify_numbers_in_text (все из фактов).
    # Поэтому [?] ставим максимально консервативно: ТОЛЬКО если во всём
    # блоке нет НИ ОДНОЙ валидной цитаты, а числа есть. Это ловит реальную
    # «голую» генерацию, но не флагует абзацы где цитаты сгруппированы в конце.
    if require_for_numbers:
        block_has_cite = bool(_CITATION_PATTERN.search(cleaned))
        block_has_nums = bool(_extract_numbers(cleaned))
        if block_has_nums and not block_has_cite:
            cleaned = cleaned.rstrip() + " [?]"
    return cleaned


# ════════════════════════════════════════════════════════════════════
# FACT FORMATTING (input для LLM)
# ════════════════════════════════════════════════════════════════════


def format_facts_for_prompt(facts: list[Fact], with_source: bool = True,
                             max_facts: int = 40) -> str:
    """Форматирует факты для system/user prompt'а."""
    if not facts:
        return "(нет фактов)"
    lines = []
    for f in facts[:max_facts]:
        # Базовое значение
        val = f"{f.value} {f.unit}".strip()
        line = f"[{f.entity_bank_slug}] {f.attribute} = {val}"
        if f.conditions:
            line += f" | условия: {'; '.join(f.conditions)}"
        if f.qualifications:
            line += f" | кому: {f.qualifications}"
        if f.exceptions:
            line += f" | исключения: {'; '.join(f.exceptions)}"
        if with_source and f.source_idx:
            line += f" [{f.source_idx}]"
        if f.verbatim_quote:
            q = re.sub(r"\s+", " ", f.verbatim_quote)[:180]
            line += f"\n    цитата: «{q}»"
        lines.append(line)
    return "\n".join(lines)


_SECTION_CAT_BOOST = {
    "pricing_breakdown":   {"fee", "rate", "limit"},
    "regulatory_box":      {"regulation"},
    "requirements_box":    {"requirement"},
    "government_programs":  {"requirement", "regulation", "rate"},
    "digital_channels":    {"feature"},
    "cant_do_box":         {"limit", "requirement", "regulation"},
}


def select_facts_for_section(facts: list[Fact], section_kind: str = "",
                              k: int = 60) -> list[Fact]:
    """Section-aware отбор фактов вместо «первые N» (#3).

    Ранжирует по: audit_priority, релевантности категории секции, наличию
    условий/исключений (глубина), числовому значению; затем диверсифицирует по
    банкам (round-robin), чтобы ни один банк не вытеснил остальных из топа.
    """
    if not facts:
        return []
    prio = {"high": 2.0, "medium": 1.0, "low": 0.0}
    boost = _SECTION_CAT_BOOST.get(section_kind, set())

    def _score(f: Fact) -> float:
        s = prio.get(f.audit_priority, 1.0)
        if f.category in boost:
            s += 2.0
        if f.conditions:
            s += 1.0
        if f.exceptions:
            s += 1.0
        if f.value_numeric is not None:
            s += 0.5
        if f.attribute == "продукт_доступен":
            s -= 5.0
        return s

    by_bank: dict[str, list[Fact]] = {}
    for f in sorted(facts, key=_score, reverse=True):
        by_bank.setdefault(f.entity_bank_slug, []).append(f)
    # round-robin по банкам
    out: list[Fact] = []
    idx = 0
    while len(out) < k and any(idx < len(v) for v in by_bank.values()):
        for v in by_bank.values():
            if idx < len(v):
                out.append(v[idx])
                if len(out) >= k:
                    break
        idx += 1
    return out


def facts_for_entity(facts: list[Fact], bank_slug: str) -> list[Fact]:
    """Подмножество фактов для конкретного банка."""
    return [f for f in facts if f.entity_bank_slug == bank_slug]


def facts_by_category(facts: list[Fact], categories: list[str]) -> list[Fact]:
    """Подмножество фактов по категориям (fee/rate/limit/...)."""
    cats = set(c.lower() for c in categories)
    return [f for f in facts if f.category.lower() in cats]


def facts_by_priority(facts: list[Fact], priorities: list[str]) -> list[Fact]:
    """Подмножество фактов по audit_priority."""
    prio = set(p.lower() for p in priorities)
    return [f for f in facts if f.audit_priority.lower() in prio]


# ════════════════════════════════════════════════════════════════════
# DEFAULT MODEL
# ════════════════════════════════════════════════════════════════════


def get_default_model(prefer_smart: bool = True) -> str:
    """Дефолтная модель для narrative-генерации."""
    import os
    if prefer_smart:
        return (os.getenv("LLM_MODEL_SMART") or
                  os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"))
    return (os.getenv("LLM_MODEL_FAST") or
              os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"))
