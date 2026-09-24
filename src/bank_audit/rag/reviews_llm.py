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

from ..ai.analyst import (LLM_API_KEY, LLM_BASE_URL, fast_model, insight_model,
                          smart_model)
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
        f"Главные проблемы по разметке кодификатором: {themes_str}.\n\n"
        f"Жалобы клиентов:\n{joined}\n\n"
        "Дай аудитору: (1) вероятную причину всплеска/аномалии; "
        "(2) 2–3 доминирующие темы своими словами; (3) что конкретно проверить. Кратко."
    )
    try:
        resp = await _client().chat.completions.create(
            model=insight_model(),
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


def signal_lines(sig: dict) -> tuple[list[str], str]:
    """Строки сигналов с точными числами и строка общего объёма недели."""
    lines = []
    for s in (sig or {}).get("signals") or []:
        bits = []
        if s.get("new"):
            bits.append("НОВАЯ проблема (раньше почти не было)")
        elif s.get("ratio"):
            bits.append(f"×{s['ratio']} к норме ~{s['baseline_week']}/нед")
        if s.get("accel"):
            bits.append(f"ускоряется (нед: {s.get('prev_week')}→{s['week']})")
        mr = s.get("market_ratio")
        if s.get("bank_specific"):
            bits.append(f"ТОЛЬКО у банка (рынок без банка ×{mr if mr is not None else '~1'})")
        elif mr is not None and mr >= 1.4:
            bits.append(f"рынок тоже растёт ×{mr} (возможно отраслевое)")
        if s.get("geo"):
            bits.append(f"{s['geo']['share']}% из г. {s['geo']['city']}")
        lines.append(f'- {s["label"]} [{s.get("level", "medium")}]: {s["week"]} за 7 дн; '
                     + "; ".join(bits))
    ov = (sig or {}).get("overall") or {}
    ov_line = (f'Всего жалоб за неделю: {ov.get("week")} (обычно ~{ov.get("baseline_week")}/нед'
               + (f', рынок ×{ov["market_ratio"]}' if ov.get("market_ratio") is not None else "")
               + ").") if ov.get("week") is not None else ""
    return lines, ov_line


def signal_context(sig: dict, bank: str, product: str | None = None,
                   per_signal: int = 12) -> str:
    """Жалобы, из которых сложился КАЖДЫЙ сигнал, — отдельно по сигналам, с
    изложением и дословной цитатой; плюс жалобы недели вне кодификатора.

    Синхронная (ходит в базу) — звать через asyncio.to_thread. Модель видит
    только жалобы своего сигнала и не может перенести в объяснение формулировку
    из отзыва другой темы: так 22.09 в заголовок попало «навязывание платной
    карты без согласия» из чужого отзыва о событии 2025 года."""
    from . import reviews_dash as rd
    blocks = []
    for s in ((sig or {}).get("signals") or [])[:4]:
        ev = rd.signal_evidence(bank, s["key"], product=product, limit=per_signal) or []
        items = []
        for e in ev:
            q = f" — «{e['quote'][:200]}»" if e.get("quote") else ""
            items.append(f"  — {e.get('date') or ''}: {e.get('summary') or ''}{q}")
        blocks.append(f"ЖАЛОБЫ СИГНАЛА «{s['label']}» (показано {len(items)} из {s['week']}):\n"
                      + ("\n".join(items) or "  —"))
    nov = rd.novel_week(bank, product=product, limit=20) or []
    nov_lines = "\n".join(f"  — {n['new_topic']}: {n.get('summary') or ''}" for n in nov)
    return ("\n\n".join(blocks)
            + "\n\nЖАЛОБЫ НЕДЕЛИ, ДЛЯ КОТОРЫХ НЕТ ТОЧНОГО КОДА (как их назвала модель):\n"
            + (nov_lines or "  —"))


# ── Срочные аномалии за 7 дней (audit-радар, on-demand при загрузке блока) ───
_ANOM_SYSTEM = (
    "Ты — старший аналитик службы внутреннего аудита банка. НЕ пересказывай жалобы — "
    "дай АНАЛИЗ: что аномально, почему важно, куда смотреть и насколько срочно. Тебе "
    "дают точные недельные метрики (НЕ меняй числа) и свежие жалобы. Сигналы, на "
    "которые опирайся:\n"
    "• рост темы к норме (×N) и УСКОРЕНИЕ (растёт неделя-к-неделе) — проблема нарастает;\n"
    "• «только у банка» = всплеск у банка, а рынок по теме ровный → это НАША регрессия "
    "(высокий приоритет); если рынок тоже растёт — вероятно отраслевое/сезонное (ниже);\n"
    "• гео-концентрация (≥40% в одном городе) → локальный сбой (отделение/банкомат/регион);\n"
    "• жалобы без точного кода кодификатора → свежий инцидент, которого ещё нет в списке проблем.\n"
    "Не алармируй без чисел; без эмодзи."
)


async def anomaly_brief(sig: dict, context: str) -> str | None:
    """sig — reviews_dash.weekly_signals(); context — signal_context(). Возвращает
    markdown-аналитику (приоритизированную) или None — тогда фронт показывает
    сами сигналы."""
    signals = (sig or {}).get("signals") or []
    if not signals:
        return None
    lines, ov_line = signal_lines(sig)
    user = (
        "СИГНАЛЫ НЕДЕЛИ (числа точные, не меняй):\n" + "\n".join(lines) + f"\n{ov_line}\n\n"
        + context + "\n\n"
        "Выдай markdown-список (начинай каждый пункт с «- »):\n"
        "1) 2–4 пункта по приоритету. Формат: «**[ВЫСОКИЙ/СРЕДНИЙ]** **<проблема>** — "
        "что изменилось (с цифрой), пометь если *только у банка*/*локально*/*ускоряется*, "
        "вероятная причина — ТОЛЬКО из жалоб этого сигнала, что проверить аудитору».\n"
        "2) Если среди жалоб без точного кода несколько об одном и том же — добавь пункт "
        "«- **Новое:** <суть> (≈N жалоб)».\n"
        "Не переноси формулировки из жалоб одного сигнала в другой. Не выдумывай причин, "
        "которых нет в жалобах. Коротко, аналитично, без вступления и без эмодзи."
    )
    try:
        resp = await _client().chat.completions.create(
            model=insight_model(),
            messages=[{"role": "system", "content": _ANOM_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.2, max_tokens=1800)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:  # noqa: BLE001
        log.warning("reviews_llm.anomaly_brief упал: %s", e)
        return None
