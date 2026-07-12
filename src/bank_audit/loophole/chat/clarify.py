"""Clarification-воронка для чат-агента loophole.

Адаптация ``bank_audit.ai.clarify`` под модуль loophole: промпт из
``chat/prompt/01_clarify.md``, флаг ``LOOPHOLE_ASKING_ENABLED`` (дефолт «1»),
fail-open (любой сбой → ``{"complete": true}``).

Контракт:
  generate_clarifications(question, history) -> dict
  build_enriched_question(question, answers) -> str
"""
from __future__ import annotations

import logging
import os
from typing import Any

from openai import AsyncOpenAI

from ...ai.llm_utils import (
    _loose_json_loads,
    _patch_client_reasoning_effort,
    deep_reasoning_extra,
    detect_bank_slugs,
    normalize_question,
)
from .tools import load_prompt

log = logging.getLogger(__name__)

_MAX_QUESTIONS = 5
_TOP_BANKS = ["sberbank", "tinkoff", "alfabank", "vtb"]


def clarify_enabled() -> bool:
    return os.getenv("LOOPHOLE_ASKING_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _clarify_model() -> str:
    return (
        os.getenv("LOOPHOLE_ASKING_MODEL")
        or os.getenv("LLM_MODEL_SMART")
        or os.getenv("LLM_MODEL_NAME", "gpt-4o")
    )


def _client() -> AsyncOpenAI:
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    c = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=70, max_retries=2)
    return _patch_client_reasoning_effort(c)


def _validate(data: Any) -> dict:
    """Нормализует/обрезает ответ модели. При любой кривизне → complete=true."""
    if not isinstance(data, dict):
        return {"complete": True, "questions": [], "reason": "parse_fail"}
    if data.get("complete") is True:
        return {
            "complete": True,
            "questions": [],
            "reason": str(data.get("reason", ""))[:200],
        }
    qs_in = data.get("questions") or []
    if not isinstance(qs_in, list) or not qs_in:
        return {"complete": True, "questions": [], "reason": "no_questions"}
    out: list[dict] = []
    seen_ids: set[str] = set()
    for q in qs_in[:_MAX_QUESTIONS]:
        if not isinstance(q, dict):
            continue
        text = q.get("question") or q.get("text")
        if not text:
            continue
        qtype = q.get("type") if q.get("type") in ("single", "multi", "text") else "single"
        opts: list[dict] = []
        for o in (q.get("options") or []):
            if isinstance(o, dict) and (o.get("label") or o.get("value")):
                label = str(o.get("label") or o.get("value"))
                opts.append({
                    "value": str(o.get("value") or label),
                    "label": label[:80],
                    "recommended": bool(o.get("recommended")),
                })
            elif isinstance(o, str):
                opts.append({"value": o, "label": o[:80], "recommended": False})
        if qtype != "text" and not opts:
            continue
        base = str(q.get("id") or f"q{len(out)}")
        qid = base
        n = 1
        while qid in seen_ids:
            qid = f"{base}_{n}"
            n += 1
        seen_ids.add(qid)
        out.append({
            "id": qid,
            "question": str(text)[:200],
            "type": qtype,
            "allow_other": bool(q.get("allow_other", True)),
            "options": opts[:6],
        })
    if not out:
        return {"complete": True, "questions": [], "reason": "all_questions_invalid"}
    return {
        "complete": False,
        "questions": out,
        "reason": str(data.get("reason", ""))[:200],
    }


async def generate_clarifications(
    question: str,
    history: list | None = None,
) -> dict:
    """Решает полноту запроса и (если неполный) генерирует уточняющие вопросы.

    Fail-open: при любом сбое → ``{"complete": true}`` (никогда не блокируем).
    """
    if not clarify_enabled():
        return {"complete": True, "questions": [], "reason": "disabled"}
    q = normalize_question(question or "")
    if len(q) < 3:
        return {"complete": True, "questions": [], "reason": "too_short"}
    hinted = detect_bank_slugs(q)
    system = load_prompt("01_clarify")
    user_msg = (
        f"Запрос аудитора:\n{q}\n\n"
        f"Банки, явно упомянутые в запросе: "
        f"{', '.join(hinted) if hinted else '(не указаны — предложи топ-4 + другое)'}\n\n"
        f"Верни JSON по контракту."
    )
    try:
        resp = await _client().chat.completions.create(
            model=_clarify_model(),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user_msg}],
            temperature=0.0,
            max_tokens=2500,
            extra_body=deep_reasoning_extra(),
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[loophole.clarify] LLM failed: %s — fail-open", e)
        return {"complete": True, "questions": [], "reason": "llm_error"}
    try:
        data = _loose_json_loads(raw)
    except Exception:
        log.warning("[loophole.clarify] no JSON parse, raw200=%r — fail-open", raw[:200])
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
    res = []
    for a in (answers or []):
        if not isinstance(a, dict):
            continue
        vals = [str(x) for x in (a.get("selected") or []) if str(x).strip()]
        oth = (a.get("other") or "").strip()
        if oth:
            vals.append(oth)
        if vals:
            res.append({
                "question": str(a.get("question") or "").strip(),
                "vals": vals,
            })
    return res


def _template_fallback(question: str, answered: list) -> str:
    if not answered:
        return question
    bits = "; ".join(
        f"{a['question'].rstrip('?')}: {', '.join(a['vals'])}" for a in answered
    )
    return f"{question} (уточнения — {bits})"


async def build_enriched_question(question: str, answers: list) -> str:
    """Исходный запрос + ответы воронки → обогащённый NL-запрос."""
    q = (question or "").strip()
    answered = _answers_summary(answers)
    if not answered:
        return q
    bits = "\n".join(f"— {a['question']}: {', '.join(a['vals'])}" for a in answered)
    user_msg = f"Исходный запрос:\n{q}\n\nОтветы аудитора на уточнения:\n{bits}"
    try:
        resp = await _client().chat.completions.create(
            model=_clarify_model(),
            messages=[{"role": "system", "content": SYSTEM_PROMPT_REWRITE},
                      {"role": "user", "content": user_msg}],
            temperature=0.2,
            max_tokens=900,
        )
        enriched = (resp.choices[0].message.content or "").strip().strip('"').strip()
    except Exception as e:
        log.warning("[loophole.clarify] rewrite failed: %s — template fallback", e)
        return _template_fallback(q, answered)
    if not enriched or len(enriched) < len(q) // 2:
        return _template_fallback(q, answered)
    allowed = set(detect_bank_slugs(q))
    for a in answered:
        allowed |= set(detect_bank_slugs(" ".join(a["vals"])))
    enriched_banks = set(detect_bank_slugs(enriched))
    if enriched_banks and not enriched_banks.issubset(allowed | set(_TOP_BANKS)):
        return _template_fallback(q, answered)
    return enriched
