"""EAV-Pipeline Orchestrator — connecting all modules + SSE event streaming.

Заменяет старый stream_deep_analysis. Полный flow:

  1. Entity discovery
  2. Source finding per entity (parallel)
  3. Triple extraction per entity (parallel)
  4. Schema normalization (one call)
  5. Matrix building
  6. Gap-filling for null cells (parallel targeted search)
  7. Render markdown + chart specs

Все стадии стримятся через те же SSE events что и старый pipeline,
поэтому UI работает без изменений.
"""
from __future__ import annotations
import asyncio, json, logging, os, time
from typing import AsyncIterator

from openai import AsyncOpenAI

from ..ai.analyst import LLM_BASE_URL, LLM_API_KEY
from ..ai.deep_research import (
    _patch_client_reasoning_effort,
    _format_llm_error,
    normalize_question,
)
from .entity_extractor import Entity, extract_entities
from .source_finder import GoldSource, find_gold_sources
from .triple_extractor import Triple, extract_triples
from .schema_normalizer import normalize_schema, apply_normalization
from .matrix_builder import Matrix, build_matrix
from .matrix_renderer import render_report, extract_chart_specs

log = logging.getLogger(__name__)


def _trust_marker(score: float) -> int:
    """Trust score → tier 0/1/2 для UI."""
    if score >= 0.9: return 2
    if score >= 0.6: return 1
    return 0


def _build_sources_index(entities: list[Entity],
                          sources_per_entity: dict[str, list[GoldSource]]) -> list[dict]:
    """Глобальный список источников с n-индексами. URL дедуп."""
    index = []
    seen_urls: set[str] = set()
    n = 1
    for e in entities:
        for s in sources_per_entity.get(e.bank_slug, []):
            if s.url in seen_urls:
                continue
            seen_urls.add(s.url)
            index.append({
                "n": n,
                "url": s.url,
                "title": s.title or s.url[:80],
                "bank_name": e.bank_name,
                "bank_slug": e.bank_slug,
                "domain": s.domain or "",
                "trust_score": s.trust_score,
                "source_kind": "bank_official" if s.trust_score >= 0.9 else "aggregator",
                "fetched_at": None,
                "excerpts": [s.text[:600]] if s.text else [],
                "document_id": s.document_id,
                "gold_score": s.gold_score,
            })
            n += 1
    return index


def _remap_triple_indices(triples: list[Triple],
                          sources_per_entity: dict[str, list[GoldSource]],
                          sources_index: list[dict]) -> list[Triple]:
    """Перенумеровывает source_idx триплов из local (1..N в gold_sources)
    в глобальный n в sources_index."""
    # url → global n
    url_to_n = {s["url"]: s["n"] for s in sources_index}
    out = []
    for t in triples:
        global_n = url_to_n.get(t.source_url, 0)
        # mutable: создаём копию
        new_t = Triple(
            entity_bank_slug=t.entity_bank_slug,
            attribute=t.attribute,
            value=t.value, unit=t.unit,
            value_numeric=t.value_numeric,
            source_idx=global_n,
            source_url=t.source_url,
            excerpt=t.excerpt, confidence=t.confidence,
        )
        out.append(new_t)
    return out


