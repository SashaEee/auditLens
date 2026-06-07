"""Per-Entity Breakdown — связный narrative по каждому банку.

Это секция-«мясо» отчёта. Заменяет старый bullet-list на связный текст
5-10 предложений в котором аудитор узнаёт:
  • Как банк позиционирует продукт
  • Какие условия применимы (цены / ставки / лимиты)
  • Кому доступен / не доступен (segment)
  • Что КАК ИСКЛЮЧЕНИЕ выходит за рамки
  • Какие требования к клиенту

ПРИМЕР качественного per-entity (из demo/doverennost.json для Сбер):
  «Сбербанк взимает плату 290 ₽ за оформление обычной доверенности и
   1 200 ₽ за нотариальное удостоверение по форме банка [4]. Услуга
   доступна во всех отделениях, требуется паспорт и СНИЛС, оформление
   занимает до 15 минут. Срок действия доверенности — до 3 лет, но банк
   рекомендует оформлять на 1 год для актуализации данных [5]. Важно:
   доверенность не распространяется на закрытие счёта и снятие более
   100 000 ₽ единовременно — для этих операций требуется присутствие
   доверителя [4].»

Параллельно генерирует narrative для каждого банка (asyncio.gather).
"""
from __future__ import annotations
import asyncio, logging
from openai import AsyncOpenAI

from .base import (
    NarrativeContext,
    parse_json_object,
    verify_numbers_in_text,
    enforce_citations,
    format_facts_for_prompt,
    facts_for_entity,
    get_default_model,
)
from ..fact import Fact
from ..entity_extractor import Entity

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — аудитор-аналитик. Пишешь СВЯЗНЫЙ narrative-абзац
5-10 предложений о продукте конкретного банка для коллеги-аудитора.

ПРАВИЛА:

1) СВЯЗНЫЙ ТЕКСТ, НЕ BULLET-LIST. Используй переходы:
   «Кроме того...», «Важно учитывать...», «Однако в случае...»,
   «При этом...», «Исключение составляет...», «Параметр доступен только...»

2) СТРУКТУРА (примерно):
   1) Стартуй с главного: что банк предлагает + основное число
   2) Дай 2-3 параметра с конкретикой
   3) Упомяни УСЛОВИЯ применения (conditions из фактов)
   4) Упомяни ИСКЛЮЧЕНИЯ (exceptions)
   5) Упомяни КОМУ доступно (qualifications) если ограничено
   6) Закрой риском/нюансом для аудитора

3) КАЖДОЕ УТВЕРЖДЕНИЕ С ЦИФРОЙ → [N] цитата в конце.
   Пример: «годовое обслуживание 0 ₽ при остатке от 30 000 ₽ [3]»

4) ИЗ ФАКТОВ можно ИЗВЛЕКАТЬ И СВЯЗЫВАТЬ, но НЕЛЬЗЯ ДОБАВЛЯТЬ НОВЫХ ЦИФР.
   Если в фактах нет — не пиши.

5) ИЗБЕГАЙ:
   ❌ «Лучший выбор» / маркетинговый тон
   ❌ «Можно предположить» / «Возможно»  — только то что в фактах
   ❌ Сравнение с другими банками (это в key_findings)
   ❌ Повторение значения 2-3 раза в разных формулировках

6) ЕСЛИ ФАКТОВ ОЧЕНЬ МАЛО (1-2):
   Напиши 2-3 предложения честно: «Из открытых источников установлено только...».
   Не выдумывай контекст.

ВЫХОД: JSON-объект:
{
  "narrative": "Полный связный текст 5-10 предложений с [N] на цифрах",
  "highlight_quote": "Самая важная цитата из verbatim_quote (если есть)",
  "missing_critical": ["список атрибутов которые НЕ нашлись и важны"]
}

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext, core_attrs: list[str] | None = None) -> str:
    """Главная: генерирует секцию per-bank для всех entity."""
    if not ctx.entities:
        return ""

    # Параллельный вызов LLM для каждого банка
    coros = []
    for e in ctx.entities:
        ent_facts = facts_for_entity(ctx.facts, e.bank_slug)
        coros.append(_generate_for_one(ctx, e, ent_facts, core_attrs or []))

    bank_blocks = await asyncio.gather(*coros, return_exceptions=False)

    lines = ["## 🏦 Детально по каждому банку", ""]
    for block in bank_blocks:
        if block:
            lines.append(block)
            lines.append("")
    return "\n".join(lines).rstrip()


