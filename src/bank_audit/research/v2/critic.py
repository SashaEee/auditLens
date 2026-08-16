"""Critic — верификатор отчёта.

Отдельный вызов (не тот же, что Analyst — конфликт интересов). Проверяет:
  1. Числовая верификация: каждое число в отчёте ↔ есть в bundle.facts.
  2. Claim-grounding: сильные выводы подтверждены фактами/дельтами.
  3. Coverage: отчёт отвечает на все части вопроса.
  4. Пустоты/вода: есть ли места без опоры.

Если critic находит проблемы → orchestrator просит Analyst переписать с
конкретными замечаниями (одна итерация).

Числовая сверка — детерминированная сантехника на общем разборе чисел
(numbers.py): один парсер и для отчёта, и для фактов bundle.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from ...ai.llm_utils import deep_reasoning_extra
from ...clock import today_anchor
from . import numbers as _num
from .knowledge_bundle import KnowledgeBundle, Fact

log = logging.getLogger(__name__)


@dataclass
class Critique:
    ok: bool
    blocking_issues: list[str] = field(default_factory=list)
    weak_claims: list[str] = field(default_factory=list)
    missing_aspects: list[str] = field(default_factory=list)  # части вопроса без ответа
    numeric_hallucinations: list[float] = field(default_factory=list)
    citation_errors: list[dict] = field(default_factory=list)  # claim ↔ источник [N] не бьются
    subject_mismatch: str = ""   # отчёт отвечает НЕ на тот вопрос (подмена предмета)
    # Части вопроса, которых НЕТ в bundle: переписывание их не вернёт, поэтому
    # это единственные замечания, которые честно показать аудитору как есть.
    unanswered: list[str] = field(default_factory=list)
    repair_directive: str = ""  # инструкция для переписывания


SYSTEM_PROMPT = """Ты — критик аудиторских отчётов. Твоя задача — проверить
ЧЕРНОВИК отчёта на качество и достоверность, опираясь на KNOWLEDGE BUNDLE.

Проверяешь 4 аспекта:

0. ПРЕДМЕТ ВОПРОСА (проверяй ПЕРВЫМ). Определи, о чём спросил аудитор: о
   сравнении банков, о документе/данных регулятора, о жалобах, о процессе.
   Затем определи, о чём НА САМОМ ДЕЛЕ написан отчёт. Если это разные вещи —
   заполни subject_mismatch одной фразой: «спросили <X>, отчёт про <Y>».
   Классический случай: спросили таблицу значений ЦБ — отчёт сравнивает ставки
   банков по витринам. Для аудитора это «неверные данные», даже когда каждое
   отдельное число верное. Подмена предмета — всегда blocking_issue.
   Если предмет совпадает — subject_mismatch пустая строка.

1. ОТВЕЧАЕТ ЛИ НА ВОПРОС: разбей вопрос аудитора на части. Каждая часть
   должна быть освещена. Если что-то пропущено (напр. просили рейтинг — нет
   рейтинга) — это blocking_issue.
   ВАЖНО разделять две причины пропуска:
     • данные ЕСТЬ в bundle, но писатель их не использовал → это чинится
       переписыванием, пиши в blocking_issues / repair_directive;
     • данных в bundle НЕТ вообще → переписывание не поможет, никакая
       директива их не создаст. Такое пиши в unanswered — коротко и по-русски,
       чего именно не хватает («предельные значения ПСК на II квартал 2026 —
       в источниках нет ни одной категории»). Это увидит аудитор, поэтому
       формулируй как честную оговорку, а не как упрёк писателю.

2. CLAIM-GROUNDING: каждое сильное утверждение («Сбер дороже», «Т-Банк
   надёжнее») должно опираться на конкретные факты из bundle. Голословные
   выводы → weak_claims.

3. ЧИСЛОВАЯ ДОСТОВЕРНОСТЬ: числа в отчёте должны быть из bundle (с тем же
   значением). Если число выдумано или искажено → numeric_hallucinations.

4. ПУСТОТЫ/ВОДА: абзацы без фактической опоры, повторы, маркетинговый тон.
   → weak_claims.

5. GROUNDING ЦИТАТ (КРИТИЧНО для аудита!): для каждого утверждения со ссылкой [N]
   найди источник N в разделе ИСТОЧНИКИ и сверь. Если утверждение ПРОТИВОРЕЧИТ
   тексту источника или НЕ подтверждается им — это citation_error: грубейшая ошибка,
   отчёт уверенно врёт со ссылкой. Пример: отчёт пишет «остаётся в SWIFT [42]», а
   источник 42 говорит об ОТКЛЮЧЕНИИ банка от SWIFT → citation_error. Проверяй
   только то, что реально процитировано; если источника N нет в разделе — не штрафуй.

