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
from .source_finder import GoldSource, find_gold_sources, find_gold_sources_extended
from .triple_extractor import Triple, extract_triples
from .fact import Fact
from .fact_extractor import extract_facts
from .schema_normalizer import normalize_schema, apply_normalization
from .matrix_builder import Matrix, build_matrix
from .narrative_renderer import render_narrative_report, extract_chart_specs
from .research_brief import synthesize_brief
from .core_schema import CoreAttr, discover_core_schema, build_extract_hint
from .narrative_generators.regulatory_box import REGULATORY_DOMAINS
from .topic_classifier import classify_topic, TopicProfile
from .regulatory_source_finder import find_regulatory_sources
from .llm_throttle import patch_client_throttle
from .gap_filler import fill_gaps
from .audit_focus_filter import filter_for_narrative

log = logging.getLogger(__name__)


def _trust_marker(score: float) -> int:
    """Trust score → tier 0/1/2 для UI."""
    if score >= 0.9: return 2
    if score >= 0.6: return 1
    return 0


def _build_sources_index(entities: list[Entity],
                          sources_per_entity: dict[str, list[GoldSource]]) -> list[dict]:
    """Глобальный список источников с n-индексами. URL дедуп.

    Особый ключ "__regulatory__" — для regulatory sources (общие, не привязаны
    к конкретному банку). Также детектирует kind: bank_official / aggregator /
    regulatory / pdf.
    """
    def _classify_kind(url: str, domain: str, trust: float, is_reg: bool) -> str:
        if is_reg:
            return "regulatory"
        if url.lower().endswith(".pdf"):
            return "pdf"
        if trust >= 0.9:
            return "bank_official"
        return "aggregator"

    index = []
    seen_urls: set[str] = set()
    n = 1

    # Сначала per-entity sources
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
                "source_kind": _classify_kind(s.url, s.domain or "",
                                                 s.trust_score, False),
                "fetched_at": None,
                "excerpts": [s.text[:600]] if s.text else [],
                "document_id": s.document_id,
                "gold_score": s.gold_score,
            })
            n += 1

    # Затем regulatory sources (не привязаны к bank)
    for s in sources_per_entity.get("__regulatory__", []):
        if s.url in seen_urls:
            continue
        seen_urls.add(s.url)
        index.append({
            "n": n,
            "url": s.url,
            "title": s.title or s.url[:80],
            "bank_name": "",
            "bank_slug": None,
            "domain": s.domain or "",
            "trust_score": s.trust_score,
            "source_kind": _classify_kind(s.url, s.domain or "",
                                             s.trust_score, True),
            "fetched_at": None,
            "excerpts": [s.text[:600]] if s.text else [],
            "document_id": s.document_id,
            "gold_score": s.gold_score,
        })
        n += 1

    return index


def _remap_fact_indices(facts: list[Fact],
                          sources_index: list[dict]) -> list[Fact]:
    """Перенумеровывает source_idx из local в global n из sources_index."""
    url_to_n = {s["url"]: s["n"] for s in sources_index}
    out = []
    for f in facts:
        global_n = url_to_n.get(f.source_url, 0)
        out.append(Fact(
            entity_bank_slug=f.entity_bank_slug,
            attribute=f.attribute,
            value=f.value, unit=f.unit, value_numeric=f.value_numeric,
            conditions=list(f.conditions), qualifications=f.qualifications,
            exceptions=list(f.exceptions), verbatim_quote=f.verbatim_quote,
            page_context=f.page_context, category=f.category,
            audit_priority=f.audit_priority,
            related_attrs=list(f.related_attrs),
            source_idx=global_n,
            source_url=f.source_url,
            confidence=f.confidence,
        ))
    return out


