"""Triple Extractor — самое сердце EAV-pipeline.

Принимает: Entity + список gold sources.
Возвращает: список троек (attribute, value, unit, source) с цитатами.

Главное: НЕ задаёт схему заранее. LLM сам решает какие attribute'ы у этого
продукта существуют. На эквайринге будут «комиссия за транзакцию»,
на ипотеке — «ставка», на доверенности — «срок действия».

Структура каждой тройки строгая:
  {
    "attribute":     "минимальная_ставка",   # snake_case на русском
    "value":         "6.0",                  # СТРОКА (для унификации)
    "unit":          "%",                    # ед. изм. или ""
    "value_numeric": 6.0,                    # parsed float если число
    "source_idx":    1,                      # 1-based index в gold_sources
    "excerpt":       "...ставка от 6%...",   # 200-300 chars цитата
    "confidence":    "high|medium|low",
  }
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from .entity_extractor import Entity
from .source_finder import GoldSource

log = logging.getLogger(__name__)


@dataclass
class Triple:
    """Один факт о entity, привязанный к источнику."""
    entity_bank_slug: str
    attribute: str             # snake_case ru
    value: str
    unit: str = ""
    value_numeric: float | None = None
    source_idx: int = 0        # 1-based
    source_url: str = ""
    excerpt: str = ""
    confidence: str = "high"   # high/medium/low

    def to_dict(self) -> dict:
        return {
            "bank":          self.entity_bank_slug,
            "attribute":     self.attribute,
            "value":         self.value,
            "unit":          self.unit,
            "value_numeric": self.value_numeric,
            "source_idx":    self.source_idx,
            "source_url":    self.source_url,
            "excerpt":       self.excerpt[:300],
            "confidence":    self.confidence,
        }


SYSTEM_PROMPT = """Ты — экстрактор фактов из банковских документов. Твоя
задача — извлечь ВСЕ конкретные характеристики ПРОДУКТА из текста, в виде
структурированных троек (атрибут, значение, единица_измерения).

ПРАВИЛА:

1) АТРИБУТЫ — НАЗВАНИЯ ХАРАКТЕРИСТИК ПРОДУКТА:
   • snake_case на русском: "минимальная_ставка", "годовая_комиссия",
     "лимит_снятия_наличных", "максимальная_сумма", "срок_действия_доверенности",
     "первоначальный_взнос", "кэшбэк_базовый", "возрастные_ограничения"
   • НЕ копируй фразу из текста дословно. Нормализуй ("комиссия за выпуск" =
     "выпуск_карты_бесплатно" если 0₽, или "комиссия_за_выпуск" с value=300).
   • Каждый атрибут УНИКАЛЕН в выходе.

2) ЗНАЧЕНИЯ — ТО ЧТО ИЗМЕРЯЕТСЯ:
   • Числовые: "6.5", "300000", "30" (как строка для унификации)
   • Перечисления: "паспорт, СНИЛС, справка 2-НДФЛ"
   • Булево: "да", "нет"
   • Диапазон: "от 6 до 22" с unit="%" (или две отдельные тройки min/max)

3) ЕДИНИЦЫ ИЗМЕРЕНИЯ: "%", "₽", "руб/мес", "лет", "дней", "млн руб",
   "тыс руб/день", "рабочих дней". Без unit для перечислений и булевых.

4) ИСТОЧНИК: source_idx — это НОМЕР В ПЕРЕДАННОМ СПИСКЕ sources (1-based).
   Excerpt — короткая цитата 100-300 chars, дословно из текста.

5) CONFIDENCE:
   • "high"   — точные числа/факты прямо в продуктовом документе банка
   • "medium" — пересказ или со страницы агрегатора
   • "low"    — из обзора/блога/отзыва

6) НЕ ИЗВЛЕКАЙ:
   ❌ Маркетинговые слоганы («Лучший выбор!», «Удобно и быстро»)
   ❌ Числа из ПРОМО-АКЦИЙ если они не относятся к базовым условиям продукта
     (различай: «годовое обслуживание 0 ₽» — продуктовый факт ✅
                «бонус 25% по акции CityDrive» — это акция ❌)
   ❌ Универсальные банковские правила («звоните на 900»)
   ❌ Off-topic информацию (про другие продукты этого банка)

7) ЕСЛИ В ТЕКСТЕ НЕТ КОНКРЕТНЫХ ФАКТОВ О ПРОДУКТЕ — верни пустой массив [].
   Это лучше чем выдумать.