ВЫХОД (строгий JSON):
{
  "ok": false,                      // true только если серьёзных проблем нет
  "subject_mismatch": "",           // «спросили X, отчёт про Y» либо пустая строка
  "unanswered": ["предельные значения ПСК на II квартал 2026 — в источниках нет"],
  "blocking_issues": ["Нет рейтинга, хотя аудитор просил"],
  "weak_claims": ["«Сбер надёжнее» — голословно, нет опоры"],
  "missing_aspects": ["рейтинг"],
  "numeric_hallucinations": [],
  "citation_errors": [
    {"claim":"Сбербанк остаётся в SWIFT","source_n":42,"issue":"источник 42 говорит об ОТКЛЮЧЕНИИ Сбера от SWIFT (6-й пакет) — утверждение противоречит источнику"}
  ],
  "repair_directive": "Добавь рейтинг-таблицу (он есть в bundle). Замени голословное утверждение на «Сбер дороже на 1,5% [3]». ИСПРАВЬ/УБЕРИ утверждение про SWIFT [42] — оно противоречит источнику."
}

Если отчёт хороший — верни {"ok":true,"blocking_issues":[],...} с пустым
repair_directive, пустым subject_mismatch и пустым unanswered.
"""


async def critique_report(client: AsyncOpenAI, report_md: str,
                            bundle: KnowledgeBundle, question: str,
                            model: str | None = None,
                            on_reasoning=None) -> Critique:
    """Верифицирует отчёт. Возвращает Critique с замечаниями."""
    if len(report_md) < 200:
        return Critique(ok=False, blocking_issues=["Отчёт слишком короткий / пустой"])

    model = model or os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                                 "gpt-4o-mini")

    # Сначала детерминированная проверка чисел (быстро, без LLM)
    halluc_nums = _check_numbers(report_md, bundle)

    # Волна 4: критик обязан видеть ВЕСЬ отчёт и богатый контекст — раньше
    # report_md[:12000]+bundle 14k против 44k у писателя: хвостовые секции
    # (риски, рекомендации — ради них отчёт и читают) не проверялись вовсе.
    context = bundle.to_prompt_context(max_chars=24000)
    # Grounding цитат: даём критику excerpt'ы ТОЛЬКО реально процитированных в
    # отчёте источников [N] (фокус + лимит контекста), чтобы он сверил утверждения
    # с первоисточником и поймал «враньё со ссылкой» (claim ↔ источник расходятся).
    src_block = _cited_sources_block(report_md, bundle)
    user_msg = (
        f"# ВОПРОС АУДИТОРА\n{question}\n\n"
        f"# ЧЕРНОВИК ОТЧЁТА\n{report_md[:30000]}\n\n"
        f"# KNOWLEDGE BUNDLE\n{context}\n\n"
        + (f"# ИСТОЧНИКИ (excerpt'ы для проверки цитат [N])\n{src_block}\n\n"
           if src_block else "")
        + "Проверь отчёт, ВКЛЮЧАЯ grounding цитат [N] по разделу ИСТОЧНИКИ. JSON."
    )
    _msgs = [{"role": "system", "content": today_anchor() + "\n\n" + SYSTEM_PROMPT},
             {"role": "user", "content": user_msg}]
    try:
        if on_reasoning is not None:
            from ._streaming import stream_completion
            raw, _r, _t = await stream_completion(
                client, on_reasoning=on_reasoning,
                model=model, messages=_msgs, temperature=0.0,
                max_tokens=2200, extra_body=deep_reasoning_extra())
            raw = (raw or "").strip()
        else:
            resp = await client.chat.completions.create(
                model=model, messages=_msgs,
                temperature=0.0, max_tokens=2200,
                extra_body=deep_reasoning_extra(),  # верификация/grounding — reasoning: effort=high
            )
            raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[critic] LLM failed: %s — only deterministic check", e)
        return Critique(ok=len(halluc_nums) == 0,
                          numeric_hallucinations=halluc_nums)

    data = _parse_json(raw)
    if not data:
        return Critique(ok=len(halluc_nums) == 0,
                          numeric_hallucinations=halluc_nums)

    # Дополняем LLM-проверку детерминированными находками
    llm_halluc = [float(x) for x in (data.get("numeric_hallucinations") or [])
                    if _is_number(x)]
    all_halluc = list(set(halluc_nums + llm_halluc))

    # Citation-grounding ошибки: утверждение противоречит/не подтверждается своим [N]
    cit_errs: list[dict] = []
    for ce in (data.get("citation_errors") or []):
        if not isinstance(ce, dict) or not str(ce.get("claim") or "").strip():
            continue
        sn = ce.get("source_n")
        cit_errs.append({
            "claim": str(ce.get("claim"))[:200],
            "source_n": int(sn) if str(sn).isdigit() else 0,
            "issue": str(ce.get("issue") or "")[:300],
        })
    cit_errs = cit_errs[:8]

    # Citation-ошибки — серьёзные: ok=False и обязательно в repair_directive,
    # чтобы Analyst переписал/убрал утверждения, противоречащие источникам.
    repair = str(data.get("repair_directive") or "")
    # Подмена предмета — самая дорогая ошибка: отчёт может быть безупречен по
    # числам и всё равно бесполезен. Ставим директиву ПЕРВОЙ, чтобы писатель
    # начал с возврата к заданному вопросу, а не с косметики формулировок.
    mismatch = str(data.get("subject_mismatch") or "").strip()
    if mismatch:
        repair = (f"ГЛАВНОЕ: отчёт отвечает не на тот вопрос ({mismatch}). "
                  "Перестрой отчёт вокруг заданного вопроса: сначала прямой "
                  "ответ по фактам bundle, и только потом смежный контекст. "
                  "Если прямого ответа в bundle нет — так и напиши первой "
                  "строкой, не заменяя его сравнением банков. "
                  + repair)
    if cit_errs:
        ce_txt = "; ".join(
            f"«{c['claim']}»" + (f" [{c['source_n']}]" if c['source_n'] else "")
            + f" — {c['issue']}" for c in cit_errs[:5])
        repair = ((repair + " ") if repair else "") + (
            "КРИТИЧНО (grounding): исправь по фактам или УБЕРИ утверждения, "
            f"противоречащие своим источникам: {ce_txt}.")

    return Critique(
        ok=(bool(data.get("ok")) and len(all_halluc) == 0
            and not cit_errs and not mismatch),
        subject_mismatch=mismatch[:300],
        unanswered=[str(x).strip() for x in (data.get("unanswered") or []) if str(x).strip()][:5],
        blocking_issues=[str(x) for x in (data.get("blocking_issues") or [])][:6],
        weak_claims=[str(x) for x in (data.get("weak_claims") or [])][:8],
        missing_aspects=[str(x) for x in (data.get("missing_aspects") or [])][:5],
        numeric_hallucinations=all_halluc[:10],
        citation_errors=cit_errs,
        repair_directive=repair,
    )


def _cited_sources_block(report_md: str, bundle: KnowledgeBundle,
                          max_chars: int = 11000) -> str:
    """Excerpt'ы источников [N], РЕАЛЬНО процитированных в отчёте — для grounding.
    Фокус на цитируемом (а не на всех 40+ источниках) → меньше контекста, точнее.
    """
    cited = []
    seen = set()
    for m in re.findall(r"\[(\d{1,3})\]", report_md):
        n = int(m)
        if n not in seen:
            seen.add(n); cited.append(n)
    if not cited:
        return ""
    try:
        by_n = {s["n"]: s for s in bundle.sources.to_ui()}
    except Exception:
        return ""
    lines = []
    for n in sorted(cited)[:40]:
        s = by_n.get(n)
        if not s:
            continue
        exc = (s.get("excerpt") or "").strip()
        if not exc:
            continue
        lines.append(f"[{n}] {s.get('domain','')} ({s.get('source_kind','')}): {exc[:350]}")
    return "\n".join(lines)[:max_chars]


# ════════════════════════════════════════════════════════════════════════
# Детерминированная проверка чисел (переиспользует base.py guards)
# ════════════════════════════════════════════════════════════════════════


def _check_numbers(report_md: str, bundle: KnowledgeBundle) -> list[float]:
    """Числа отчёта, которых нет в фактах bundle (кандидаты в выдумки).

    Берём только числа С ЕДИНИЦЕЙ (₽, %, п.п., лет). Год определяется ЕДИНИЦЕЙ
    измерения, а не диапазоном: прежняя проверка пропускала как «год» всё в
    интервале 1990-2050, из-за чего выдуманная комиссия «2 000 ₽» считалась
    безопасной. Разбор общий с оркестратором (numbers.py) — раньше два конца
    сверки читали числа по-разному, и дробные значения не совпадали никогда.
    """
    if not bundle.facts:
        return []
    # Волна 3: сверка знает единицы, множители и производные. Кандидат на
    # удаление — только то, что не совпало даже с допуском округления крупных
    # сумм И не является дельтой/кратным (их пересчитывают, а не вычищают).
    audit = _num.audit_report_numbers(report_md, bundle.facts)
    return audit["removal_candidates"][:10]


def _collect_fact_numbers(facts: list[Fact]) -> set[float]:
    """База сверки из фактов — единым разбором (см. numbers.py).

    Прежде здесь звался narrative_generators.base._facts_numbers, который ждёт
    полей value_numeric/exceptions/qualifications. У v2-Fact их нет — вызов
    всегда падал с AttributeError, и работал запасной разбор, выбрасывавший
    десятичную запятую: факт «27,608%» попадал в базу как 27608.
    """
    return _num.numbers_from_facts(facts)


def _collect_fact_numbers_lite(facts: list[Fact]) -> set[float]:
    """Оставлено для совместимости вызовов: разбор теперь один на всех."""
    return _num.numbers_from_facts(facts)


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        import json
        return json.loads(raw)
    except Exception:
        pass
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    start = t.find("{")
    if start < 0:
        return None
    depth = 0; in_str = False; esc = False; end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc: esc = False; continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: end = i + 1; break
    cand = t[start:end] if end > 0 else t[start:].rstrip().rstrip(",") + "}"
    try:
        import json
        return json.loads(cand)
    except Exception:
        return None


def _is_number(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False
