"""Модуль «asking» — clarification-воронка ПЕРЕД deep research.

Срабатывает ТОЛЬКО если запрос реально неполный. Идея: пользователи плохо
формулируют промпты → research обобщает → плохой результат. Воронка задаёт
2-4 точечных вопроса с кликабельными вариантами, собирает ответы и обогащает
промпт. Промпт — в первую очередь КЛАССИФИКАТОР полноты (complete=true → скип),
а не генератор вопросов ради вопросов.

Сетевой контракт (для /api/ai/clarify):
  generate_clarifications(q)        -> {"complete": bool, "reason": str, "questions": [...]}
  build_enriched_question(q, ans)   -> str  (NL-запрос для research)

Fail-open: любой сбой/мусорный JSON → {"complete": true} (никогда не блокируем).
За флагом ASKING_ENABLED (env, дефолт off).
"""
from __future__ import annotations

import logging
import os

from openai import AsyncOpenAI

from .analyst import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL_NAME
from .llm_utils import (_patch_client_reasoning_effort, deep_reasoning_extra,
                        _loose_json_loads, normalize_question, detect_bank_slugs)

log = logging.getLogger(__name__)

_TOP_BANKS = ["sberbank", "tinkoff", "alfabank", "vtb"]
_MAX_QUESTIONS = 4


def clarify_enabled() -> bool:
    return os.getenv("ASKING_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def _clarify_model() -> str:
    return (os.getenv("LLM_MODEL_REASONING") or os.getenv("LLM_MODEL_ANALYST")
            or os.getenv("LLM_MODEL_SMART") or LLM_MODEL_NAME)


def _client() -> AsyncOpenAI:
    c = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=70, max_retries=2)
    return _patch_client_reasoning_effort(c)


SYSTEM_PROMPT_CLARIFY = """Ты — ФИЛЬТР ПОЛНОТЫ запроса для аудиторской deep-research системы по банковским продуктам РФ.
Единственная задача: решить, достаточно ли в запросе деталей, чтобы исследование дало КОНКРЕТНЫЙ (не обобщённый) ответ. Если да — пропустить. Если нет — задать 2-4 точечных уточняющих вопроса.

ПРАВИЛА:
1. Запрос уже конкретен (ясно: продукт/аспект + что сравнивать + по возможности банки) → верни {"complete": true, "questions": []}. НЕ выдумывай вопросы ради вопросов — это раздражает пользователя.
2. Запрос расплывчат → {"complete": false, "questions": [...]} с 2-4 вопросами СТРОГО про недостающие оси ИМЕННО ЭТОГО запроса.
   ЗАПРЕЩЕНО унифицировать. Спрашивай по сути продукта:
     • переводы → тип перевода (исходящие/входящие/загран/между своими/C2C);
     • карты → тип карты, валюта, сегмент (дебет/кредит);
     • вклады → срок, сумма, капитализация;
     • ипотека/кредиты → сегмент заёмщика, цель, сумма/срок.
   Вопрос задавай только если ответ РЕАЛЬНО меняет результат.
3. Максимум 4 вопроса. 2 точных лучше, чем 4 общих.
4. Каждый вопрос: 2-5 кликабельных вариантов (options) + allow_other:true. type: "single" (один), "multi" (несколько), "text" (свободный ответ, options=[]).
5. Если банки в запросе УЖЕ названы — НЕ спрашивай про банки. Если нет — дай вариант "Топ-4: Сбербанк, Альфа-Банк, Т-Банк, ВТБ" (recommended:true) + пару конкретных.

Верни СТРОГО JSON (без преамбулы, без markdown-fence):
{"complete": bool, "reason": "<кратко>", "questions": [
  {"id":"<snake_case>", "question":"<текст>", "type":"single|multi|text", "allow_other": true,
   "options":[{"value":"<машинный>","label":"<для UI>","hint":"<опц короткий>","recommended":<опц bool>}]}]}

ПРИМЕР A (полный запрос):
"Сравни комиссии за исходящие SWIFT-переводы физлиц в Сбербанке и Т-Банке"
→ {"complete": true, "questions": [], "reason": "продукт, аспект, тип перевода и банки заданы"}

ПРИМЕР B (расплывчатый):
"Проанализируй условия переводов в разных банках"
→ {"complete": false, "reason": "не заданы тип перевода, банки, фокус",
"questions": [
{"id":"transfer_type","question":"Какие переводы анализируем?","type":"single","allow_other":true,
 "options":[{"value":"outgoing_intl","label":"Исходящие заграничные","recommended":true},
 {"value":"incoming","label":"Входящие"},{"value":"c2c_internal","label":"Между своими счетами"},
 {"value":"c2c_sbp","label":"На карты других банков (СБП/C2C)"}]},
{"id":"banks","question":"Какие банки сравнить?","type":"multi","allow_other":true,
 "options":[{"value":"top4","label":"Топ-4: Сбер, Альфа, Т-Банк, ВТБ","recommended":true},
 {"value":"sberbank","label":"Сбербанк"},{"value":"gazprombank","label":"Газпромбанк"}]},
{"id":"focus","question":"Что важнее всего в сравнении?","type":"text","allow_other":false,"options":[]}]}
"""


