"""Pricing Breakdown — детальная таблица стоимости / тарифов.

Цель: дать аудитору ОДНУ страницу с ВСЕМИ ценами по всем банкам,
с условиями применения, исключениями и сегментами.

Входные факты: только category in {fee, rate, limit}.

Структура:
  • Markdown-таблица: банк × tariff_attribute
    с указанием условий в подстроках («при остатке от 30k»)
  • Затем narrative-абзац объясняющий главные различия

ПРИМЕР качественного pricing breakdown:
  | Параметр | Сбер | ВТБ | Альфа |
  | Годовое обслуживание | 0 ₽ при остатке от 30k [1] | 0 ₽ (всегда) [2] | 990 ₽ [3] |
  | Кешбэк по покупкам | до 1.5% [1] | до 2% [2] | до 1% (только Premium) [3] |
  | Снятие наличных | бесплатно [1] | бесплатно до 200k/мес [2] | 1.5% [3] |

  Сбер и ВТБ предлагают бесплатное обслуживание при выполнении условий
  (остаток / зачисление пенсии), Альфа берёт фиксированную плату [3].
  При этом ВТБ имеет лимит на бесплатное снятие 200k ₽/мес,
  превышение тарифицируется 1% [2].
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
    facts_by_category,
    get_default_model,
)
from ..fact import Fact
from ..entity_extractor import Entity

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — аудитор-финансист пишущий ДЕТАЛЬНУЮ РАЗБИВКУ
СТОИМОСТИ продукта по банкам.

Сначала анализируешь — какие тарифы есть у банков, какие условия их активируют.
Затем формулируешь 1-2 абзаца текста-вывода про различия.

ПРАВИЛА:

1) ВЕРНИ JSON:
   {
     "intro": "1 предложение что включено в разбивку",
     "narrative": "1-2 абзаца про различия между банками с [N] цитатами",
     "key_pricing_diffs": [
        {
           "attribute": "name",
           "summary": "Краткое описание разницы (1 предложение, с [N])"
        }
     ]
   }

2) ЦИФРЫ — ТОЛЬКО ИЗ ФАКТОВ. Никаких «примерно 500 ₽».

3) ОБЯЗАТЕЛЬНО упоминай УСЛОВИЯ (conditions) и ИСКЛЮЧЕНИЯ (exceptions):
   ✅ «Сбер берёт 0 ₽ при остатке от 30k, иначе 990 ₽ [3]»
   ❌ «Сбер берёт 0 ₽» — теряется ключевое условие

4) ОТМЕЧАЙ когда условие применимо ТОЛЬКО к segment:
   ✅ «У Альфы кешбэк 5% доступен только Premium-клиентам [4]»

5) В key_pricing_diffs выбирай 3-5 самых ВАЖНЫХ различий:
   • Где между банками разница в разы (1 vs 5 раз)
   • Где у одного банка есть условие, а у других нет
   • Где есть скрытые/неочевидные комиссии

БЕЗ преамбулы, БЕЗ markdown fences."""


async def generate(ctx: NarrativeContext) -> str:
    """Главная: генерирует pricing-секцию."""
    pricing_facts = facts_by_category(ctx.facts, ["fee", "rate", "limit"])
    if not pricing_facts:
        return ""

    facts_str = format_facts_for_prompt(pricing_facts, max_facts=60)
    entities_str = ", ".join(e.bank_name for e in ctx.entities)

    brief_block = ctx.brief_block("pricing_breakdown")
    user_msg = (
        (brief_block + "\n\n" if brief_block else "")
        + f"# Сравниваемые банки\n{entities_str}\n\n"
        f"# Все ценовые/тарифные факты ({len(pricing_facts)})\n{facts_str}\n\n"
        f"Напиши pricing-breakdown: narrative со сравнением относительных величин "
        f"(во сколько раз/на сколько ₽ дороже) и 3-5 key_pricing_diffs. JSON."
    )

    raw = await _llm_call(ctx, user_msg)
    table_md = _render_pricing_table(ctx, pricing_facts)

    if not raw:
        return _wrap("## 💵 Разбивка стоимости", table_md, narrative="",
                       diffs=[])

    data = parse_json_object(raw) or {}
    narrative = str(data.get("narrative") or "").strip()
    intro = str(data.get("intro") or "").strip()
    diffs = data.get("key_pricing_diffs") or []
    if not isinstance(diffs, list):
        diffs = []

    # Antihalluc
    allowed_src = {s.get("n") for s in ctx.sources_index if s.get("n")}
    if narrative:
        ok, halluc = verify_numbers_in_text(narrative, pricing_facts)
        if not ok:
            log.warning("[pricing_breakdown] narrative drop (halluc=%s)", halluc)
            narrative = ""
        else:
            narrative = enforce_citations(narrative, allowed_src,
                                             require_for_numbers=True)

    clean_diffs = []
    for d in diffs[:5]:
        if not isinstance(d, dict):
            continue
        summary = str(d.get("summary") or "").strip()
        attr = str(d.get("attribute") or "").strip()
        if not summary or not attr:
            continue
        ok, _ = verify_numbers_in_text(summary, pricing_facts)
        if not ok:
            continue
        summary = enforce_citations(summary, allowed_src, require_for_numbers=True)
        clean_diffs.append({"attribute": attr, "summary": summary})

    return _wrap("## 💵 Разбивка стоимости", table_md, narrative=narrative,
                   diffs=clean_diffs, intro=intro)


def _render_pricing_table(ctx: NarrativeContext, facts: list[Fact]) -> str:
    """Markdown таблица: attribute × bank."""
    if not facts:
        return ""
    # Group by attribute
    attrs = sorted({f.attribute for f in facts})
    banks = [(e.bank_slug, e.bank_name) for e in ctx.entities]

    by_key: dict[tuple[str, str], Fact] = {}
    for f in facts:
        key = (f.entity_bank_slug, f.attribute)
        # При нескольких — берём по audit_priority high+
        prev = by_key.get(key)
        prio_rank = {"high": 0, "medium": 1, "low": 2}
        if (prev is None or
            prio_rank.get(f.audit_priority, 3) < prio_rank.get(prev.audit_priority, 3)):
            by_key[key] = f

    header = "| Параметр | " + " | ".join(b[1] for b in banks) + " |"
    sep    = "|---" + ("|---" * len(banks)) + "|"
    rows = [header, sep]
    for attr in attrs:
        cells = [attr.replace("_", " ")]
        any_value = False
        for slug, _ in banks:
            f = by_key.get((slug, attr))
            if f is None:
                cells.append("—")
                continue
            val = f"{f.value} {f.unit}".strip()
            if f.conditions:
                val += f" _({f.conditions[0][:60]})_"
            if f.source_idx:
                val += f" [{f.source_idx}]"
            cells.append(val)
            any_value = True
        if any_value:
            rows.append("| " + " | ".join(cells) + " |")

    return "\n".join(rows)


def _wrap(title: str, table_md: str, narrative: str,
            diffs: list[dict], intro: str = "") -> str:
    parts = [title, ""]
    if intro:
        parts.append(intro)
        parts.append("")
    if table_md:
        parts.append(table_md)
        parts.append("")
    if narrative:
        parts.append(narrative)
        parts.append("")
    if diffs:
        parts.append("**Ключевые различия:**")
        parts.append("")
        for d in diffs:
            parts.append(f"- **{d['attribute']}** — {d['summary']}")
    return "\n".join(parts).rstrip()


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
        log.warning("[pricing_breakdown] LLM failed: %s", e)
        return ""
