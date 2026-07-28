"""LLM-нарратив профиля интересов пользователя (само-дополняющаяся персонализация).

Читает недавние запросы + авто-счётчики интересов и просит insight-модель описать
в 2-3 предложениях, чем занимается аудитор — для персонализации дайджеста и подачи.
Best-effort: любой сбой → None, ничего не ломает.
"""
from __future__ import annotations

import logging
import os

from openai import AsyncOpenAI

from ..ai.analyst import insight_model
from ..ai.llm_utils import _patch_client_reasoning_effort
from . import userdata

log = logging.getLogger(__name__)

_SYS = (
    "Ты составляешь краткий профиль интересов аудитора банковского сектора для "
    "персонализации рабочего инструмента. На основе его недавних запросов и статистики "
    "опиши в 2–3 предложениях, чем он занимается: какие банки, продукты и темы в фокусе, "
    "какой характер задач. От третьего лица, по-русски, по-деловому, без воды, без "
    "обращения и без вводных вроде «на основе данных». Не выдумывай — только по данным."
)


def _client() -> AsyncOpenAI:
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    return _patch_client_reasoning_effort(
        AsyncOpenAI(base_url=base, api_key=key, timeout=60, max_retries=1))


async def generate_profile_note(username: str) -> str | None:
    """Генерирует и сохраняет нарратив профиля по запросам + ручному описанию.

    None если данных совсем мало (нет ни запросов, ни ручного описания) / сбой.
    """
    queries = userdata.recent_queries(username, 25)
    top = userdata.top_interests(username)
    user = userdata.get_user(username) or {}
    prefs = user.get("prefs") or {}
    if isinstance(prefs, str):
        import json
        prefs = json.loads(prefs or "{}")
    self_desc = (prefs.get("self_description") or "").strip()
    # Нужны хоть какие-то данные: ≥3 запроса ИЛИ ручное описание.
    if len(queries) < 3 and not self_desc:
        return None
    parts = []
    if self_desc:
        parts.append("Аудитор сам описал свою работу (это ПРИОРИТЕТ):\n" + self_desc)
    if queries:
        parts.append("Недавние запросы:\n" + "\n".join(f"- {q}" for q in queries[:25]))
    parts.append(
        f"Частые банки: {', '.join(top.get('banks') or []) or '—'}\n"
        f"Продукты: {', '.join(top.get('products') or []) or '—'}\n"
        f"Закреплено вручную: {', '.join((top.get('pinned') or [])+(top.get('custom') or [])) or '—'}"
    )
    user_msg = "\n\n".join(parts)
    try:
        r = await _client().chat.completions.create(
            model=insight_model(),
            messages=[{"role": "system", "content": _SYS},
                      {"role": "user", "content": user_msg}],
            temperature=0.3,
            max_tokens=240,
        )
        note = (r.choices[0].message.content or "").strip()
    except Exception:
        log.warning("[profile_ai] generate failed", exc_info=True)
        return None
    if note:
        try:
            userdata.set_profile_note(username, note)
        except Exception:
            log.warning("[profile_ai] save failed", exc_info=True)
    return note or None