def _validate(data: dict) -> dict:
    """Нормализуем/обрезаем ответ модели. При любой кривизне → complete=true."""
    if not isinstance(data, dict):
        return {"complete": True, "questions": [], "reason": "parse_fail"}
    if data.get("complete") is True:
        return {"complete": True, "questions": [], "reason": str(data.get("reason", ""))[:200]}
    qs_in = data.get("questions") or []
    if not isinstance(qs_in, list) or not qs_in:
        return {"complete": True, "questions": [], "reason": "no_questions"}
    out = []
    for q in qs_in[:_MAX_QUESTIONS]:
        if not isinstance(q, dict) or not q.get("question"):
            continue
        qtype = q.get("type") if q.get("type") in ("single", "multi", "text") else "single"
        opts = []
        for o in (q.get("options") or []):
            if isinstance(o, dict) and o.get("label"):
                opts.append({"value": str(o.get("value") or o["label"]),
                             "label": str(o["label"])[:80],
                             "hint": (str(o["hint"])[:80] if o.get("hint") else None),
                             "recommended": bool(o.get("recommended"))})
        if qtype != "text" and not opts:
            continue  # вариативный вопрос без опций бесполезен
        out.append({"id": str(q.get("id") or f"q{len(out)}"),
                    "question": str(q["question"])[:200], "type": qtype,
                    "allow_other": bool(q.get("allow_other", True)),
                    "options": opts[:6]})
    if not out:
        return {"complete": True, "questions": [], "reason": "all_questions_invalid"}
    return {"complete": False, "questions": out, "reason": str(data.get("reason", ""))[:200]}


async def generate_clarifications(question: str, history: list | None = None) -> dict:
    """Решает полноту запроса и (если неполный) генерит уточняющие вопросы."""
    if not clarify_enabled():
        return {"complete": True, "questions": [], "reason": "disabled"}
    q = normalize_question(question or "")
    if len(q) < 3:
        return {"complete": True, "questions": [], "reason": "too_short"}
    hinted = detect_bank_slugs(q)
    user_msg = (f"Запрос аудитора:\n{q}\n\n"
                f"Банки, явно упомянутые в запросе: "
                f"{', '.join(hinted) if hinted else '(не указаны — предложи топ-4 + другое)'}\n\n"
                f"Верни JSON по контракту.")
    try:
        resp = await _client().chat.completions.create(
            model=_clarify_model(),
            messages=[{"role": "system", "content": SYSTEM_PROMPT_CLARIFY},
                      {"role": "user", "content": user_msg}],
            temperature=0.0, max_tokens=2500, extra_body=deep_reasoning_extra())
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[clarify] LLM failed: %s — fail-open (complete)", e)
        return {"complete": True, "questions": [], "reason": "llm_error"}
    data = _loose_json_loads(raw)
    if data is None:
        try:
            from ..research.v2.conductor import _salvage_truncated_json
            salv = _salvage_truncated_json(raw)
            if salv:
                data = _loose_json_loads(salv)
        except Exception:
            pass
    if data is None:
        log.warning("[clarify] no JSON parse, raw200=%r — fail-open", raw[:200])
        return {"complete": True, "questions": [], "reason": "parse_fail"}
    return _validate(data)


