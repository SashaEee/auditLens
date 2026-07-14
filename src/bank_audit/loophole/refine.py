"""Уточнение ключевых слов на основе накопленной БД (langchain LLM).

Анализирует топ записей с is_loophole=TRUE и предлагает новые ключевые слова
для расширения охвата авто-сборщика. Сохраняет их через keywords.add_refined.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..ai.llm_utils import _loose_json_loads
from . import repository as repo
from . import keywords as kw_mod
from .config import LoopholeSettings

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — аналитик, уточняющий ключевые слова для поиска лазеек в банковских продуктах.
На входе — список описаний выявленных лазеек (title + snippet). Предложи 3-7 НОВЫХ ключевых слов/фраз на русском, которые расширят охват поиска и найдут похожие лазейки.

Требования:
- конкретные фразы (не одно слово), как поисковые запросы;
- на русском;
- не дублировать уже известные слова из списка existing;
- в формате JSON: {"keywords": ["фраза 1", "фраза 2", ...]}
"""


def _default_llm():
    from langchain_openai import ChatOpenAI
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    model = LoopholeSettings.load().effective_classify_model()
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key, temperature=0.3)


def _build_messages(loopholes: list[dict], existing: list[str]) -> list:
    user = (
        f"Известные лазейки:\n"
        + "\n".join(
            f"- {l.get('title') or ''}: {l.get('snippet') or ''}" for l in loopholes[:20]
        )
        + f"\n\nУже известные ключевые слова: {', '.join(existing) or '(нет)'}\n\n"
        "Предложи новые ключевые слова в формате JSON."
    )
    try:
        from langchain_core.messages import SystemMessage, HumanMessage
        return [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user)]
    except Exception:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]


def _parse_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        data = _loose_json_loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        kws = data.get("keywords") or []
    elif isinstance(data, list):
        kws = data
    else:
        return []
    return [str(k).strip() for k in kws if str(k).strip()][:10]


async def refine_keywords(
    *,
    llm: Any = None,
    session=None,
    limit: int = 50,
) -> list[str]:
    """Анализирует БД и добавляет уточнённые ключевые слова. Возвращает список новых."""
    # Топ выявленных лазеек.
    loopholes = repo.search_relevant("", only_loophole=True, limit=limit, session=session)
    if not loopholes:
        log.info("[refine] нет выявленных лазеек — нечего уточнять")
        return []
    existing = kw_mod.active_keywords(session=session)
    if llm is None:
        llm = _default_llm()
    messages = _build_messages(loopholes, existing)
    try:
        resp = await llm.ainvoke(messages)
        raw = getattr(resp, "content", None) or str(resp)
    except Exception as e:
        log.warning("[refine] LLM failed: %s", e)
        return []
    new_kws = _parse_keywords(raw)
    # Дедуп против существующих.
    existing_set = set(existing)
    added: list[str] = []
    for kw in new_kws:
        if kw in existing_set:
            continue
        kw_mod.add_refined(kw, session=session)
        existing_set.add(kw)
        added.append(kw)
    return added
