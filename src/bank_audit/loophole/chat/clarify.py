"""Clarification-воронка для чат-агента loophole.

Адаптация ``bank_audit.ai.clarify`` под модуль loophole: промпт из
``chat/prompt/01_clarify.md``, флаг ``LOOPHOLE_ASKING_ENABLED`` (дефолт «1»),
fail-closed (любой сбой блокирует запуск агента до повторной попытки).

Контракт:
  generate_clarifications(question, history) -> dict
  build_enriched_question(question, answers) -> str
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from hashlib import sha256
from typing import Any, Literal, TypedDict

from openai import AsyncOpenAI

from ...ai.llm_utils import (
    _loose_json_loads,
    _patch_client_reasoning_effort,
    deep_reasoning_extra,
    detect_bank_slugs,
    normalize_question,
)
from .. import repository as repo
from ..pii_mask import mask as pii_mask
from .tools_nanobot import load_prompt

log = logging.getLogger(__name__)

_MAX_QUESTIONS = 5
_TOP_BANKS = ["sberbank", "tinkoff", "alfabank", "vtb"]
_TOKEN_TTL_SECONDS = 600
_MAX_PENDING_TOKENS = 4096
_clarification_tokens: dict[str, tuple[str, float]] = {}
_execution_tokens: dict[str, tuple[str, float]] = {}


def clarification_questions(value: Any) -> list[dict]:
    """Возвращает вопросы для UI, включая безопасный fallback при incomplete."""
    if not isinstance(value, dict) or value.get("complete") is True:
        return []
    questions = value.get("questions")
    if isinstance(questions, list) and questions:
        return questions
    return [{
        "id": "query_scope",
        "question": "Уточните запрос: что именно исследовать — банк, продукт или период?",
        "type": "text",
        "allow_other": True,
        "options": [],
    }]


def _mask_for_llm(value: Any) -> str:
    """Маскирует ПДн и типовые credentials перед отправкой в LLM."""
    masked, _ = pii_mask(str(value or ""))
    return repo.redact_audit_text(masked, limit=10000)


def _masked_history(history: list | None) -> str:
    """Форматирует историю для prompt только после redaction пользовательского текста."""
    lines = []
    for msg in history or []:
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
            lines.append(f"{msg['role']}: {_mask_for_llm(msg.get('content', ''))}")
    return "\n".join(lines) or "(история отсутствует)"


class ClarificationUnavailable(TypedDict):
    """Типизированный fail-closed результат проверки/переписывания."""

    complete: Literal[False]
    questions: list[dict]
    reason: Literal["clarification_unavailable", "answers_required"]


def _clarification_unavailable() -> ClarificationUnavailable:
    """Безопасный результат при невозможности проверить полноту запроса."""
    return {
        "complete": False,
        "questions": [],
        "reason": "clarification_unavailable",
    }


def _clarification_answers_required() -> ClarificationUnavailable:
    """Возвращает состояние clarification без разрешения на execution."""
    return {
        "complete": False,
        "questions": [],
        "reason": "answers_required",
    }


def _token_fingerprint(user_id: str, workspace_id: int | None, query: str) -> str:
    """Создаёт digest контекста без хранения исходного запроса в token state."""
    payload = f"{user_id}\x00{workspace_id}\x00{normalize_question(query)}"
    return sha256(payload.encode("utf-8")).hexdigest()


def _purge_tokens(store: dict[str, tuple[str, float]]) -> None:
    now = time.monotonic()
    expired = [token for token, (_, expires) in store.items() if expires <= now]
    for token in expired:
        store.pop(token, None)
    while len(store) >= _MAX_PENDING_TOKENS:
        store.pop(next(iter(store)))


def _issue_token(
    store: dict[str, tuple[str, float]],
    *,
    user_id: str,
    workspace_id: int | None,
    query: str,
) -> str:
    _purge_tokens(store)
    token = secrets.token_urlsafe(32)
    store[token] = (
        _token_fingerprint(user_id, workspace_id, query),
        time.monotonic() + _TOKEN_TTL_SECONDS,
    )
    return token


def _consume_token(
    store: dict[str, tuple[str, float]],
    token: str | None,
    *,
    user_id: str,
    workspace_id: int | None,
    query: str,
) -> bool:
    if not token:
        return False
    record = store.get(token)
    if record is None or record[1] <= time.monotonic():
        store.pop(token, None)
        return False
    if record[0] != _token_fingerprint(user_id, workspace_id, query):
        return False
    store.pop(token, None)
    return True


def issue_clarification_token(
    *, user_id: str, workspace_id: int | None, query: str
) -> str:
    """Выдаёт одноразовый server-side challenge для ответа на вопросы."""
    return _issue_token(
        _clarification_tokens,
        user_id=user_id,
        workspace_id=workspace_id,
        query=query,
    )


def consume_clarification_token(
    token: str | None,
    *,
    user_id: str,
    workspace_id: int | None,
    query: str,
) -> bool:
    """Проверяет и поглощает challenge ровно один раз."""
    return _consume_token(
        _clarification_tokens,
        token,
        user_id=user_id,
        workspace_id=workspace_id,
        query=query,
    )


def issue_execution_token(
    *, user_id: str, workspace_id: int | None, query: str
) -> str:
    """Выдаёт одноразовое разрешение на запуск обогащённого запроса."""
    return _issue_token(
        _execution_tokens,
        user_id=user_id,
        workspace_id=workspace_id,
        query=query,
    )


def consume_execution_token(
    token: str | None,
    *,
    user_id: str,
    workspace_id: int | None,
    query: str,
) -> bool:
    """Проверяет execution token по trusted user/workspace/query и использует его."""
    return _consume_token(
        _execution_tokens,
        token,
        user_id=user_id,
        workspace_id=workspace_id,
        query=query,
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
    """Нормализует ответ модели; повреждённый ответ блокирует запуск."""
    if not isinstance(data, dict):
        return _clarification_unavailable()
    if data.get("complete") is True:
        return {
            "complete": True,
            "questions": [],
            "reason": str(data.get("reason", ""))[:200],
        }
    qs_in = data.get("questions") or []
    if not isinstance(qs_in, list) or not qs_in:
        return _clarification_unavailable()
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
        return _clarification_unavailable()
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

    При ошибке LLM или JSON возвращает безопасный отказ без запуска агента.
    """
    q = normalize_question(question or "")
    if len(q) < 3:
        return {
            "complete": False,
            "questions": [],
            "reason": "query_too_short",
        }
    safe_q = _mask_for_llm(q)
    hinted = detect_bank_slugs(q)
    system = load_prompt("01_clarify")
    user_msg = (
        f"Запрос аудитора:\n{safe_q}\n\n"
        f"История диалога:\n{_masked_history(history)}\n\n"
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
    except Exception:  # noqa: BLE001 — любой сбой LLM должен быть fail-closed
        log.warning("[loophole.clarify] LLM failed — fail-closed")
        return _clarification_unavailable()
    try:
        data = _loose_json_loads(raw)
    except Exception:  # noqa: BLE001 — повреждённый ответ не должен запускать агента
        log.warning("[loophole.clarify] no JSON parse — fail-closed")
        return _clarification_unavailable()
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


async def build_enriched_question(
    question: str,
    answers: list,
) -> str | ClarificationUnavailable:
    """Исходный запрос + ответы воронки → обогащённый NL-запрос.

    Сбой rewrite возвращает типизированный fail-closed результат: строка для
    execution не выдаётся, пока запрос не пройдёт повторную проверку.
    """
    q = (question or "").strip()
    answered = _answers_summary(answers)
    if not answered:
        return _clarification_answers_required()
    safe_q = _mask_for_llm(q)
    safe_answered = [
        {
            "question": _mask_for_llm(a["question"]),
            "vals": [_mask_for_llm(value) for value in a["vals"]],
        }
        for a in answered
    ]
    bits = "\n".join(
        f"— {a['question']}: {', '.join(a['vals'])}" for a in safe_answered
    )
    user_msg = f"Исходный запрос:\n{safe_q}\n\nОтветы аудитора на уточнения:\n{bits}"
    try:
        resp = await _client().chat.completions.create(
            model=_clarify_model(),
            messages=[{"role": "system", "content": SYSTEM_PROMPT_REWRITE},
                      {"role": "user", "content": user_msg}],
            temperature=0.2,
            max_tokens=900,
        )
        enriched = (resp.choices[0].message.content or "").strip().strip('"').strip()
    except Exception:  # noqa: BLE001 — сбой rewrite не должен запускать агента
        log.warning("[loophole.clarify] rewrite failed — fail-closed")
        return _clarification_unavailable()
    if not enriched or len(enriched) < len(q) // 2:
        return _template_fallback(q, answered)
    allowed = set(detect_bank_slugs(q))
    for a in answered:
        allowed |= set(detect_bank_slugs(" ".join(a["vals"])))
    enriched_banks = set(detect_bank_slugs(enriched))
    if enriched_banks and not enriched_banks.issubset(allowed | set(_TOP_BANKS)):
        return _template_fallback(q, answered)
    return enriched