ВЫХОД: JSON массив троек. БЕЗ преамбулы, БЕЗ markdown-fences.
[
  {"attribute":"...", "value":"...", "unit":"...", "source_idx":1,
   "excerpt":"...", "confidence":"high"},
  ...
]"""


def _build_sources_block(sources: list[GoldSource], max_chars_per: int = 3500) -> str:
    """Формирует блок с источниками для LLM."""
    parts = []
    for i, s in enumerate(sources, 1):
        title = (s.title or s.url)[:120]
        body = (s.text or "")[:max_chars_per]
        parts.append(f"### Source [{i}] — {title}\nURL: {s.url}\n\n{body}")
    return "\n\n---\n\n".join(parts)


def _parse_json_array(raw: str) -> list | None:
    """Толерантный парсер JSON-массива (тот же что в entity_extractor)."""
    if not raw:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    start = t.find("[")
    if start < 0:
        return None
    depth = 0; in_str = False; esc = False; end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc: esc = False; continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0: end = i + 1; break
    candidate = t[start:end] if end > 0 else (t[start:].rstrip().rstrip(",") + "]")
    try:
        return json.loads(candidate)
    except Exception:
        pass
    cleaned = re.sub(r",\s*([\]}])", r"\1", candidate)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _try_parse_numeric(val: str, unit: str) -> float | None:
    """Парсит численное значение если возможно."""
    if not val:
        return None
    # Извлекаем первое число
    m = re.search(r"-?\d+(?:[.,]\d+)?", val)
    if not m:
        return None
    s = m.group(0).replace(",", ".")
    try:
        f = float(s)
    except Exception:
        return None
    # Конверсия единиц: млн → ×1e6, тыс → ×1e3
    ul = (unit or "").lower()
    if "млрд" in ul: f *= 1e9
    elif "млн" in ul: f *= 1e6
    elif "тыс" in ul: f *= 1e3
    return f


async def extract_triples(client: AsyncOpenAI, entity: Entity,
                           sources: list[GoldSource],
                           model: str | None = None,
                           focus_attribute: str | None = None) -> list[Triple]:
    """Извлекает тройки из gold sources для одного entity.

    focus_attribute — опциональная фокусировка (для gap-filling: «найди
    только годовая_комиссия»). Если задан — промпт добавляет требование.
    """
    if not sources:
        return []
    model = model or os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                                 "gpt-4o-mini")
    sources_block = _build_sources_block(sources)
    user_msg = (
        f"# ENTITY\nБанк: {entity.bank_name} (slug: {entity.bank_slug})\n"
        f"Продукт: {entity.product}\n"
        + (f"Аудитория: {entity.audience}\n" if entity.audience else "")
        + (f"\n# FOCUS\nИзвлекай ТОЛЬКО факты по атрибуту «{focus_attribute}»\n"
            if focus_attribute else "")
        + f"\n# SOURCES\n{sources_block}\n\n"
        f"Извлеки тройки. Помни: НЕ выдумывай чисел, только из текста sources. "
        f"source_idx — НОМЕР источника (1-{len(sources)})."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=4000, temperature=0.0,
            ),
            timeout=60,
        )
    except Exception as e:
        log.warning("[triple_extractor] %s LLM call failed: %s", entity.bank_slug, e)
        return []
    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_json_array(raw)
    if not isinstance(data, list):
        log.warning("[triple_extractor] %s no JSON array (raw 200=%r)",
                     entity.bank_slug, raw[:200])
        return []

    triples: list[Triple] = []
    seen_attrs: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        attr = (item.get("attribute") or "").strip().lower().replace(" ", "_")
        if not attr or attr in seen_attrs:
            continue
        value = str(item.get("value") or "").strip()
        if not value or value.lower() in ("null", "none", "—", "-", ""):
            continue
        unit = str(item.get("unit") or "").strip()
        try:
            src_idx = int(item.get("source_idx") or 0)
        except Exception:
            src_idx = 0
        if src_idx < 1 or src_idx > len(sources):
            continue
        excerpt = str(item.get("excerpt") or "").strip()
        conf = (item.get("confidence") or "high").lower()
        if conf not in ("high", "medium", "low"):
            conf = "medium"
        seen_attrs.add(attr)
        triples.append(Triple(
            entity_bank_slug=entity.bank_slug,
            attribute=attr,
            value=value,
            unit=unit,
            value_numeric=_try_parse_numeric(value, unit),
            source_idx=src_idx,
            source_url=sources[src_idx - 1].url,
            excerpt=excerpt[:300],
            confidence=conf,
        ))

    log.warning("[triple_extractor] %s × %s → %s triples",
                 entity.bank_slug, entity.product[:30], len(triples))
    return triples
