"""LLM-классификатор «лазейка/не лазейка» (langchain chain).

Использует ChatOpenAI (base_url/api_key из тех же env, что и ai/analyst.py).
Промпт — langchain ChatPromptTemplate. Парсинг ответа — через
bank_audit.ai.llm_utils._loose_json_loads (толерантный).

Контракт вердикта: {"is_loophole": bool, "confidence": float, "reason": str}.
Fail-safe: любой сбой/мусор → Verdict(is_loophole=False, confidence=0.0).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..ai.llm_utils import _loose_json_loads
from .config import LoopholeSettings
from .models import Verdict
from . import repository as repo

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — аудитор-аналитик. Определи, является ли описание банковской практики ЛАЗЕЙКОЙ/УЯЗВИМОСТЬЮ (недобросовестной, скрытой, нарушающей права клиента или регуляторные требования) или нормальной стандартной практикой.

Лазейка — это: скрытые комиссии, отказ в выдаче вклада, навязанные услуги, изменение ставки в одностороннем порядке, штрафы не по договору, нарушение 161-ФЗ, вводящие в заблуждение формулировки и т.п.

Верни СТРОГО JSON без преамбулы и markdown-fence:
{"is_loophole": <bool>, "confidence": <0.0-1.0>, "reason": "<краткое обоснование на русском>"}
"""


def _default_llm():
    """Создаёт langchain ChatOpenAI с теми же env, что и ai/analyst.py."""
    from langchain_openai import ChatOpenAI
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    model = LoopholeSettings.load().effective_classify_model()
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key, temperature=0.0)


def _build_prompt(text: str) -> list:
    """Сообщения для LLM. Использует langchain-сообщения, если доступен langchain,
    иначе — простые dict'ы (для мок-тестов)."""
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        return [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=text)]
    except Exception:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]


def _parse_verdict(raw: str) -> Verdict:
    """Парсит ответ LLM в Verdict. Fail-safe при мусоре."""
    if not raw:
        return Verdict(is_loophole=False, confidence=0.0, reason="empty_response")
    try:
        data = _loose_json_loads(raw)
    except Exception:
        return Verdict(is_loophole=False, confidence=0.0, reason="parse_fail")
    if not isinstance(data, dict):
        return Verdict(is_loophole=False, confidence=0.0, reason="not_dict")
    return Verdict(
        is_loophole=bool(data.get("is_loophole", False)),
        confidence=float(data.get("confidence", 0.0) or 0.0),
        reason=str(data.get("reason", ""))[:500],
    )


async def classify_text(text: str, *, llm: Any = None) -> Verdict:
    """Классифицирует текст записи. Возвращает Verdict.

    llm — опциональный langchain-совместимый ChatModel (для мок-тестов).
    """
    if llm is None:
        llm = _default_llm()
    messages = _build_prompt(text)
    try:
        resp = await llm.ainvoke(messages)
        raw = getattr(resp, "content", None) or str(resp)
    except Exception as e:
        log.warning("[classify] LLM failed: %s — fail-safe", e)
        return Verdict(is_loophole=False, confidence=0.0, reason="llm_error")
    return _parse_verdict(raw)


async def classify_record(
    record_id: int,
    *,
    llm: Any = None,
    model: str = "",
    session=None,
) -> Verdict:
    """Классифицирует запись из БД и сохраняет вердикт. Возвращает Verdict."""
    row = repo.get_record(record_id, session=session)
    if not row:
        return Verdict(is_loophole=False, confidence=0.0, reason="not_found")
    text = " ".join(
        str(x) for x in (row.get("title"), row.get("snippet"), row.get("raw_text")) if x
    )
    verdict = await classify_text(text, llm=llm)
    repo.update_verdict(
        record_id,
        is_loophole=verdict.is_loophole,
        confidence=verdict.confidence,
        reason=verdict.reason,
        model=model or LoopholeSettings.load().effective_classify_model(),
        session=session,
    )
    return verdict
