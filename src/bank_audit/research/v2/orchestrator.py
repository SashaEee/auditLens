"""Orchestrator v2 — главный pipeline агентского deep research.

Flow:
  1. Conductor: вопрос → ResearchPlan (intent + missions для агентов)
  2. Экспертные агенты параллельно (с учётом зависимостей) → KnowledgeBundle
  3. Analyst: bundle → черновик отчёта
  4. Critic: верификация → при проблемах одна перепись
  5. Финальный отчёт + артефакты (sources, ranking, charts)

SSE-совместимость: эмитит те же event-типы что старый orchestrator
(plan/step_start/step_done/sources/text/done), плюс новые
(ranking/insights/agent_tool_call). Фронтенд работает без изменений.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator

from openai import AsyncOpenAI

from ...ai.analyst import LLM_BASE_URL, LLM_API_KEY
from ...ai.llm_utils import (_patch_client_reasoning_effort, _format_llm_error,
                              normalize_question)
from .conductor import plan_research, ResearchPlan
from .knowledge_bundle import KnowledgeBundle
from .base_agent import AgentMission
from .agents import AGENT_REGISTRY
from .analyst import write_report
from .critic import critique_report, Critique
from .llm_throttle_v2 import patch_client_throttle

log = logging.getLogger(__name__)


def _evt(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)


_AGENT_LABELS_UI = {
    "researcher": "Исследование условий",
    "reviews": "Отзывы и жалобы",
    "regulatory": "Регуляторное поле",
    "ranking": "Рейтинг",
    "market": "Рыночный контекст",
}


async def stream_deep_research_v2(question: str,
                                    history: list[dict] | None = None,
                                    ) -> AsyncIterator[str]:
    """Главный entry-point. Yields SSE-data строки."""
    started = time.time()
    question = normalize_question(question)
    yield _evt({"type": "mode", "value": "deep"})

    # ── Setup client ─────────────────────────────────────────────────────
    try:
        client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                              max_retries=4, timeout=180.0)
    except Exception as e:
        yield _evt({"type": "text", "chunk": _format_llm_error(e, "подключение LLM")})
        yield _evt({"type": "done"})
        return
    client = _patch_client_reasoning_effort(client)
    from ..llm_throttle import DEFAULT_MAX_CONCURRENT as _MAXC
    client = patch_client_throttle(client, max_concurrent=_MAXC)  # env LLM_MAX_CONCURRENT, по умолч. 8

    conductor_model = (os.getenv("LLM_MODEL_REASONING") or os.getenv("LLM_MODEL_SMART")
                        or os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"))
    agents_model = (os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                                "gpt-4o-mini"))

    # ── Stage 1: CONDUCTOR ───────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "planning"})
    yield _evt({"type": "stage_status", "stage": "conductor",
                "label": "Анализ вопроса и построение плана",
                "detail": "Кондуктор определяет интент, субъектов и агентов",
                "estimate_s": 8})
    try:
        plan = await plan_research(client, conductor_model, question, history)
    except Exception as e:
        log.exception("[v2] conductor failed: %s", e)
        yield _evt({"type": "text", "chunk": _format_llm_error(e, "планирование")})
        yield _evt({"type": "done"})
        return

    yield _evt({"type": "stage_status", "stage": "plan_ready",
                "label": f"План: {plan.intent}",
                "detail": plan.intent_summary[:120],
                "estimate_s": 0})
    # plan event для UI (шаги = миссии агентов)
    yield _evt({"type": "plan", "steps": plan.to_ui_plan()})

    # ── Stage 2: EXPERT AGENTS ───────────────────────────────────────────
    yield _evt({"type": "phase", "value": "research"})
    # stage_status для самой долгой фазы. Раньше последним stage_status тут был
    # plan_ready (estimate_s=0) → баннер в UI замерзал на «идёт / ~30s» на всю
    # волну агентов (минуты). Даём таймеру опору; _with_heartbeat тикает
    # progress_elapsed по этой оценке, а step_done агентов идут по мере готовности.
    _n_missions = max(1, len(plan.missions))
    yield _evt({"type": "stage_status", "stage": "research",
                "label": "Сбор данных агентами",
                "detail": f"{_n_missions} агент(ов) ищут и читают источники параллельно",
                "estimate_s": min(180, 40 + 22 * _n_missions)})
    bundle = KnowledgeBundle(
        question=question,
        intent=plan.intent_summary or plan.intent,
        subjects=list(plan.subjects),
        subject_labels=dict(plan.subject_labels),
    )

    async for progress_evt in _run_missions_streaming(client, agents_model, plan, bundle):
        yield progress_evt

    # Эмитим источники когда все агенты отработали (полный индекс)
    sources_ui = bundle.sources.to_ui()
    if sources_ui:
        total = len(sources_ui)
        high = sum(1 for s in sources_ui if s["trust_score"] >= 0.85)
        mid = sum(1 for s in sources_ui if 0.6 <= s["trust_score"] < 0.85)
        yield _evt({"type": "sources", "sources": sources_ui})
        yield _evt({"type": "coverage",
                    "total_sources": total, "high_trust": high, "mid_trust": mid,
                    "low_trust": total - high - mid,
                    "pdf_sources": sum(1 for s in sources_ui
                                         if s["url"].lower().endswith(".pdf")),
                    "regulatory_sources": sum(1 for s in sources_ui
                                                if s["source_kind"] == "regulatory"),
                    "sources_per_bank": {}, "parity_warning": None,
                    "warning": None})

    # ── Stage 2.5: РАННЯЯ ОТДАЧА таблицы (perceived latency) — §5a/§5b ────
    # Контракт ранней отдачи (коммит a1e5631) перенесён из EAV-orchestrator:
    # самый ценный артефакт — сравнительная таблица + графики — готов сразу
    # после сбора данных (детерминированно, без LLM). Отдаём его ДО analyst/
    # critic (~40-60с раньше) — пользователь видит «мясо», нарратив идёт следом.
    # write_report(preview_emitted=True) потом НЕ дублирует заголовок/таблицу.
    preview_emitted = False
    try:
        n_subjects = len(bundle.subjects)
        n_facts = len(bundle.facts)
        n_complaints = len(bundle.complaints)
        cov_pct = round(_coverage_pct(bundle))
        yield _evt({"type": "text", "chunk": f"# Аудит-отчёт: {question}\n\n"})
        summary_bits = [f"**{n_subjects}** субъектов",
                          f"**{n_facts}** фактов"]
        if n_complaints:
            summary_bits.append(f"**{n_complaints}** кластеров жалоб")
        summary_bits.append(f"покрытие **{cov_pct:.0f}%**")
        yield _evt({"type": "text", "chunk":
            f"_Сравнение {', '.join(summary_bits)}._\n\n"})
        table_md = bundle.to_comparison_table()
        if table_md:
            yield _evt({"type": "text", "chunk": table_md + "\n\n"})
        early_charts = bundle.extract_chart_specs()
        for ch in early_charts:
            yield _evt({"type": "chart", "spec": ch})
            await asyncio.sleep(0.05)
        preview_emitted = True
        yield _evt({"type": "stage_status", "stage": "preview_ready",
                    "label": "Сравнительная таблица готова",
                    "detail": "Выводы и нарратив формируются…", "estimate_s": 0})
    except Exception as e:
        log.warning("[v2] ранняя отдача таблицы не удалась: %s", e)
        early_charts = []

    # ── Stage 3: ANALYST ─────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "synthesizing"})
    yield _evt({"type": "stage_status", "stage": "analyst",
                "label": "Написание отчёта",
                "detail": f"Из {len(bundle.facts)} фактов, "
                            f"{len(bundle.complaints)} жалоб, "
                            f"{len(bundle.insights)} инсайтов",
                "estimate_s": 20})
    try:
        report_md = await write_report(client, bundle, plan,
                                         preview_emitted=preview_emitted)
    except Exception as e:
        log.exception("[v2] analyst failed: %s", e)
        report_md = (f"# Аудит-отчёт: {question}\n\n"
                       f"⚠ Не удалось сгенерировать отчёт. "
                       f"Собрано {len(bundle.facts)} фактов.")

    # ── Stage 4: CRITIC + REPAIR ─────────────────────────────────────────
    yield _evt({"type": "stage_status", "stage": "critic",
                "label": "Верификация отчёта",
                "detail": "Проверка чисел, обоснованности выводов, покрытия",
                "estimate_s": 12})
    try:
        critique = await critique_report(client, report_md, bundle, question)
    except Exception as e:
        log.warning("[v2] critic failed: %s — skip repair", e)
        critique = Critique(ok=True)

    # Если critic нашёл blocking issues — одна перепись
    if not critique.ok and critique.repair_directive:
        log.warning("[v2] critic: %s blocking, %s weak → repair",
                      len(critique.blocking_issues), len(critique.weak_claims))
        yield _evt({"type": "stage_status", "stage": "repair",
                    "label": "Доработка по замечаниям критика",
                    "detail": critique.repair_directive[:120],
                    "estimate_s": 15})
        try:
            report_md = await _rewrite_with_critique(
                client, report_md, critique, bundle, plan,
                preview_emitted=preview_emitted)
        except Exception as e:
            log.warning("[v2] repair failed: %s", e)

    # Эмитим верификацию. §4c: раньше verified = total − hallucinations, т.е.
    # каждое непомеченное число считалось «проверенным» — но реально сверяются
    # только числа, которые нашлись в bundle.facts. Делаем честно:
    # verified = числа отчёта, сопоставленные с числами фактов; unverified =
    # есть в отчёте, но НЕ найдены в фактах (включая, но не только, галлюц.).
    report_nums = _extract_all_numbers(report_md)
    fact_nums = _collect_fact_numbers(bundle)
    verified_nums, unverified_nums = _split_verified(report_nums, fact_nums)
    # numeric_hallucinations (от Critic) — подмножество unverified, гарантируем
    # что они учтены (не потеряются, если детерм. чек их не словил).
    unverified_set = set(unverified_nums)
    for h in critique.numeric_hallucinations:
        unverified_set.add(round(float(h), 3))
    verified_count = len(report_nums) - len(unverified_set)
    yield _evt({"type": "verification",
                "method": "agent_bundle_grounding",
                "numeric_checked": len(report_nums),
                "verified": max(0, verified_count),
                "unverified": len(unverified_set),
                "checked": True})
    yield _evt({"type": "claim_check",
                "verified": max(0, verified_count),
                "dropped": len(unverified_set),
                "samples": []})

    # ── Stage 5: STREAM FINAL REPORT ─────────────────────────────────────
    # Очередность: отчёт параграфами (для UI-отрисовки)
    paragraphs = report_md.split("\n\n")
    for p in paragraphs:
        if not p.strip():
            continue
        yield _evt({"type": "text", "chunk": p + "\n\n"})
        await asyncio.sleep(0.03)

    # Артефакты для UI
    if bundle.ranking and bundle.ranking.entries:
        yield _evt({"type": "ranking",
                    "criterion": bundle.ranking.criterion,
                    "entries": [{"subject": e.subject,
                                  "subject_label": bundle.subject_labels.get(e.subject, e.subject),
                                  "rank": e.rank, "score": e.score,
                                  "rationale": e.rationale,
                                  "data_gap": e.data_gap,
                                  "evidence_ns": e.evidence_ns}
                                 for e in bundle.ranking.sorted_entries()]})

    if bundle.insights:
        yield _evt({"type": "insights",
                    "items": [{"headline": i.headline,
                                "explanation": i.explanation,
                                "evidence_ns": i.evidence_ns,
                                "impact": i.impact}
                               for i in bundle.insights]})

    # Gaps для UI
    if bundle.coverage_notes:
        yield _evt({"type": "gaps",
                    "insufficient_banks": [],
                    "missing": [{"attribute": n.what,
                                  "missing_banks": n.subjects,
                                  "all": False}
                                 for n in bundle.coverage_notes]})

    # Outline (секции из плана)
    yield _evt({"type": "outline",
                "sections": plan.output_sections or
                              ["summary", "key_findings", "ranking",
                               "complaints", "risks", "methodology", "sources"]})

    # ── Done ─────────────────────────────────────────────────────────────
    elapsed = time.time() - started
    log.warning("[v2] DONE in %.1fs: intent=%s, %s facts, %s complaints, "
                  "%s insights, %s sources, coverage=%.0f%%",
                  elapsed, plan.intent, len(bundle.facts), len(bundle.complaints),
                  len(bundle.insights), len(bundle.sources.all()),
                  _coverage_pct(bundle))
    yield _evt({"type": "phase", "value": "done"})
    yield _evt({"type": "done"})


# ════════════════════════════════════════════════════════════════════════
# MISSIONS RUNNER — параллельный запуск агентов с учётом зависимостей
# ════════════════════════════════════════════════════════════════════════


async def _run_missions_streaming(client: AsyncOpenAI, model: str,
                                     plan: ResearchPlan,
                                     bundle: KnowledgeBundle) -> AsyncIterator[str]:
    """Запускает миссии агентов с эмитом SSE-прогресса.

    Независимые миссии — параллельно, зависимые — после завершения зависимостей.
    Эмитит step_start/step_done события (совместимо со старым фронтом).
    """
    completed: set[str] = set()
    results: dict[str, dict] = {}
    pending = list(plan.missions)

    for wave in range(3):
        if not pending:
            break
        ready = [m for m in pending
                  if all(d in completed for d in plan.dependencies.get(m.agent_id, []))]
        if not ready:
            ready = pending  # не блокируем
        pending = [m for m in pending if m not in ready]

        # Подмешиваем контекст от зависимостей. Зависимые агенты (ranking,
        # иногда market) РАНЬШЕ получали лишь 400-символьную выжимку каждого
        # dependency → ранжировали вслепую и переискали факты. Теперь даём им
        # ПОЛНЫЙ bundle-контекст (все собранные факты/жалобы по субъектам) —
        # grounding становится реальным, агент опирается на готовые данные.
        for m in ready:
            deps = plan.dependencies.get(m.agent_id, [])
            ctx_parts: list[str] = []
            if deps:
                # Короткие summary зависимостей (для понимания «кто что нашёл»).
                ctx_parts.extend(f"[{d}]: {results[d].get('summary', '')[:400]}"
                                  for d in deps if d in results)
                # Полный bundle — это и есть grounding. Числа/цитаты с [N].
                bundle_ctx = bundle.to_prompt_context(max_chars=16000)
                if bundle_ctx.strip():
                    ctx_parts.append("# СОБРАННЫЕ ДАННЫЕ (из других агентов)\n"
                                       "Опирайся на эти факты/жалобы при ранжировании. "
                                       "НЕ переищи то, что уже найдено. Числа/цитаты "
                                       "бери отсюда со ссылками [N].\n\n" + bundle_ctx)
            m.context = "\n\n".join(ctx_parts)

        # step_start для каждого агента волны
        for m in ready:
            n = plan.missions.index(m) + 1
            yield _evt({"type": "step_start", "n": n,
                        "title": f"{_AGENT_LABELS_UI.get(m.agent_id, m.agent_id)}: "
                                  f"{m.goal[:50]}",
                        "tool": m.agent_id,
                        "entity": m.subjects[0] if m.subjects else None})

        # Параллельный запуск волны. step_done эмитим ПО МЕРЕ завершения каждого
        # агента (as_completed), а не разом после всей волны (gather). Иначе вся
        # волна — один глухой await на минуты: панель агентов стоит мёртвой, потом
        # всё «доделывается» мгновенно. Теперь агенты гаснут по одному в реальном
        # времени — пользователь видит актуальную картину.
        async def _tagged(mission: AgentMission):
            try:
                return mission, await _run_one_agent(client, model, mission, bundle)
            except Exception as exc:  # belt-and-suspenders: _run_one_agent сам ловит
                return mission, exc

        tasks = [asyncio.ensure_future(_tagged(m)) for m in ready]
        for fut in asyncio.as_completed(tasks):
            m, res = await fut
            n = plan.missions.index(m) + 1
            if isinstance(res, Exception):
                log.warning("[v2] agent %s failed: %s", m.agent_id, res)
                results[m.agent_id] = {"error": str(res), "summary": ""}
                found = 0
            else:
                results[m.agent_id] = res
                found = res.get("n_tool_calls", 0)
            completed.add(m.agent_id)
            yield _evt({"type": "step_done", "n": n,
                        "found": found, "used": found,
                        "detail": results[m.agent_id].get("summary", "")[:120]})


def _tier_models() -> tuple[str, str]:
    """(smart, fast) из env. fast→Haiku для механических стадий; при отсутствии
    LLM_MODEL_FAST падаем на smart (тиринг безопасно выключается)."""
    smart = (os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"))
    fast = (os.getenv("LLM_MODEL_FAST") or smart)
    return smart, fast


async def _run_one_agent(client: AsyncOpenAI, model: str,
                           mission: AgentMission, bundle: KnowledgeBundle) -> dict:
    """Запускает один агент. Возвращает {agent_id, summary, progress}.

    Модель выбирается по тиру агента (ускорение v2): механические агенты
    (reviews/regulatory/market) — на быстрой; researcher — навигация быстрая,
    извлечение сильное; ranking — сильная."""
    agent_cls = AGENT_REGISTRY.get(mission.agent_id)
    if agent_cls is None:
        log.warning("[v2] unknown agent %s — skipping", mission.agent_id)
        return {"agent_id": mission.agent_id,
                "summary": f"неизвестный агент {mission.agent_id}",
                "n_tool_calls": 0}
    smart, fast = _tier_models()
    tier = getattr(agent_cls, "MODEL_TIER", "smart")
    final_tier = getattr(agent_cls, "FINAL_MODEL_TIER", None) or tier
    loop_model = fast if tier == "fast" else smart
    final_model = fast if final_tier == "fast" else smart
    log.warning("[v2] agent %s: loop=%s, final=%s", mission.agent_id,
                 loop_model.split("/")[-1], final_model.split("/")[-1])
    agent = agent_cls(client=client, model=smart, mission=mission, bundle=bundle,
                       loop_model=loop_model, final_model=final_model,
                       smart_model=smart)
    try:
        result = await agent.run()
    except Exception as e:
        log.exception("[v2] agent %s crashed: %s", mission.agent_id, e)
        return {"agent_id": mission.agent_id, "error": str(e),
                "summary": f"агент упал: {e}", "n_tool_calls": 0}
    summary = result.get("artifacts", {}).get("summary", "")
    return {"agent_id": mission.agent_id, "summary": summary,
            "progress": result.get("progress", {}),
            "n_tool_calls": result.get("progress", {}).get("n_tool_calls", 0)}


# ════════════════════════════════════════════════════════════════════════
# CRITIC REPAIR
# ════════════════════════════════════════════════════════════════════════


async def _rewrite_with_critique(client: AsyncOpenAI, draft: str,
                                   critique: Critique, bundle: KnowledgeBundle,
                                   plan: ResearchPlan,
                                   preview_emitted: bool = False) -> str:
    """Просит Analyst переписать отчёт с учётом замечаний критика.

    preview_emitted — таблица уже отдана ранним preview; просим НЕ вставлять её
    обратно и НЕ дублировать заголовок (контракт ранней отдачи §5a)."""
    from .analyst import SYSTEM_PROMPT, _clean_citations
    issues_block = ""
    if critique.blocking_issues:
        issues_block += "БЛОКИРУЮЩИЕ:\n- " + "\n- ".join(critique.blocking_issues)
    if critique.weak_claims:
        issues_block += "\n\nСЛАБЫЕ ВЫВОДЫ (замени на обоснованные):\n- " + \
                         "\n- ".join(critique.weak_claims)
    if critique.missing_aspects:
        issues_block += "\n\nПРОПУЩЕННЫЕ АСПЕКТЫ: " + ", ".join(critique.missing_aspects)
    if critique.numeric_hallucinations:
        issues_block += ("\n\nЧИСЛА БЕЗ ОПОРЫ (убери или замени на факты из bundle): "
                          + ", ".join(str(n) for n in critique.numeric_hallucinations))

    preview_note = ""
    if preview_emitted:
        preview_note = (
            "\n\n# ВАЖНО: сравнительная таблица уже отрисована вверху отчёта. "
            "НЕ вставляй её отдельной секцией и НЕ пиши повторный заголовок "
            "«# Аудит-отчёт». Начинай с анализа (TL;DR / ключевые выводы)."
        )

    user_msg = (
        f"# ЧЕРНОВИК\n{draft[:10000]}\n\n"
        f"# ЗАМЕЧАНИЯ КРИТИКА\n{issues_block}\n\n"
        f"# ДИРЕКТИВА\n{critique.repair_directive}\n\n"
        f"# BUNDLE\n{bundle.to_prompt_context(max_chars=14000)}{preview_note}\n\n"
        f"Перепиши отчёт, исправив ВСЕ замечания критика. "
        f"Структура и стиль — из системного промпта."
    )
    # Repair = переписывание отчёта (работа аналитика) → та же сильная модель,
    # что и analyst (LLM_MODEL_ANALYST), а не быстрый SMART.
    model = (os.getenv("LLM_MODEL_ANALYST") or os.getenv("LLM_MODEL_SMART")
             or os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"))
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_msg}],
            temperature=0.0, max_tokens=6000,
        )
        md = (resp.choices[0].message.content or "").strip()
        allowed = {i + 1 for i in range(len(bundle.sources.all()))}
        return _clean_citations(md, allowed) or draft
    except Exception as e:
        log.warning("[v2] rewrite failed: %s", e)
        return draft


# ════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════


def _extract_all_numbers(text: str) -> list[float]:
    """Все числа с единицами (для счётчика верификации)."""
    import re
    nums = []
    for m in re.finditer(r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,](\d+))?\s*"
                          r"(?:₽|руб|%|процент|тыс|млн|млрд|лет|год|дн|мес)",
                          text or "", re.IGNORECASE):
        raw = re.sub(r"[ \u00a0\u202f]", "", m.group(1))
        frac = m.group(2)
        try:
            nums.append(float(raw + ("." + frac if frac else "")))
        except ValueError:
            continue
    return nums


def _collect_fact_numbers(bundle: KnowledgeBundle) -> set[float]:
    """Все числа из bundle.facts (value/conditions/verbatim) — реальная база
    для сверки. Переиспользует critic._collect_fact_numbers когда доступно,
    иначе лёгкий встроенный сбор."""
    try:
        from .critic import _collect_fact_numbers as _collect
        return _collect(bundle.facts)
    except Exception:
        pass
    import re
    nums: set[float] = set()
    for f in bundle.facts:
        for txt in [f.value, " ".join(f.conditions), f.verbatim]:
            for m in re.finditer(r"\d[\d .,]*", txt or ""):
                raw = re.sub(r"[ .,]", "", m.group(0))
                if raw.isdigit():
                    nums.add(float(raw))
    return nums


def _split_verified(report_nums: list[float],
                       fact_nums: set[float]) -> tuple[list[float], list[float]]:
    """Разносит числа отчёта на (сопоставленные с фактами, не сопоставленные).
    Сопоставление как в critic: точное совпадение либо относительная
    погрешность < 2%. Годы (1990-2050) считаются safe и идут в verified."""
    safe_years = {float(y) for y in range(1990, 2050)}
    verified: list[float] = []
    unverified: list[float] = []
    for n in report_nums:
        if n in safe_years:
            verified.append(n)
            continue
        if any(abs(n - fn) < 0.001 for fn in fact_nums):
            verified.append(n)
            continue
        if any(fn and abs(n - fn) / abs(fn) < 0.02 for fn in fact_nums if fn):
            verified.append(n)
            continue
        unverified.append(n)
    return verified, unverified


def _coverage_pct(bundle: KnowledgeBundle) -> float:
    """Грубая метрика покрытия: сколько субъектов имеют ≥1 факт."""
    if not bundle.subjects:
        return 0.0
    covered = sum(1 for s in bundle.subjects if bundle.facts_for(s))
    return 100.0 * covered / len(bundle.subjects)
