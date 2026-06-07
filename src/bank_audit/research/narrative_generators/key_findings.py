"""Key Findings — 3-5 главных инсайтов аудитора (narrative).

Это ПЕРВЫЙ блок отчёта. От его качества зависит впечатление.
Отличается от bullet-list тем, что каждое утверждение — связный текст
2-3 предложения, обосновано конкретными фактами с цитированием.

ПРИМЕР качественного key_finding (из demo/doverennost.json):
  «Из 5 проверенных банков лишь Сбер взимает фиксированную плату
   за оформление доверенности (от 290 ₽), остальные банки оформляют
   бесплатно [1, 4]. При этом у ВТБ услуга доступна только в премиум-
   сегменте (Привилегия+) [3], что аудитору важно учитывать при оценке
   массового использования.»

Антигаллюцинации:
  • Все числа должны быть в фактах (verify_numbers_in_text)
  • Цитаты [N] enforced
  • Если число вызывает подозрение → перегенерация
"""
from __future__ import annotations
import asyncio, logging, re
from openai import AsyncOpenAI

from .base import (
    NarrativeContext,
    parse_json_object,
    verify_numbers_in_text,
    enforce_citations,
    format_facts_for_prompt,
    facts_by_priority,
    get_default_model,
)
from ..fact import Fact
from ..entity_extractor import Entity

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — главный аудитор пишущий ИНСАЙТЫ для коллег.
На основе фактов о банковских продуктах ты формулируешь 3-5 ГЛАВНЫХ
наблюдений — то, на что аудитор должен обратить внимание в первую
очередь.

КАЖДЫЙ ИНСАЙТ — связный абзац 2-4 предложения:
  • Начни с самого важного факта (цифра/уникальность/ограничение)
  • Объясни в чём суть для аудитора (риск/возможность/нюанс)
  • Подтверди ссылкой [N] на источник в КОНЦЕ каждого утверждения

ПРАВИЛА:

1) ЦИФРЫ ТОЛЬКО ИЗ ФАКТОВ. Если в фактах есть «150 ₽» — пиши «150 ₽».
   Не выдумывай цифры. Если не уверен — формулируй качественно
   («некоторые банки» вместо «3 из 5»).

1a) НЕ СКЛАДЫВАЙ РАЗНОТИПНЫЕ ВЕЛИЧИНЫ. Разовую комиссию/страховку (% от суммы
    кредита или ₽) НЕЛЬЗЯ прибавлять к ГОДОВОЙ ставке, чтобы получить «APR/ПСК».
    Пример НЕВЕРНО: «ставка 16,5 % + страховка 24,9 % = эффективная ставка 41 %».
    ПСК/APR указывай ТОЛЬКО если он ЯВНО приведён в источнике как ПСК — не вычисляй
    сам. Стоимость страховки описывай отдельно как «разовый % от суммы», не как ставку.

2) [N] ОБЯЗАТЕЛЬНА после каждого утверждения с числом.
   Пример: «Сбер берёт 290 ₽ за оформление [4]».

3) ИЗБЕГАЙ:
   ❌ «Мы рекомендуем» / «Лучший вариант»  — это для секции рекомендаций
   ❌ «На рынке есть...» / «В целом видно...» — расплывчатые формулировки
   ❌ Маркетинговый тон («Отличное предложение!»)
   ❌ Повторение одного и того же в разных формулировках

4) СТРУКТУРА КАЖДОГО ИНСАЙТА:
   • КОНТРАСТ — что отличается между банками («только Сбер делает X»)
   • УСЛОВИЕ — что важно учесть («доступно только в Premium-сегменте»)
   • ИМПЛИКАЦИЯ — что это значит для аудитора («это создаёт риск...»)

4a) СОГЛАСОВАННОСТЬ заголовок↔текст: headline — это краткая суть narrative,
    они НЕ должны противоречить. Если в headline «только ВТБ указывает 1.4 млн»,
    то и narrative должен это утверждать про ВТБ (а не про другой банк).
    Не приписывай в заголовке одному банку то, что в тексте у другого.

4b) ГЛУБИНА «витрина↔реальность»: для рекламных «до X%»/«от Y₽» всегда
    раскрывай разрыв — заявленный максимум vs базовое значение vs условия его
    получения. Это самый ценный для аудитора слой анализа.

5) ТЕМЫ ДЛЯ ИНСАЙТОВ (выбирай 3-5 самых важных):
   • Самое большое расхождение по цене/ставке
   • Уникальное предложение одного банка которое нет у других
   • Скрытое условие/исключение которое легко упустить
   • Регуляторное требование которое не все соблюдают
   • Сегмент аудитории на который продукт НЕ распространяется
   • Дистанционные сервисы (если есть существенная разница)

ВЫХОД: JSON-объект:
{
  "findings": [
    {
      "headline": "Краткий заголовок (под 80 chars, ключевая мысль)",
      "narrative": "Полный абзац 2-4 предложения с [N] на каждом числе",
      "category": "pricing|access|regulatory|features|risk",
      "audit_severity": "high|medium|low"
    },
    ...
  ]
}