async def _generate_for_one(ctx: NarrativeContext, entity: Entity,
                              facts: list[Fact], core_attrs: list[str]) -> str:
    """Narrative для одного банка."""
    if not facts:
        return _empty_block(entity)

    # «Особый случай»: единственный факт = «продукт_доступен: не найден»
    if len(facts) == 1 and facts[0].attribute == "продукт_доступен":
        f = facts[0]
        return (f"### {entity.bank_name}\n"
                f"_Продукт: {entity.product}_\n\n"
                f"⚠ {f.value}. {f.verbatim_quote or ''}".strip())

    facts_str = format_facts_for_prompt(facts, max_facts=30)
    core_hint = ""
    if core_attrs:
        core_hint = f"\n# Приоритетные атрибуты для упоминания\n{', '.join(core_attrs[:12])}\n"

    # #1 директива из меморандума + архетип банка (если синтез его выделил)
    brief_block = ctx.brief_block("per_entity_breakdown")
    archetype = ""
    try:
        if ctx.brief and getattr(ctx.brief, "bank_archetypes", None):
            archetype = ctx.brief.bank_archetypes.get(entity.bank_slug, "")
    except Exception:
        archetype = ""

    user_msg = (
        (brief_block + "\n\n" if brief_block else "")
        + f"# Банк: {entity.bank_name} ({entity.bank_slug})\n"
        f"# Продукт: {entity.product}\n"
        + (f"# Аудитория: {entity.audience}\n" if entity.audience else "")
        + (f"# Архетип этого банка (из общего разбора): {archetype}\n" if archetype else "")
        + core_hint +
        f"\n# Факты ({len(facts)})\n{facts_str}\n\n"
        f"Напиши связный аналитический narrative 5-10 предложений: не пересказ, а "
        f"разбор стратегии банка по продукту, условий и подвохов (витрина↔реальность). "
        f"Верни JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback_block(entity, facts)

    data = parse_json_object(raw)
    if not data or not data.get("narrative"):
        log.warning("[per_entity_breakdown] %s no narrative — fallback", entity.bank_slug)
        return _fallback_block(entity, facts)

    narrative = str(data.get("narrative") or "").strip()
    # Verify numbers
    ok, halluc = verify_numbers_in_text(narrative, facts)
    if not ok:
        log.warning("[per_entity_breakdown] %s hallucinated %s — fallback",
                     entity.bank_slug, halluc)
        return _fallback_block(entity, facts)

    # Enforce citations
    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    narrative = enforce_citations(narrative, allowed_src, require_for_numbers=True)

    highlight = str(data.get("highlight_quote") or "").strip()
    missing = data.get("missing_critical") or []
    if not isinstance(missing, list):
        missing = []

    return _render_block(entity, narrative, highlight, missing, facts)


async def _llm_call(ctx: NarrativeContext, user_msg: str) -> str:
    try:
        resp = await asyncio.wait_for(
            ctx.client.chat.completions.create(
                model=ctx.model or get_default_model(),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=1500, temperature=0.0,
            ),
            timeout=45,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[per_entity_breakdown] LLM failed: %s", e)
        return ""


_CATEGORY_LABELS = {
    "fee":         "💵 Комиссии и стоимость",
    "rate":        "💹 Ставки и проценты",
    "limit":       "📊 Лимиты",
    "requirement": "📋 Требования к клиенту",
    "feature":     "⚙️ Функции и условия",
    "regulation":  "📜 Регуляторное",
}
_CATEGORY_ORDER = ["fee", "rate", "limit", "requirement", "feature", "regulation"]
_PRIO_RANK = {"high": 0, "medium": 1, "low": 2}


def _facts_appendix(facts: list[Fact], max_total: int = 40) -> str:
    """Детерминированный реестр ВСЕХ параметров банка по категориям.

    Аудитору нужны все материальные факты, а не только упомянутые в narrative.
    Дедуп по атрибуту (лучший по приоритету), группировка по категории,
    условия/исключения и цитата [N] у каждого.
    """
    real = [f for f in facts if f.attribute != "продукт_доступен"]
    if not real:
        return ""
    # дедуп по атрибуту: оставляем самый приоритетный
    best: dict[str, Fact] = {}
    for f in real:
        prev = best.get(f.attribute)
        if prev is None or _PRIO_RANK.get(f.audit_priority, 3) < _PRIO_RANK.get(prev.audit_priority, 3):
            best[f.attribute] = f
    uniq = list(best.values())
    # группировка
    by_cat: dict[str, list[Fact]] = {}
    for f in uniq:
        by_cat.setdefault(f.category if f.category in _CATEGORY_LABELS else "feature", []).append(f)

    lines = ["", f"**Полный реестр параметров ({len(uniq)}):**", ""]
    shown = 0
    for cat in _CATEGORY_ORDER:
        group = by_cat.get(cat)
        if not group:
            continue
        group.sort(key=lambda f: _PRIO_RANK.get(f.audit_priority, 3))
        lines.append(f"_{_CATEGORY_LABELS[cat]}:_")
        for f in group:
            if shown >= max_total:
                break
            val = f"{f.value} {f.unit}".strip()
            cite = f" [{f.source_idx}]" if f.source_idx else ""
            extra = ""
            if f.conditions:
                extra += f" — при условиях: {'; '.join(f.conditions[:3])}"
            if f.exceptions:
                extra += f" — исключения: {'; '.join(f.exceptions[:2])}"
            if f.qualifications:
                extra += f" — {f.qualifications}"
            attr_h = f.attribute.replace("_", " ")
            lines.append(f"- **{attr_h}**: {val}{extra}{cite}")
            shown += 1
        lines.append("")
        if shown >= max_total:
            lines.append("_(показаны первые параметры; полный список — в источниках)_")
            break
    return "\n".join(lines).rstrip()


def _render_block(entity: Entity, narrative: str, highlight: str,
                    missing: list[str], facts: list[Fact]) -> str:
    n_high = sum(1 for f in facts if f.audit_priority == "high")
    real_n = sum(1 for f in facts if f.attribute != "продукт_доступен")
    parts = [
        f"### {entity.bank_name}",
        f"_Продукт: {entity.product}_  •  _Фактов: {real_n} (high: {n_high})_",
        "",
        narrative,
    ]
    if highlight:
        parts.append("")
        parts.append(f"> **Источник дословно:** _{highlight[:300]}_")
    # Полный реестр параметров (аудит-глубина: показываем ВСЕ факты, не только narrative)
    appendix = _facts_appendix(facts)
    if appendix:
        parts.append("")
        parts.append(appendix)
    if missing:
        clean_missing = [str(m).strip() for m in missing if m][:5]
        if clean_missing:
            parts.append("")
            parts.append(f"⚠ **Не раскрыто:** {', '.join(clean_missing)}")
    return "\n".join(parts)


def _fallback_block(entity: Entity, facts: list[Fact]) -> str:
    """Если LLM упал — bullet-list из топ фактов."""
    parts = [
        f"### {entity.bank_name}",
        f"_Продукт: {entity.product}_  •  _Фактов: {len(facts)}_",
        "",
        f"_Автоматическая narrative-генерация не удалась, ниже сырые факты:_",
        "",
    ]
    # Сортируем: high → medium → low
    prio_rank = {"high": 0, "medium": 1, "low": 2}
    sorted_facts = sorted(facts, key=lambda f: prio_rank.get(f.audit_priority, 3))
    for f in sorted_facts[:10]:
        cite = f" [{f.source_idx}]" if f.source_idx else ""
        cond = f" (при условиях: {', '.join(f.conditions[:2])})" if f.conditions else ""
        parts.append(f"- **{f.attribute}**: {f.value} {f.unit}{cond}{cite}".strip())
    return "\n".join(parts)


def _empty_block(entity: Entity) -> str:
    return (f"### {entity.bank_name}\n"
            f"_Продукт: {entity.product}_\n\n"
            f"⚠ Не найдено фактов в источниках. Возможные причины: "
            f"продукт отсутствует у банка, информация не публикуется, "
            f"или поисковая выборка слишком узкая. Требуется ручная проверка.")