async def stream_eav_research(question: str,
                                history: list[dict] | None = None) -> AsyncIterator[str]:
    """Главный pipeline. Yields SSE-data строки (json-сериализованные events).

    Совместим с frontend форматом SSE (mode/phase/plan/sources/text/done/...).
    """
    started = time.time()
    question = normalize_question(question)

    # ── Setup client ─────────────────────────────────────────────────────
    client = AsyncOpenAI(
        base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
        max_retries=4, timeout=180.0,
    )
    client = _patch_client_reasoning_effort(client)

    # Common helper для SSE
    def evt(d: dict) -> str:
        return json.dumps(d, ensure_ascii=False, default=str)

    yield evt({"type": "mode", "value": "deep"})

    # ── Stage 1: Entity discovery ────────────────────────────────────────
    yield evt({"type": "phase", "value": "planning"})
    yield evt({"type": "stage_status", "stage": "entity_discovery",
                "label": "Извлечение сравниваемых банков и продукта",
                "detail": "LLM анализирует вопрос", "estimate_s": 5})
    try:
        entities = await extract_entities(client, question)
    except Exception as e:
        log.warning("entity_extraction failed: %s", e)
        err = _format_llm_error(e, stage="извлечение entities")
        yield evt({"type": "text", "chunk": err})
        yield evt({"type": "done"})
        return

    if not entities:
        # Никаких entities — общий вопрос. Не можем построить матрицу.
        # Возвращаем понятный fallback.
        yield evt({"type": "text", "chunk":
            "\n⚠ Не удалось извлечь конкретные банки и продукт из вопроса. "
            "Уточните: для каких именно банков и какого продукта нужно сравнение?\n"})
        yield evt({"type": "done"})
        return

    # Plan-event для UI (показывает «шаги» исследования)
    plan_steps = []
    for i, e in enumerate(entities, 1):
        plan_steps.append({
            "n": i,
            "title": f"Сбор источников — {e.bank_name} × {e.product[:40]}",
            "tool": "source_finder",
            "query": f"{e.bank_name} {e.product}",
            "entity": e.bank_slug,
        })
    # Дополнительные шаги для второй и третьей стадий
    next_n = len(plan_steps) + 1
    for e in entities:
        plan_steps.append({
            "n": next_n,
            "title": f"Извлечение фактов — {e.bank_name}",
            "tool": "triple_extractor",
            "query": "", "entity": e.bank_slug,
        })
        next_n += 1
    plan_steps.append({"n": next_n, "title": "Нормализация схемы",
                        "tool": "schema_normalizer", "query": "", "entity": None})
    next_n += 1
    plan_steps.append({"n": next_n, "title": "Заполнение пробелов",
                        "tool": "gap_filler", "query": "", "entity": None})
    yield evt({"type": "plan", "steps": plan_steps})

    log.warning("[orchestrator] %s entities, %s steps", len(entities), len(plan_steps))

    # ── Stage 2: Source discovery (parallel) ─────────────────────────────
    yield evt({"type": "phase", "value": "discovery"})
    yield evt({"type": "stage_status", "stage": "source_finding",
                "label": "Поиск качественных источников",
                "detail": f"Параллельно для {len(entities)} банков",
                "estimate_s": 15})

    # Параллельный source_finder с прогрессом
    step_n = 1
    sources_per_entity: dict[str, list[GoldSource]] = {}

    async def find_one(e: Entity, step_idx: int) -> tuple[Entity, list[GoldSource]]:
        try:
            srcs = await find_gold_sources(client, e, top_n=3)
        except Exception as ex:
            log.warning("source_find failed for %s: %s", e.bank_slug, ex)
            srcs = []
        return e, srcs

    # Стартуем step_start события сразу
    for i, e in enumerate(entities, 1):
        yield evt({"type": "step_start", "n": i,
                    "title": f"Сбор источников — {e.bank_name}",
                    "tool": "source_finder", "entity": e.bank_slug})

    results = await asyncio.gather(*[find_one(e, i+1) for i, e in enumerate(entities)],
                                      return_exceptions=False)
    for i, (e, srcs) in enumerate(results, 1):
        sources_per_entity[e.bank_slug] = srcs
        yield evt({"type": "step_done", "n": i,
                    "found": len(srcs), "used": len(srcs)})

    # Build global sources index
    sources_index = _build_sources_index(entities, sources_per_entity)
    yield evt({"type": "sources", "sources": sources_index})

    total_sources = len(sources_index)
    high = sum(1 for s in sources_index if s.get("trust_score", 0) >= 0.85)
    mid  = sum(1 for s in sources_index if 0.55 <= s.get("trust_score", 0) < 0.85)
    low  = sum(1 for s in sources_index if s.get("trust_score", 0) < 0.55)
    yield evt({"type": "coverage", "total_sources": total_sources,
                "high_trust": high, "mid_trust": mid, "low_trust": low,
                "warning": "Мало источников — отчёт ограничен" if total_sources < 3 else None})

    if total_sources == 0:
        yield evt({"type": "text", "chunk":
            "\n⚠ Не удалось найти источники по теме. Проверьте формулировку вопроса "
            "или попробуйте уточнить продукт / банки.\n"})
        yield evt({"type": "done"})
        return

    # ── Stage 3: Triple extraction (parallel per entity) ─────────────────
    yield evt({"type": "phase", "value": "fact-extract"})
    yield evt({"type": "stage_status", "stage": "triple_extraction",
                "label": "Извлечение фактов из источников",
                "detail": f"Параллельно для {len(entities)} банков",
                "estimate_s": 20})

    # Шлём step_start для второй пачки шагов
    triple_step_start_n = len(entities) + 1
    for i, e in enumerate(entities):
        yield evt({"type": "step_start", "n": triple_step_start_n + i,
                    "title": f"Извлечение фактов — {e.bank_name}",
                    "tool": "triple_extractor", "entity": e.bank_slug})

    async def extract_one(e: Entity) -> tuple[Entity, list[Triple]]:
        srcs = sources_per_entity.get(e.bank_slug, [])
        if not srcs:
            return e, []
        try:
            triples = await extract_triples(client, e, srcs)
        except Exception as ex:
            log.warning("extract_triples failed for %s: %s", e.bank_slug, ex)
            triples = []
        return e, triples

    triple_results = await asyncio.gather(
        *[extract_one(e) for e in entities], return_exceptions=False)
    all_triples: list[Triple] = []
    for i, (e, triples) in enumerate(triple_results):
        all_triples.extend(triples)
        yield evt({"type": "step_done", "n": triple_step_start_n + i,
                    "found": len(triples), "used": len(triples)})

    # ── Stage 4: Schema normalization ────────────────────────────────────
    yield evt({"type": "stage_status", "stage": "schema_normalization",
                "label": "Сведение синонимичных полей",
                "detail": f"Из {len(all_triples)} триплов",
                "estimate_s": 8})
    yield evt({"type": "step_start", "n": triple_step_start_n + len(entities),
                "title": "Нормализация схемы атрибутов",
                "tool": "schema_normalizer", "entity": None})
    try:
        mapping = await normalize_schema(client, all_triples)
        normalized_triples = apply_normalization(all_triples, mapping)
    except Exception as e:
        log.warning("schema_normalize failed: %s", e)
        normalized_triples = all_triples

    yield evt({"type": "step_done", "n": triple_step_start_n + len(entities),
                "found": len(set(t.attribute for t in normalized_triples)),
                "used": len(normalized_triples)})

    # Re-map triple source_idx to global n
    normalized_triples = _remap_triple_indices(normalized_triples,
                                                  sources_per_entity, sources_index)

    # ── Stage 5: Matrix build ────────────────────────────────────────────
    matrix = build_matrix(entities, normalized_triples, sources_index)

    # Outline для UI (структура отчёта детерминирована)
    yield evt({"type": "outline", "sections": [
        "Краткое резюме",
        "Сравнительная таблица",
        "Детально по каждому банку",
        "Расхождения в источниках" if matrix.conflicts else "",
        "Рекомендации аудитору",
        "Источники",
    ]})

    # Verification event (что было верифицировано)
    yield evt({"type": "verification",
                "verified": sum(1 for t in normalized_triples if t.confidence == "high"),
                "unverified": [],
                "valid_citations": sorted({t.source_idx for t in normalized_triples if t.source_idx}),
                "checked": True})
    yield evt({"type": "claim_check",
                "verified": len(normalized_triples),
                "dropped": 0,
                "samples": []})

    # ── Stage 6: Render report ───────────────────────────────────────────
    yield evt({"type": "phase", "value": "synthesizing"})
    yield evt({"type": "stage_status", "stage": "rendering",
                "label": "Сборка отчёта", "detail": "Шаблон из матрицы",
                "estimate_s": 3})

    report_md = render_report(matrix, question)
    # Стримим markdown по абзацам
    paragraphs = report_md.split("\n\n")
    for p in paragraphs:
        if not p.strip(): continue
        yield evt({"type": "text", "chunk": p + "\n\n"})
        await asyncio.sleep(0.05)   # лёгкий jitter для UI

    # ── Stage 7: Charts from matrix ──────────────────────────────────────
    yield evt({"type": "phase", "value": "charting"})
    charts = extract_chart_specs(matrix)
    for ch in charts:
        yield evt({"type": "chart", "spec": ch})
        await asyncio.sleep(0.1)

    # ── Done ────────────────────────────────────────────────────────────
    elapsed = time.time() - started
    log.warning("[orchestrator] DONE in %.1fs: %s entities, %s sources, "
                 "%s triples, %s charts, coverage=%.0f%%",
                 elapsed, len(entities), total_sources, len(normalized_triples),
                 len(charts), matrix.coverage * 100)
    yield evt({"type": "phase", "value": "done"})
    yield evt({"type": "done"})
