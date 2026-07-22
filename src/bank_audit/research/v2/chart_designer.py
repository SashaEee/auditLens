"""Визуальный редактор deep-отчёта: LLM решает ЧТО/КАК/ГДЕ визуализировать.

Замена детерминированного extract_chart_specs (он остаётся аварийным фолбэком):
LLM получает готовый отчёт + валидированные числовые факты bundle и возвращает
2-4 спека графиков с якорями-фразами. Пайплайн вставляет маркеры [[CHART:i]]
в текст сразу после якорей — фронт рендерит графики ПО МЕСТУ смысла.

Анти-галлюцинация: каждое число каждого графика обязано присутствовать среди
чисел фактов bundle (сверка с допуском) — иначе график отбрасывается. LLM
владеет смыслом и композицией, код гарантирует правду цифр.
"""
from __future__ import annotations

import json
import logging

from ...ai.analyst import insight_model
from ...ai.llm_utils import _loose_json_loads
from .knowledge_bundle import _parse_first_number_unit

log = logging.getLogger(__name__)

_ALLOWED_TYPES = {"bar", "horizontalBar", "line", "doughnut"}
_MAX_CHARTS = 4

_SYS = (
    "Ты — визуальный редактор аудит-отчёта для аудиторов Сбербанка. Твоя задача — "
    "выбрать 2-4 МЕСТА в отчёте, где график реально помогает понять цифры, и "
    "спроектировать эти графики.\n\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Числа — ТОЛЬКО из списка ФАКТОВ ниже (каждое сверяется программно; "
    "выдуманное число = график выбрасывается). Ничего не вычисляй, не суммируй, "
    "не усредняй — бери значения как есть.\n"
    "2. anchor — ТОЧНАЯ подстрока из текста отчёта (30-120 символов, конец "
    "предложения того абзаца, к которому относится график). График вставится "
    "СРАЗУ ПОСЛЕ абзаца с этой подстрокой.\n"
    "3. Тип по смыслу: horizontalBar — сравнение банков/субъектов; bar — "
    "категории; line — динамика во времени; doughnut — доли целого (только "
    "если это реально доли).\n"
    "4. insight — одна фраза-вывод под графиком (что видно из картинки), "
    "с конкретикой.\n"
    "5. highlight — метка Сбера, если он есть среди labels (подсветим фирменным "
    "цветом).\n"
    "6. reference_line — опционально {label, value}: медиана/ключевая ставка, "
    "если такое число ЕСТЬ в фактах.\n"
    "7. НЕ дублируй один и тот же срез данных в двух графиках. Меньше, но метче.\n\n"
    "Верни СТРОГО JSON без markdown:\n"
    '{"charts":[{"anchor":"...","type":"horizontalBar","title":"...",'
    '"unit":"% годовых","insight":"...","labels":["Сбербанк","ВТБ"],'
    '"series":[{"label":"Ставка","data":[14.0,18.24]}],'
    '"highlight":"Сбербанк","reference_line":{"label":"медиана","value":12.09},'
    '"sources":[3,7]}]}'
)


def _facts_digest(bundle) -> tuple[str, set[float]]:
    """Компактный нумерованный дамп числовых фактов + множество допустимых чисел."""
    lines: list[str] = []
    nums: set[float] = set()
    for f in bundle.facts:
        num, unit = _parse_first_number_unit(f.value)
        if num is None:
            continue
        subj = bundle.subject_labels.get(bundle.canonical_subject(f.subject),
                                         f.subject)
        lines.append(f"- {subj} | {f.attribute} | {num} {unit or ''} | src={f.source_n}")
        nums.add(round(float(num), 6))
        if len(lines) >= 140:
            break
    return "\n".join(lines), nums


def _num_allowed(v, nums: set[float]) -> bool:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return False
    return any(abs(x - n) <= max(0.01, abs(n) * 0.002) for n in nums)


