"""LLM-объяснение аномалий/пиков по выборке реальных жалоб (on-demand, по кнопке).

Не классифицирует корпус и не трогает горячий путь — вызывается только когда
аудитор нажал «Объяснить» на гео-аномалии или пике динамики. Возвращает
человекочитаемую прозу (без JSON-парсинга → устойчиво к провайдеру, который не
поддерживает response_format=json_object).
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from ..ai.analyst import LLM_API_KEY, LLM_BASE_URL, smart_model
from ..ai.llm_utils import _patch_client_reasoning_effort

log = logging.getLogger(__name__)

_SYSTEM = (
    "Ты — аналитик службы внутреннего аудита Сбербанка. Тебе дают выборку реальных "
    "негативных жалоб клиентов (banki.ru) по конкретному срезу (город или месяц с "
    "всплеском). Кратко и по делу объясни, ЧТО вероятно стоит за этим всплеском/"
    "аномалией и НА ЧТО обратить внимание аудитору. Только то, что подтверждается "
    "текстами — не выдумывай фактов, цифр и причин сверх жалоб. 3–5 предложений, "
    "деловой тон, без воды и без маркетинга."
)


async def explain_segment(seg: dict, *, label: str) -> str | None:
    """seg — результат reviews_dash.segment_reviews(). label — напр. «г. Якутск»."""
    texts = (seg or {}).get("texts") or []
    if not texts:
        return None
    themes = seg.get("themes") or []
    themes_str = ", ".join(f'{t["label"]} ({t["n"]})' for t in themes) or "—"
    joined = "\n\n".join(f"— {t}" for t in texts[:20])
    user = (
        f"Срез: {label}. Жалоб в выборке: {seg.get('n')}.\n"
        f"Авто-разметка тем (regex, грубая): {themes_str}.\n\n"
        f"Жалобы клиентов:\n{joined}\n\n"
        "Дай аудитору: (1) вероятную причину всплеска/аномалии; "
        "(2) 2–3 доминирующие темы своими словами; (3) что конкретно проверить. Кратко."
    )
    try:
        client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                             max_retries=2, timeout=60)
        # gemini-2.5-flash — thinking-модель: без reasoning_effort=low скрытый
        # reasoning съедает бюджет и ответ обрезается (как и в analyst.py:1040).
        client = _patch_client_reasoning_effort(client)
        resp = await client.chat.completions.create(
            model=smart_model(),
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.2, max_tokens=2048)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:  # noqa: BLE001 — деградируем мягко, объяснение не критично
        log.warning("reviews_llm.explain_segment упал: %s", e)
        return None
