"""Schema Normalizer — сводит разные названия атрибутов от разных банков
к каноническому имени.

Пример: Сбер дал "годовая_комиссия", ВТБ "плата_за_обслуживание",
Альфа "тариф_за_обслуживание_год" — все три должны стать одним полем.

Подход: 1 LLM вызов на ВЕСЬ набор attribute'ов сразу (Map-Reduce).
LLM формирует кластеры синонимичных имён → каноническое имя на каждый кластер.
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from typing import Any

from openai import AsyncOpenAI

from .triple_extractor import Triple, _parse_json_array

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — нормализатор схемы данных. Получаешь список разных
названий атрибутов из разных источников/банков. Группируешь синонимы и
даёшь канонические имена.

ПРАВИЛО:
• Каноническое имя — короткое snake_case на русском, до 30 chars.
• Канон должен быть ПОНЯТЕН аудитору ("годовая_комиссия", "ставка_от",
  "лимит_снятия_в_сутки", "первоначальный_взнос_мин")
• В один кластер — только реально синонимичные атрибуты, описывающие ОДНО
  и то же поле продукта.

ВЫХОД: JSON массив кластеров. Каждый кластер:
{
  "canonical": "годовая_комиссия",
  "variants":  ["плата_за_обслуживание", "тариф_за_обслуживание_год", ...],
  "category":  "fee|rate|limit|amount|term|requirement|feature",
  "unit":      "₽" | "%" | "руб/мес" | "" — единица измерения для группы
}

ВАЖНО:
• ВСЕ исходные атрибуты должны быть распределены — не пропускай
• Атрибут попадает РОВНО в один кластер
• Если атрибут уникален (нет синонимов) — отдельный кластер с variants=[он сам]

Возвращай ТОЛЬКО JSON массив. БЕЗ преамбулы."""


async def normalize_schema(client: AsyncOpenAI, triples: list[Triple],
                            model: str | None = None) -> dict[str, str]:
    """Возвращает mapping: исходный_attribute → canonical_attribute.

    Если ничего не удалось нормализовать (LLM упал) — возвращает identity-map
    (каждый attribute сам себе canonical).
    """
    model = model or os.getenv("LLM_MODEL_FAST") or os.getenv("LLM_MODEL_NAME",
                                                                "gpt-4o-mini")
    # Уникальные имена attribute'ов с примерами значения/unit
    seen: dict[str, tuple[str, str]] = {}
    for t in triples:
        if t.attribute not in seen:
            seen[t.attribute] = (t.value[:40], t.unit)
    if not seen:
        return {}

    attrs_block = "\n".join(
        f"  • {a}: пример «{v[0]}» {v[1]}".rstrip()
        for a, v in seen.items()
    )
    user_msg = (
        f"# Атрибуты ({len(seen)} штук, разных банков)\n{attrs_block}\n\n"
        f"Сгруппируй синонимы. Каноническое имя на каждый кластер."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=4000, temperature=0.0,
            ),
            timeout=45,
        )
    except Exception as e:
        log.warning("[schema_normalizer] LLM failed: %s", e)
        return {a: a for a in seen}

    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_json_array(raw)
    if not isinstance(data, list):
        log.warning("[schema_normalizer] no JSON array (raw 200=%r)", raw[:200])
        return {a: a for a in seen}

    mapping: dict[str, str] = {}
    for cluster in data:
        if not isinstance(cluster, dict):
            continue
        canon = (cluster.get("canonical") or "").strip().lower().replace(" ", "_")
        if not canon:
            continue
        variants = cluster.get("variants") or []
        if not isinstance(variants, list):
            continue
        for v in variants:
            vn = str(v).strip().lower()
            if vn in seen:
                mapping[vn] = canon

    # Fallback для атрибутов, которые LLM пропустил — сам себе canonical
    missed = 0
    for a in seen:
        if a not in mapping:
            mapping[a] = a
            missed += 1
    if missed:
        log.info("[schema_normalizer] %s/%s атрибутов не покрыты кластерами",
                  missed, len(seen))

    # Сжатие: сколько групп получилось
    n_canon = len(set(mapping.values()))
    log.warning("[schema_normalizer] %s исходных → %s канонических групп",
                len(seen), n_canon)
    return mapping


def apply_normalization(triples: list[Triple],
                         mapping: dict[str, str]) -> list[Triple]:
    """Применяет mapping к списку триплов — заменяет attribute на canonical."""
    out = []
    for t in triples:
        canon = mapping.get(t.attribute, t.attribute)
        if canon != t.attribute:
            # Создаём новый Triple с canonical имени (immutable copy)
            t_new = Triple(
                entity_bank_slug=t.entity_bank_slug,
                attribute=canon,
                value=t.value, unit=t.unit,
                value_numeric=t.value_numeric,
                source_idx=t.source_idx, source_url=t.source_url,
                excerpt=t.excerpt, confidence=t.confidence,
            )
            out.append(t_new)
        else:
            out.append(t)
    return out
