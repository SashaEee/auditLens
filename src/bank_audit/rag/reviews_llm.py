"""LLM-объяснение аномалий/пиков по выборке реальных жалоб (on-demand, по кнопке).

Не классифицирует корпус и не трогает горячий путь — вызывается только когда
аудитор нажал «Объяснить» на гео-аномалии или пике динамики. Возвращает
человекочитаемую прозу (без JSON-парсинга → устойчиво к провайдеру, который не
поддерживает response_format=json_object).
"""
from __future__ import annotations

import logging
import re

from openai import AsyncOpenAI

from ..ai.analyst import LLM_API_KEY, LLM_BASE_URL, fast_model, smart_model
from ..ai.llm_utils import _patch_client_reasoning_effort

log = logging.getLogger(__name__)


def _client() -> AsyncOpenAI:
    c = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, max_retries=2, timeout=60)
    return _patch_client_reasoning_effort(c)   # reasoning_effort=low — иначе thinking съедает ответ

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
        resp = await _client().chat.completions.create(
            model=smart_model(),
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.2, max_tokens=2048)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:  # noqa: BLE001 — деградируем мягко, объяснение не критично
        log.warning("reviews_llm.explain_segment упал: %s", e)
        return None


# ── LLM-классификация показанных отзывов (on-demand, по кнопке) ──────────────
_CLS_SYSTEM = (
    "Ты классифицируешь жалобы клиентов банка по фиксированной таксономии тем для "
    "внутреннего аудита. Учитывай СМЫСЛ и отрицания: «не навязывали страховку» — это "
    "НЕ тема навязывания; «спасибо, разблокировали» — не блокировка. Если ни одна тема "
    "не подходит — пиши other. Не выдумывай темы вне списка."
)


async def classify_reviews(items: list[dict]) -> list[dict | None]:
    """LLM-классификация ~20 показанных отзывов в фикс. таксономию (on-demand).
    Возвращает список по индексам: {themes:[{key,label,short,risk}]} или None
    (None → вызывающий оставит regex-темы как fallback)."""
    from .reviews_dash import THEMES, theme_obj
    texts = [(it.get("text") or "")[:500] for it in items]
    if not texts:
        return []
    enum = "\n".join(f'- {t["key"]}: {t["label"]}' for t in THEMES)
    listing = "\n".join(f'#{i+1}: {t}' for i, t in enumerate(texts))
    user = (
        f"Темы (ключи):\n{enum}\n- other: ничего из списка не подходит\n\n"
        "Для КАЖДОЙ жалобы выбери 1–3 ключа тем (через запятую). Формат СТРОГО по "
        "одной строке на жалобу, без пояснений:\n<номер>: <key>,<key>\n"
        "Пример:\n3: blocking,escalation\n\n"
        f"Жалобы:\n{listing}"
    )
    out: list[dict | None] = [None] * len(texts)
    try:
        resp = await _client().chat.completions.create(
            model=fast_model(),
            messages=[{"role": "system", "content": _CLS_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0, max_tokens=1600)
        content = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        log.warning("reviews_llm.classify_reviews упал: %s", e)
        return out
    valid = {t["key"] for t in THEMES}
    for line in content.splitlines():
        m = re.match(r"\s*#?(\d+)", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if not (0 <= idx < len(texts)):
            continue
        keys = [k for k in valid if re.search(r"\b" + re.escape(k) + r"\b", line)]
        out[idx] = {"themes": [theme_obj(k) for k in keys if theme_obj(k)]}
    return out
