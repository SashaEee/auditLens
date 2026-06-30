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
# Гибкий подход: LLM сам формулирует КОНКРЕТНУЮ тему обращения (free-form, не из
# фикс. списка) — ловит «Блокировка по 161-ФЗ», «Карта СВОи», «Навязанная страховка
# по ипотеке» и т.п., что хардкод-таксономия пропускает. risk-класс — для цвета.
_CLS_SYSTEM = (
    "Ты — аналитик внутреннего аудита банка. Для каждой жалобы клиента сформулируй "
    "КОНКРЕТНУЮ суть обращения короткой темой (2–5 слов: продукт + проблема, при "
    "наличии — закон/норматив). Учитывай смысл и отрицания: «не навязывали» — НЕ "
    "навязывание; «спасибо, разблокировали» — не блокировка. Не обобщай до «обслуживание»."
)
_RISKS = ("compliance", "conduct", "ops")


async def classify_reviews(items: list[dict]) -> list[dict | None]:
    """On-demand LLM-классификация ~20 показанных отзывов в КОНКРЕТНЫЕ темы (free-form).
    Возвращает по индексам {themes:[{short,label,risk}]} или None (None → regex-fallback)."""
    texts = [(it.get("text") or "")[:600] for it in items]
    if not texts:
        return []
    listing = "\n".join(f"#{i+1}: {t}" for i, t in enumerate(texts))
    user = (
        "Для КАЖДОЙ жалобы дай: (1) конкретную тему (2–5 слов, напр. «Блокировка по "
        "161-ФЗ», «Навязанная страховка по кредиту», «Карта СВОи: отказ», «Двойное "
        "списание по СБП», «Сбой в приложении»); (2) risk-класс: compliance "
        "(регуляторика/закон/ЦБ/суд), conduct (недобросовестные практики к клиенту), "
        "ops (операционные сбои/сервис).\n"
        "Формат СТРОГО по одной строке на жалобу, без лишнего:\n<номер> | <тема> | <risk>\n"
        "Пример:\n3 | Блокировка по 161-ФЗ | compliance\n\n"
        f"Жалобы:\n{listing}"
    )
    out: list[dict | None] = [None] * len(texts)
    try:
        resp = await _client().chat.completions.create(
            model=fast_model(),
            messages=[{"role": "system", "content": _CLS_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0, max_tokens=2000)
        content = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        log.warning("reviews_llm.classify_reviews упал: %s", e)
        return out
    for line in content.splitlines():
        m = re.match(r"\s*#?(\d+)\s*[|:.\)]\s*(.+)", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if not (0 <= idx < len(texts)):
            continue
        rest = m.group(2).strip()
        risk = next((rc for rc in _RISKS if rc in rest.lower()), "other")
        topic = rest.split("|")[0].strip()
        topic = re.sub(r"[|\-—:]+\s*(compliance|conduct|ops)\b.*$", "", topic, flags=re.I).strip(" .—-|:")
        if topic:
            out[idx] = {"themes": [{"short": topic[:46], "label": topic, "risk": risk}]}
    return out


# ── Срочные аномалии за 7 дней (audit-радар, on-demand при загрузке блока) ───
_ANOM_SYSTEM = (
    "Ты — аналитик службы внутреннего аудита банка. Тебе дают СЧИТАННЫЕ за 7 дней "
    "недельные аномалии (точные числа — НЕ меняй и не выдумывай новые) и свежие "
    "жалобы недели. Задача — за 10 секунд сказать аудитору, ГДЕ резко изменилось и "
    "какой МОДУЛЬ идти проверять: резкий рост жалоб = модуль внезапно деградировал "
    "или заброшен сопровождением. Не алармируй без чисел."
)


async def anomaly_brief(sig: dict, samples: list[dict]) -> str | None:
    """sig — результат reviews_dash.weekly_signals(). Возвращает markdown-резюме
    из 2–4 коротких срочных пунктов (или None — тогда показываем сами сигналы)."""
    signals = (sig or {}).get("signals") or []
    if not signals:
        return None
    lines = []
    for s in signals:
        tag = "НОВАЯ тема (раньше почти не было)" if s.get("new") else (
            f"×{s['ratio']} к норме" if s.get("ratio") else "рост")
        lines.append(f'- {s["label"]}: {s["week"]} жалоб за 7 дн ({tag}; обычно ~{s["baseline_week"]}/нед)')
    ov = (sig or {}).get("overall") or {}
    ov_line = (f'Всего за неделю: {ov.get("week")} (обычно ~{ov.get("baseline_week")}/нед).'
               if ov.get("week") is not None else "")
    samp = "\n".join(f'— {(r.get("text") or "")[:280]}' for r in (samples or [])[:14])
    user = (
        "Аномалии недели (числа точные):\n" + "\n".join(lines) + f"\n{ov_line}\n\n"
        f"Свежие жалобы недели:\n{samp}\n\n"
        "Дай 2–4 КОРОТКИХ срочных пункта (markdown, каждый с новой строки, начинай с «- »). "
        "В каждом: что резко изменилось (с цифрой из сигналов), вероятная причина из жалоб, "
        "и какой **модуль/тему** проверить (выдели жирным). Без вступления, без воды."
    )
    try:
        resp = await _client().chat.completions.create(
            model=smart_model(),
            messages=[{"role": "system", "content": _ANOM_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.2, max_tokens=1600)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:  # noqa: BLE001
        log.warning("reviews_llm.anomaly_brief упал: %s", e)
        return None