def _validate(ch: dict, nums: set[float]) -> dict | None:
    """Строгая проверка спека: тип, размерности, каждое число — из фактов."""
    t = str(ch.get("type") or "bar")
    if t not in _ALLOWED_TYPES:
        t = "bar"
    labels = [str(x)[:40] for x in (ch.get("labels") or []) if x is not None]
    series = ch.get("series") or []
    if not labels or not series or len(labels) > 12:
        return None
    datasets = []
    for s in series[:3]:
        data = (s or {}).get("data") or []
        if len(data) != len(labels):
            return None
        clean = []
        for v in data:
            if v is None:
                clean.append(None)
                continue
            if not _num_allowed(v, nums):
                return None                     # выдуманное число — бракуем весь график
            clean.append(float(v))
        datasets.append({"label": str((s or {}).get("label") or "")[:60],
                         "data": clean})
    if not datasets:
        return None
    rl = ch.get("reference_line") or None
    if rl and not _num_allowed(rl.get("value"), nums):
        rl = None                                # референс не из фактов — просто убираем
    return {
        "chartType": t,
        "title": str(ch.get("title") or "")[:120],
        "unit": str(ch.get("unit") or "")[:30],
        "insight": str(ch.get("insight") or "")[:220],
        "labels": labels,
        "datasets": datasets,
        "highlight": (str(ch.get("highlight"))[:40]
                      if ch.get("highlight") else None),
        "referenceLine": ({"label": str(rl.get("label") or "")[:40],
                           "value": float(rl["value"])} if rl else None),
        "sourceCitations": [int(n) for n in (ch.get("sources") or [])
                            if isinstance(n, (int, float))][:6],
        "_anchor": str(ch.get("anchor") or "")[:200],
    }


def _insert_marker(text: str, anchor: str, idx: int) -> tuple[str, bool]:
    """Вставить [[CHART:idx]] после абзаца, содержащего anchor."""
    pos = text.find(anchor) if anchor else -1
    if pos < 0 and anchor:
        pos = text.find(anchor.strip()[:60])
    if pos < 0:
        return text, False
    end = text.find("\n\n", pos)
    if end < 0:
        end = len(text)
    return text[:end] + f"\n\n[[CHART:{idx}]]" + text[end:], True


async def design_charts(client, report_md: str, bundle,
                        question: str) -> tuple[list[dict], str]:
    """→ (specs для фронта, отчёт с маркерами). Пустой список = фолбэк у вызывающего."""
    digest, nums = _facts_digest(bundle)
    if len(nums) < 3:
        return [], report_md

    user = (f"ВОПРОС ПОЛЬЗОВАТЕЛЯ: {question}\n\n"
            f"ОТЧЁТ:\n{report_md[:12000]}\n\n"
            f"ФАКТЫ (единственный источник чисел):\n{digest}")
    msgs = [{"role": "system", "content": _SYS},
            {"role": "user", "content": user}]
    raw = ""
    try:
        r = await client.chat.completions.create(
            model=insight_model(), messages=msgs,
            temperature=0.2, max_tokens=2200)
        raw = (r.choices[0].message.content or "").strip()
        parsed = _loose_json_loads(raw)
    except Exception:
        try:                                     # один дешёвый ретрай при обрыве JSON
            r = await client.chat.completions.create(
                model=insight_model(), messages=msgs,
                temperature=0.0, max_tokens=2200)
            raw = (r.choices[0].message.content or "").strip()
            parsed = _loose_json_loads(raw)
        except Exception as e:  # noqa: BLE001
            log.warning("[chart_designer] LLM/parse fail: %s; raw=%.200s", e, raw)
            return [], report_md

    specs: list[dict] = []
    for ch in (parsed.get("charts") or [])[:_MAX_CHARTS]:
        v = _validate(ch if isinstance(ch, dict) else {}, nums)
        if v is not None:
            specs.append(v)
    if not specs:
        return [], report_md

    # Маркеры: индексы фиксированы порядком specs (фронт: [[CHART:i]] → charts[i])
    anchored = 0
    for i, sp in enumerate(specs):
        report_md, ok = _insert_marker(report_md, sp.pop("_anchor", ""), i)
        if ok:
            anchored += 1
    log.info("[chart_designer] графиков=%d, по месту=%d", len(specs), anchored)
    return specs, report_md