# ── Сборка обогащённого промпта ──────────────────────────────────────────────

SYSTEM_PROMPT_REWRITE = """Ты переформулируешь запрос аудитора, вплетая его уточнения в ЕДИНЫЙ чёткий research-запрос на русском, естественным языком.
ЖЁСТКИЕ ПРАВИЛА:
• Сохрани названия банков ДОСЛОВНО (как в исходнике/ответах) — они нужны системе для распознавания.
• НИЧЕГО не добавляй от себя: не выдумывай банки, продукты, параметры, которых нет в исходном запросе или ответах.
• НЕ отвечай на запрос — только переформулируй его с учётом уточнений.
• Верни ОДНУ строку — готовый запрос. Без преамбулы, без кавычек."""


def _answers_summary(answers: list) -> list:
    """Оставляем только реально отвеченные вопросы. answers:
    [{question, selected:[label], other:str|None}]."""
    res = []
    for a in (answers or []):
        if not isinstance(a, dict):
            continue
        vals = [str(x) for x in (a.get("selected") or []) if str(x).strip()]
        oth = (a.get("other") or "").strip()
        if oth:
            vals.append(oth)
        if vals:
            res.append({"question": str(a.get("question") or "").strip(), "vals": vals})
    return res


def _template_fallback(question: str, answered: list) -> str:
    """Детерминированная склейка — fallback при сбое LLM-rewrite. Некрасиво,
    но рабочий research-запрос со всеми ключевыми словами/банками."""
    if not answered:
        return question
    bits = "; ".join(f"{a['question'].rstrip('?')}: {', '.join(a['vals'])}" for a in answered)
    return f"{question} (уточнения — {bits})"


async def build_enriched_question(question: str, answers: list) -> str:
    """Исходный запрос + ответы воронки → обогащённый NL-запрос для research.
    LLM-rewrite (гладкий) с детерминированным fallback и анти-галлюцинацией банков."""
    q = (question or "").strip()
    answered = _answers_summary(answers)
    if not answered:
        return q  # пропустили всё / нечем обогащать → исходный
    bits = "\n".join(f"— {a['question']}: {', '.join(a['vals'])}" for a in answered)
    user_msg = f"Исходный запрос:\n{q}\n\nОтветы аудитора на уточнения:\n{bits}"
    try:
        resp = await _client().chat.completions.create(
            model=_clarify_model(),
            messages=[{"role": "system", "content": SYSTEM_PROMPT_REWRITE},
                      {"role": "user", "content": user_msg}],
            temperature=0.2, max_tokens=900)
        enriched = (resp.choices[0].message.content or "").strip().strip('"').strip()
    except Exception as e:
        log.warning("[clarify] rewrite failed: %s — template fallback", e)
        return _template_fallback(q, answered)
    if not enriched or len(enriched) < len(q) // 2:
        return _template_fallback(q, answered)
    # Анти-галлюцинация: банки в enriched ⊆ (банки исходника ∪ ответов).
    allowed = set(detect_bank_slugs(q))
    for a in answered:
        allowed |= set(detect_bank_slugs(" ".join(a["vals"])))
    enriched_banks = set(detect_bank_slugs(enriched))
    if enriched_banks and not enriched_banks.issubset(allowed | set(_TOP_BANKS)):
        log.warning("[clarify] rewrite добавил банки %s вне разрешённых — fallback",
                    enriched_banks - (allowed | set(_TOP_BANKS)))
        return _template_fallback(q, answered)
    return enriched