БЕЗ преамбулы, БЕЗ markdown-fences. Только чистый JSON."""


async def generate(ctx: NarrativeContext, max_findings: int = 5) -> str:
    """Главная функция: возвращает markdown-секцию «Ключевые выводы»."""
    if not ctx.facts:
        return "## ⚡ Ключевые выводы\n\n_Недостаточно данных для формирования выводов._"

    # Section-aware отбор (#3): ранжирование по приоритету/условиям/банкам,
    # а не «первые N». select_facts уже учитывает важность для инсайтов.
    from .base import select_facts_for_section
    priority_facts = select_facts_for_section(ctx.facts, "key_findings", k=60)
    if not priority_facts:
        priority_facts = ctx.facts

    facts_str = format_facts_for_prompt(priority_facts, max_facts=60)
    entities_str = ", ".join(e.bank_name for e in ctx.entities)
    brief_block = ctx.brief_block("key_findings")          # #1 меморандум + директива
    excerpts = ctx.excerpts_block()                          # #3 живой язык источников

    user_msg = (
        (brief_block + "\n\n" if brief_block else "")
        + f"# Вопрос аудитора\n{ctx.question}\n\n"
        f"# Сравниваемые банки\n{entities_str}\n\n"
        f"# ФАКТЫ ({len(priority_facts)})\n{facts_str}\n\n"
        + (f"# ДОСЛОВНЫЕ ВЫДЕРЖКИ ИСТОЧНИКОВ (ищи нюансы мелким шрифтом)\n{excerpts}\n\n" if excerpts else "")
        + f"Сформулируй {max_findings} ГЛАВНЫХ ИНСАЙТОВ для аудитора в духе тезиса "
        f"меморандума: не пересказ фактов, а аналитика (контраст между банками, "
        f"витрина↔реальность, скрытые условия, сравнение относительных величин — "
        f"можно «в N раз», «на X ₽/п.п.»). Каждый — абзац 2-4 предложения с [N]. "
        f"Верни JSON. БЕЗ markdown fences."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(ctx)

    data = parse_json_object(raw)
    if not data or "findings" not in data:
        log.warning("[key_findings] no JSON findings, fallback (raw 200=%r)", raw[:200])
        return _fallback(ctx)

    findings = data.get("findings", [])
    if not isinstance(findings, list) or not findings:
        return _fallback(ctx)

    # Антигаллюцинации: фильтруем findings с лже-цифрами
    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    clean_findings = []
    dropped = 0
    for f in findings[:max_findings]:
        if not isinstance(f, dict):
            continue
        narr = str(f.get("narrative") or "").strip()
        if not narr:
            continue
        # Verify numbers
        ok, halluc = verify_numbers_in_text(narr, ctx.facts)
        if not ok:
            log.warning("[key_findings] DROP finding (hallucinated nums: %s): %s",
                         halluc, narr[:80])
            dropped += 1
            continue
        # Enforce citations
        narr = enforce_citations(narr, allowed_src, require_for_numbers=True)
        clean_findings.append({
            "headline": str(f.get("headline") or "").strip(),
            "narrative": narr,
            "category": str(f.get("category") or "").strip().lower(),
            "audit_severity": str(f.get("audit_severity") or "medium").strip().lower(),
        })

    if not clean_findings:
        log.warning("[key_findings] all findings dropped — fallback")
        return _fallback(ctx)

    log.warning("[key_findings] %s findings (%s dropped)", len(clean_findings), dropped)
    return _render_md(clean_findings)


async def _llm_call(ctx: NarrativeContext, user_msg: str) -> str:
    """Безопасный вызов LLM."""
    try:
        resp = await asyncio.wait_for(
            ctx.client.chat.completions.create(
                model=ctx.model or get_default_model(),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=2500, temperature=0.0,
            ),
            timeout=60,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[key_findings] LLM failed: %s", e)
        return ""


def _render_md(findings: list[dict]) -> str:
    """findings → markdown."""
    lines = ["## ⚡ Ключевые выводы", ""]
    sev_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    for i, f in enumerate(findings, 1):
        em = sev_emoji.get(f.get("audit_severity", "medium"), "🟡")
        headline = f.get("headline") or f"Вывод {i}"
        lines.append(f"**{em} {headline}**")
        lines.append("")
        lines.append(f.get("narrative", ""))
        lines.append("")
    return "\n".join(lines).rstrip()


def _fallback(ctx: NarrativeContext) -> str:
    """Если LLM упал — собираем минимум из топ-фактов."""
    lines = ["## ⚡ Ключевые выводы", ""]
    high_facts = facts_by_priority(ctx.facts, ["high"])
    if not high_facts:
        return lines[0] + "\n\n_Недостаточно данных для автоматического вывода._"

    # Группируем по банку
    by_bank: dict[str, list[Fact]] = {}
    for f in high_facts[:12]:
        by_bank.setdefault(f.entity_bank_slug, []).append(f)
    for bank, fs in by_bank.items():
        bank_name = next((e.bank_name for e in ctx.entities if e.bank_slug == bank), bank)
        top = fs[0]
        cite = f" [{top.source_idx}]" if top.source_idx else ""
        lines.append(f"- **{bank_name}** — {top.attribute}: "
                      f"{top.value} {top.unit}{cite}".strip())
    return "\n".join(lines)
