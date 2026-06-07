"""Risks & Recommendations — финальная секция отчёта.

Самая ВАЖНАЯ секция для аудитора (после key_findings). Здесь не просто
факты — а АНАЛИТИЧЕСКИЕ ВЫВОДЫ:
  • Какие сценарии создают РИСК для клиента или для банка
  • Что аудитору проверить на месте (recommendations)
  • Что запросить у банка дополнительно
  • Какие документы поднять у клиента
  • Какие нормативы могут быть нарушены

ПРИМЕР качественной структуры (из demo/doverennost.json):

| Сценарий | Риск | Что проверить аудитору |
|---|---|---|
| Доверенность на >1 года | Может истечь без уведомления клиента | Запросить копии уведомлений банка [4] |
| Снятие наличных по доверке >100k | Превышение лимита, отказ банка | Проверить наличие нотариального уд-я [3] |

Рекомендации:
1. Запросить регламент работы с доверенностями в каждом банке
2. Проверить наличие сверки реестра ЕИС нотариата
3. ...
"""
from __future__ import annotations
import asyncio, logging
from openai import AsyncOpenAI

from .base import (
    NarrativeContext,
    parse_json_object,
    enforce_citations,
    verify_numbers_in_text,
    format_facts_for_prompt,
    facts_by_priority,
    get_default_model,
)
from ..fact import Fact

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — главный аудитор финансовой компании, пишущий ИТОГОВЫЕ
рекомендации коллегам после сравнительного анализа продуктов разных банков.

Получаешь:
  • Все факты (что нашлось)
  • Перечень пробелов (что НЕ нашлось — критично для аудита)
  • Какие конфликты в источниках есть

Формируешь:
  1) Таблицу РИСК-СЦЕНАРИЕВ (3-5 строк)
  2) Список ACTIONABLE рекомендаций для аудитора (что сделать дальше)
  3) Список ВОПРОСОВ на которые нет ответа в открытых источниках

ПРАВИЛА:

1) РИСК-СЦЕНАРИЙ — конкретное действие → конкретное последствие.
   ✅ «Открыть продукт по доверке без нотариального уд-я → отказ при снятии >100k [3]»
   ❌ «Возможны риски» — расплывчато

2) РЕКОМЕНДАЦИИ — ДЕЙСТВИЯ, не общие фразы:
   ✅ «Запросить у банка регламент работы с доверенностями и реестр актов»
   ❌ «Изучить нормативную базу»

3) ВОПРОСЫ — то что нужно дозапросить:
   ✅ «У ВТБ не раскрыт тариф снятия наличных свыше 200k/мес — запросить»

4) Каждое утверждение → [N] цитата где применимо.

5) НЕ ВЫДУМЫВАЙ — только реальные риски из фактов и пробелов.