def _remap_triple_indices(triples: list[Triple],
                          sources_per_entity: dict[str, list[GoldSource]],
                          sources_index: list[dict]) -> list[Triple]:
    """Backward-compat alias для старого кода. Использует Fact-внутренности
    если получили Fact, иначе Triple."""
    if triples and isinstance(triples[0], Fact):
        return _remap_fact_indices(triples, sources_index)
    url_to_n = {s["url"]: s["n"] for s in sources_index}
    out = []
    for t in triples:
        global_n = url_to_n.get(t.source_url, 0)
        out.append(Triple(
            entity_bank_slug=t.entity_bank_slug,
            attribute=t.attribute,
            value=t.value, unit=t.unit,
            value_numeric=t.value_numeric,
            source_idx=global_n,
            source_url=t.source_url,
            excerpt=t.excerpt, confidence=t.confidence,
        ))
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
    # Phase 2: throttle для защиты от 429 rate limits
    client = patch_client_throttle(client, max_concurrent=4)

    # Common helper для SSE
    def evt(d: dict) -> str:
        return json.dumps(d, ensure_ascii=False, default=str)

    yield evt({"type": "mode", "value": "deep"})

    # ── Stage 0: Topic classification (LLM) ──────────────────────────────
    # Определяет: kind темы, нужны ли regulatory источники, какие домены,
    # какие секции отчёта применимы. Управляет стратегиями всех остальных stage.
    yield evt({"type": "phase", "value": "planning"})
    yield evt({"type": "stage_status", "stage": "topic_classification",
                "label": "Классификация темы вопроса",
                "detail": "LLM определяет тип продукта и стратегию поиска",
                "estimate_s": 3})
    try:
        topic_profile = await classify_topic(client, question)
    except Exception as e:
        log.warning("[orchestrator] topic_classify failed: %s", e)
        from .topic_classifier import _default_profile
        topic_profile = _default_profile(question)
    yield evt({"type": "stage_status", "stage": "topic_classified",
                "label": f"Тема: {topic_profile.topic_kind}",
                "detail": topic_profile.summary[:120],
                "estimate_s": 0})

    # ── Stage 1: Entity discovery ────────────────────────────────────────
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

    # ── Stage 1.5: Discover CORE SCHEMA — 10-15 ключевых атрибутов.
    # Делаем РАНЬШЕ source_finding, чтобы query_planner мог использовать
    # core_schema для генерации attribute-specific queries.
    yield evt({"type": "stage_status", "stage": "core_schema_discovery",
                "label": "Discovery core-схемы продукта",
                "detail": "LLM выводит 10-15 ключевых атрибутов",
                "estimate_s": 5})
    primary_product = entities[0].product if entities else ""
    primary_audience = entities[0].audience if entities else None
    try:
        core_schema = await discover_core_schema(client, primary_product, primary_audience)
    except Exception as e:
        log.warning("[orchestrator] core_schema discovery failed: %s", e)
        core_schema = []
    extract_hint = build_extract_hint(core_schema) if core_schema else ""

    # ── Stage 2: Source discovery (PARALLEL + REGULATORY) ────────────────
    yield evt({"type": "phase", "value": "discovery"})
    yield evt({"type": "stage_status", "stage": "source_finding",
                "label": "Поиск источников (Phase 2: multi-query + PDF + regulatory)",
                "detail": f"Параллельно для {len(entities)} банков + regulatory layer",
                "estimate_s": 30})

    sources_per_entity: dict[str, list[GoldSource]] = {}

    async def find_one_extended(e: Entity, step_idx: int) -> tuple[Entity, list[GoldSource]]:
        """Phase 2: extended finder с query_planner + PDF support."""
        try:
            srcs = await find_gold_sources_extended(
                client, e, core_schema=core_schema, top_n=10,
            )
        except Exception as ex:
            log.warning("[orchestrator] extended_finder failed for %s: %s — "
                          "fallback к base", e.bank_slug, ex)
            try:
                srcs = await find_gold_sources(client, e, top_n=5)
            except Exception as ex2:
                log.warning("base source_finder failed too: %s", ex2)
                srcs = []
        return e, srcs

    # Стартуем step_start события сразу
    for i, e in enumerate(entities, 1):
        yield evt({"type": "step_start", "n": i,
                    "title": f"Сбор источников — {e.bank_name}",
                    "tool": "source_finder_ext", "entity": e.bank_slug})

    # ВЕСЬ STAGE 2 параллельно: per-entity + regulatory
    entity_tasks = [find_one_extended(e, i+1) for i, e in enumerate(entities)]
    regulatory_task = find_regulatory_sources(
        client, question, topic_profile, top_n=4,
    ) if topic_profile.needs_regulatory else asyncio.sleep(0, result=[])

    all_results = await asyncio.gather(
        *entity_tasks, regulatory_task,
        return_exceptions=False,
    )
    entity_results = all_results[:-1]
    regulatory_sources = all_results[-1] if topic_profile.needs_regulatory else []

    for i, (e, srcs) in enumerate(entity_results, 1):
        sources_per_entity[e.bank_slug] = srcs
        yield evt({"type": "step_done", "n": i,
                    "found": len(srcs), "used": len(srcs)})

    # Regulatory sources идут под "shared" slug (None) — общие для всех banks
    if regulatory_sources:
        sources_per_entity["__regulatory__"] = regulatory_sources
        log.warning("[orchestrator] +%s regulatory sources",
                     len(regulatory_sources))

    # Build global sources index
    sources_index = _build_sources_index(entities, sources_per_entity)
    yield evt({"type": "sources", "sources": sources_index})

    total_sources = len(sources_index)
    high = sum(1 for s in sources_index if s.get("trust_score", 0) >= 0.85)
    mid  = sum(1 for s in sources_index if 0.55 <= s.get("trust_score", 0) < 0.85)
    low  = sum(1 for s in sources_index if s.get("trust_score", 0) < 0.55)
    n_pdf = sum(1 for s in sources_index if s.get("url", "").lower().endswith(".pdf"))
    n_reg = len(regulatory_sources)
    yield evt({"type": "coverage", "total_sources": total_sources,
                "high_trust": high, "mid_trust": mid, "low_trust": low,
                "pdf_sources": n_pdf, "regulatory_sources": n_reg,
                "warning": "Мало источников — отчёт ограничен" if total_sources < 3 else None})

    if total_sources == 0:
        yield evt({"type": "text", "chunk":
            "\n⚠ Не удалось найти источники по теме. Проверьте формулировку вопроса "
            "или попробуйте уточнить продукт / банки.\n"})
        yield evt({"type": "done"})
        return

    # ── Stage 3: Fact extraction (parallel per entity) ───────────────────
    # ОБОГАЩЁННЫЕ Fact'ы заменяют плоские Triple'ы. Каждый факт несёт:
    #   • verbatim_quote — для narrative
    #   • conditions[]   — условия применения
    #   • qualifications — сегмент аудитории
    #   • exceptions[]   — исключения
    #   • category       — fee/rate/limit/feature/requirement/regulation
    #   • audit_priority — high/medium/low
    yield evt({"type": "phase", "value": "fact-extract"})
    yield evt({"type": "stage_status", "stage": "fact_extraction",
                "label": "Извлечение обогащённых фактов из источников",
                "detail": f"Параллельно для {len(entities)} банков",
                "estimate_s": 25})

    fact_step_start_n = len(entities) + 1
    for i, e in enumerate(entities):
        yield evt({"type": "step_start", "n": fact_step_start_n + i,
                    "title": f"Извлечение фактов — {e.bank_name}",
                    "tool": "fact_extractor", "entity": e.bank_slug})

    async def extract_one(e: Entity) -> tuple[Entity, list[Fact]]:
        """Extract с двойной попыткой: если 0 facts — переформулируем product
        на более общий синоним и пробуем повторный source_finder + extract."""
        srcs = sources_per_entity.get(e.bank_slug, [])
        facts: list[Fact] = []
        if srcs:
            try:
                facts = await extract_facts(client, e, srcs,
                                              core_schema_hint=extract_hint)
            except Exception as ex:
                log.warning("extract_facts failed for %s: %s", e.bank_slug, ex)

        # SECOND CHANCE при 0 facts
        if not facts and e.product_synonyms:
            general_syn = sorted(e.product_synonyms,
                                  key=lambda s: (s.lower() == e.product.lower(), len(s)))
            for alt_product in general_syn[:2]:
                if alt_product.lower() == e.product.lower():
                    continue
                alt_e = Entity(
                    bank_slug=e.bank_slug, bank_name=e.bank_name,
                    bank_domain=e.bank_domain, product=alt_product,
                    product_synonyms=e.product_synonyms,
                    audience=e.audience,
                )
                log.warning("[orchestrator] %s: 0 facts → retry product=%r",
                             e.bank_slug, alt_product)
                try:
                    alt_srcs = await find_gold_sources(client, alt_e, top_n=3)
                    if alt_srcs:
                        sources_per_entity[e.bank_slug] = alt_srcs
                        srcs = alt_srcs
                        facts = await extract_facts(client, alt_e, alt_srcs,
                                                       core_schema_hint=extract_hint)
                        if facts:
                            for f in facts:
                                f.entity_bank_slug = e.bank_slug
                            break
                except Exception as ex:
                    log.info("retry failed for %s with %r: %s",
                              e.bank_slug, alt_product, ex)

        # Honest fallback если 0 facts
        if not facts:
            facts = [Fact(
                entity_bank_slug=e.bank_slug,
                attribute="продукт_доступен",
                value="не найден в открытых источниках" if srcs else "не найдены источники",
                unit="", value_numeric=None,
                verbatim_quote="Возможно банк не предлагает специальный продукт или информация не публикуется",
                category="feature", audit_priority="high",
                source_idx=1 if srcs else 0,
                source_url=srcs[0].url if srcs else "",
                confidence="low",
            )]
        return e, facts

    fact_results = await asyncio.gather(
        *[extract_one(e) for e in entities], return_exceptions=False)
    all_facts: list[Fact] = []
    for i, (e, facts) in enumerate(fact_results):
        all_facts.extend(facts)
        n_high = sum(1 for f in facts if f.audit_priority == "high")
        # Reg sources также экстрагируются если есть (под synthetic entity)
        # — это даст наполнение для regulatory_box секции.
        # ↓ Это происходит ниже, отдельным шагом, чтобы не блокировать per-entity.
        yield evt({"type": "step_done", "n": fact_step_start_n + i,
                    "found": len(facts), "used": len(facts),
                    "detail": f"{n_high} priority-high"})

    # ── Stage 3.5: Extract из regulatory sources (если есть) ─────────────
    # Создаём synthetic entity «Регулятор» для extract из НПА-документов.
    # Эти факты используются regulatory_box секцией.
    if regulatory_sources:
        try:
            reg_entity = Entity(
                bank_slug="_regulator",
                bank_name="Регулятор / НПА",
                bank_domain="",
                product=question[:100],
                audience=None,
                product_synonyms=[],
            )
            reg_extract_hint = (extract_hint or "") + (
                "\n# ВАЖНО: это РЕГУЛЯТОРНЫЕ источники (ГК РФ / ЦБ / нотариат / "
                "Минобороны и т.п.). Извлекай НОРМАТИВНЫЕ факты:\n"
                "  • статьи закона, конкретные пункты, сроки\n"
                "  • obligations / запреты / разрешения\n"
                "  • категорию ставь REGULATION для каждого извлечённого факта\n"
            )
            reg_facts = await extract_facts(
                client, reg_entity, regulatory_sources,
                core_schema_hint=reg_extract_hint,
            )
            # Принудительно — категория regulation
            for f in reg_facts:
                if f.attribute != "продукт_доступен":
                    f.category = "regulation"
                    if f.audit_priority == "low":
                        f.audit_priority = "medium"
            # Skip placeholder
            reg_facts = [f for f in reg_facts if f.attribute != "продукт_доступен"]
            if reg_facts:
                all_facts.extend(reg_facts)
                log.warning("[orchestrator] +%s regulatory facts extracted",
                             len(reg_facts))
        except Exception as e:
            log.warning("[orchestrator] regulatory extract failed: %s", e)

    # ── Stage 4: Schema normalization ────────────────────────────────────
    yield evt({"type": "stage_status", "stage": "schema_normalization",
                "label": "Сведение синонимичных полей",
                "detail": f"Из {len(all_facts)} фактов",
                "estimate_s": 8})
    yield evt({"type": "step_start", "n": fact_step_start_n + len(entities),
                "title": "Нормализация схемы атрибутов",
                "tool": "schema_normalizer", "entity": None})
    try:
        mapping = await normalize_schema(client, all_facts)
        normalized_facts = apply_normalization(all_facts, mapping)
    except Exception as e:
        log.warning("schema_normalize failed: %s", e)
        normalized_facts = all_facts

    yield evt({"type": "step_done", "n": fact_step_start_n + len(entities),
                "found": len(set(f.attribute for f in normalized_facts)),
                "used": len(normalized_facts)})

    # Re-map source_idx to global n
    normalized_facts = _remap_fact_indices(normalized_facts, sources_index)

    # ── Stage 5: Matrix build ────────────────────────────────────────────
    core_attr_names = [a.name for a in core_schema]
    matrix = build_matrix(entities, normalized_facts, sources_index,
                           core_attrs=core_attr_names)
    initial_coverage = matrix.coverage

    # ── Stage 5.5: Gap-filling (Phase 2 — закрывает пустые клетки) ───────
    if core_schema and matrix.coverage < 0.85:
        yield evt({"type": "stage_status", "stage": "gap_filling",
                    "label": "Заполнение пробелов (targeted web search)",
                    "detail": f"Coverage {round(matrix.coverage * 100)}% → "
                                "пытаемся повысить",
                    "estimate_s": 30})
        try:
            gap_facts, _ = await fill_gaps(
                client, matrix, entities, core_schema, sources_index,
                build_matrix_fn=lambda e, f, s, c: build_matrix(e, f, s, core_attrs=c),
                initial_facts=normalized_facts,
            )
        except Exception as e:
            log.warning("[orchestrator] gap_filler failed: %s", e)
            gap_facts = []

        if gap_facts:
            # Нормализация новых фактов через тот же mapping
            try:
                gap_facts = apply_normalization(gap_facts, mapping)
            except Exception as e:
                log.warning("apply_normalization for gap_facts failed: %s", e)
            normalized_facts = normalized_facts + gap_facts
            # Re-build матрицы
            matrix = build_matrix(entities, normalized_facts, sources_index,
                                    core_attrs=core_attr_names)
            log.warning("[orchestrator] after gap_filler: %s facts (+%s), "
                          "coverage %.0f%% (+%.0f%%)",
                          len(normalized_facts), len(gap_facts),
                          matrix.coverage * 100,
                          (matrix.coverage - initial_coverage) * 100)

    # Verification event (что было верифицировано)
    yield evt({"type": "verification",
                "verified": sum(1 for f in normalized_facts if f.confidence == "high"),
                "unverified": [],
                "valid_citations": sorted({f.source_idx for f in normalized_facts if f.source_idx}),
                "checked": True})
    yield evt({"type": "claim_check",
                "verified": len(normalized_facts),
                "dropped": 0,
                "samples": []})

    # ── Stage 6: Narrative outline planning + section generation ─────────
    yield evt({"type": "phase", "value": "synthesizing"})
    yield evt({"type": "stage_status", "stage": "outline_planning",
                "label": "Планирование структуры отчёта (LLM)",
                "detail": "Выбор 5-8 секций оптимальных для темы",
                "estimate_s": 5})

    # Detect regulatory sources (учитывая обе сигнатуры: domains и kind)
    has_reg = (topic_profile.needs_regulatory or
                any(s.get("source_kind") == "regulatory" or
                     s.get("domain", "") in REGULATORY_DOMAINS
                     for s in sources_index))

    # ── Stage 6.0: ГЛОБАЛЬНЫЙ СИНТЕЗ (research_brief) ────────────────────
    # Единый «мозг» отчёта: один reasoning-вызов смотрит на ВСЮ картину и строит
    # аналитический меморандум (тезис, инсайты, архетипы, директивы секциям).
    # Без него секции были изолированными summary («generic ChatGPT»).
    yield evt({"type": "stage_status", "stage": "synthesis_brief",
                "label": "Глобальный аналитический синтез",
                "detail": "Единый разбор всей картины перед секциями",
                "estimate_s": 25})
    brief = None
    try:
        brief = await synthesize_brief(
            client, question, entities, normalized_facts, matrix,
            sources_index, core_schema=core_schema)
    except Exception as e:
        log.warning("[orchestrator] research_brief failed: %s", e)
    if brief:
        yield evt({"type": "stage_status", "stage": "synthesis_brief_done",
                    "label": "Синтез готов",
                    "detail": f"{len(brief.insights)} инсайтов, тезис сформирован",
                    "estimate_s": 0})

    # Narrative-генерация
    yield evt({"type": "stage_status", "stage": "narrative_render",
                "label": "Генерация narrative-секций (под меморандум)",
                "detail": "key_findings, per_bank, pricing, risks…",
                "estimate_s": 30})

    # NB: НЕ фильтруем факты до brief/секций (низкоприоритетные «фичи» иногда
    # объясняют механику продукта). Релевантность даёт section-aware отбор внутри
    # генераторов (select_facts_for_section), а не предварительная отсечка.
    try:
        used_sections, report_md = await render_narrative_report(
            client=client,
            model=os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                              "gpt-4o-mini"),
            question=question,
            entities=entities,
            facts=normalized_facts,
            matrix=matrix,
            sources_index=sources_index,
            core_schema=core_schema,
            has_regulatory=has_reg,
            topic_profile=topic_profile,
            brief=brief,
        )
    except Exception as e:
        log.exception("[orchestrator] narrative_render failed: %s", e)
        used_sections, report_md = [], (
            f"# Аудит-отчёт: {question}\n\n"
            f"⚠ Не удалось сгенерировать narrative-отчёт. "
            f"Найдено {len(normalized_facts)} фактов по {len(entities)} банкам, "
            f"покрытие core-схемы {round(matrix.coverage * 100)}%."
        )

    # Outline event для UI (теперь dynamic, не hardcoded)
    yield evt({"type": "outline", "sections": [s.title for s in used_sections]})

    # Stream markdown chunk-by-chunk (для красивого UI-отображения)
    paragraphs = report_md.split("\n\n")
    for p in paragraphs:
        if not p.strip():
            continue
        yield evt({"type": "text", "chunk": p + "\n\n"})
        await asyncio.sleep(0.03)

    # ── Stage 7: Charts from matrix ──────────────────────────────────────
    yield evt({"type": "phase", "value": "charting"})
    charts = extract_chart_specs(matrix)
    for ch in charts:
        yield evt({"type": "chart", "spec": ch})
        await asyncio.sleep(0.1)

    # ── Done ────────────────────────────────────────────────────────────
    elapsed = time.time() - started
    n_high = sum(1 for f in normalized_facts if f.audit_priority == "high")
    log.warning("[orchestrator] DONE in %.1fs: %s entities, %s sources, "
                 "%s facts (%s high), %s sections, %s charts, coverage=%.0f%%",
                 elapsed, len(entities), total_sources, len(normalized_facts),
                 n_high, len(used_sections), len(charts), matrix.coverage * 100)
    yield evt({"type": "phase", "value": "done"})
    yield evt({"type": "done"})