ВЫХОД: JSON:
{
  "risk_scenarios": [
    {"scenario": "Действие клиента/банка", "risk": "Последствие", "mitigation": "Что проверить", "source_idx": 3}
  ],
  "recommendations": [
    "Actionable рекомендация 1",
    "Actionable рекомендация 2"
  ],
  "open_questions": [
    "Вопрос на который нет ответа в источниках"
  ]
}

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext,
                    gaps: list[tuple[str, str]] | None = None,
                    conflicts: dict | None = None) -> str:
    """Генерирует Risks & Recommendations.

    gaps      — список (bank_slug, attribute) пустых клеток матрицы
    conflicts — конфликтующие триплы из matrix
    """
    if not ctx.facts:
        return _empty_section()

    high_facts = facts_by_priority(ctx.facts, ["high"])
    facts_str = format_facts_for_prompt(high_facts or ctx.facts, max_facts=30)

    gaps_str = "(нет)"
    if gaps:
        gaps_by_attr: dict[str, list[str]] = {}
        for bank, attr in gaps:
            gaps_by_attr.setdefault(attr, []).append(bank)
        gaps_str = "\n".join(
            f"  • {attr}: нет данных у {', '.join(banks)}"
            for attr, banks in list(gaps_by_attr.items())[:15]
        )

    conflicts_str = "(нет)"
    if conflicts:
        c_lines = []
        for (bank, attr), group in list(conflicts.items())[:10]:
            vals = " vs ".join(f"{g.value}{g.unit}" for g in group)
            c_lines.append(f"  • {bank} {attr}: {vals}")
        conflicts_str = "\n".join(c_lines)

    brief_block = ctx.brief_block("risks_recommendations")
    user_msg = (
        (brief_block + "\n\n" if brief_block else "")
        + f"# Вопрос аудитора\n{ctx.question}\n\n"
        f"# Главные факты ({len(high_facts or ctx.facts)})\n{facts_str}\n\n"
        f"# Пробелы (не раскрыто)\n{gaps_str}\n\n"
        f"# Конфликты в источниках\n{conflicts_str}\n\n"
        f"Опираясь на меморандум (критичные пробелы и ловушки), напиши "
        f"risk_scenarios + recommendations + open_questions. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    if not raw:
        return _fallback(ctx, gaps)

    data = parse_json_object(raw) or {}
    risks = data.get("risk_scenarios") or []
    recs = data.get("recommendations") or []
    open_q = data.get("open_questions") or []

    if not isinstance(risks, list):
        risks = []
    if not isinstance(recs, list):
        recs = []
    if not isinstance(open_q, list):
        open_q = []

    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}

    # Clean risks
    clean_risks = []
    for r in risks[:7]:
        if not isinstance(r, dict):
            continue
        scen = str(r.get("scenario") or "").strip()
        risk = str(r.get("risk") or "").strip()
        if not scen or not risk:
            continue
        mit = str(r.get("mitigation") or "").strip()
        # Verify
        all_t = scen + " " + risk + " " + mit
        ok, _ = verify_numbers_in_text(all_t, ctx.facts)
        if not ok:
            continue
        clean_risks.append({
            "scenario": enforce_citations(scen, allowed_src, require_for_numbers=False),
            "risk": enforce_citations(risk, allowed_src, require_for_numbers=False),
            "mitigation": enforce_citations(mit, allowed_src, require_for_numbers=False),
            "source_idx": r.get("source_idx"),
        })

    # Clean recommendations
    clean_recs = []
    for rec in recs[:8]:
        s = str(rec).strip()
        if not s:
            continue
        ok, _ = verify_numbers_in_text(s, ctx.facts)
        if not ok:
            continue
        s = enforce_citations(s, allowed_src, require_for_numbers=False)
        clean_recs.append(s)

    # Clean open_questions
    clean_q = [str(q).strip() for q in open_q if str(q).strip()][:8]

    return _render_md(clean_risks, clean_recs, clean_q)


def _render_md(risks: list[dict], recs: list[str], open_q: list[str]) -> str:
    parts = ["## ⚠️ Риски и рекомендации", ""]
    if risks:
        parts.append("### Риск-сценарии")
        parts.append("")
        parts.append("| Сценарий | Риск | Что проверить |")
        parts.append("|---|---|---|")
        for r in risks:
            scen = r["scenario"]
            cite = f" [{r['source_idx']}]" if r.get("source_idx") else ""
            mit = r["mitigation"] or "—"
            parts.append(f"| {scen}{cite} | {r['risk']} | {mit} |")
        parts.append("")
    if recs:
        parts.append("### Рекомендации аудитору")
        parts.append("")
        for i, r in enumerate(recs, 1):
            parts.append(f"{i}. {r}")
        parts.append("")
    if open_q:
        parts.append("### Открытые вопросы — требуется дозапрос")
        parts.append("")
        for q in open_q:
            parts.append(f"- {q}")
    return "\n".join(parts).rstrip()


def _empty_section() -> str:
    return ("## ⚠️ Риски и рекомендации\n\n"
            "_Недостаточно данных для содержательных рекомендаций. "
            "Требуется ручной аудит и дозапрос документации у банков._")


def _fallback(ctx: NarrativeContext,
                gaps: list[tuple[str, str]] | None) -> str:
    """Без LLM — список пробелов как рекомендации."""
    parts = ["## ⚠️ Риски и рекомендации", ""]
    parts.append("### Рекомендации аудитору (автоматические)")
    parts.append("")
    if gaps:
        by_attr: dict[str, list[str]] = {}
        for bank, attr in gaps:
            by_attr.setdefault(attr, []).append(bank)
        for attr, banks in sorted(by_attr.items())[:10]:
            bank_names = [next((e.bank_name for e in ctx.entities
                                  if e.bank_slug == b), b) for b in banks]
            parts.append(f"- Запросить **{attr}** у: {', '.join(bank_names)}")
    else:
        parts.append("- Все ключевые параметры раскрыты. Провести сверку с актуальными "
                      "тарифными документами банков.")
    return "\n".join(parts)


async def _llm_call(ctx: NarrativeContext, user_msg: str) -> str:
    try:
        resp = await asyncio.wait_for(
            ctx.client.chat.completions.create(
                model=ctx.model or get_default_model(),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=2000, temperature=0.0,
            ),
            timeout=60,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[risks_recommendations] LLM failed: %s", e)
        return ""
