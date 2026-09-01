"""Движок gpt-researcher в контракте SSE нашего UI.

Интерфейс подписан на поток событий stream_deep_research_v2: mode → phase →
stage_status → plan → sources → text → verification → gaps → done. Чтобы
движок появился в UI, он должен отдавать ровно тот же поток, а не свой.

Отдельно здесь считается доверие к источнику нашим `_trust_for` — тем же, что
у конвейера v2. Это и оценка в карточке источника, и фильтр шума: страницы
вроде pikabu.ru получают низкий вес и в отчёт не идут.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import AsyncIterator
from urllib.parse import urlparse

from ..v2.tools.web_tools import _kind_for, _trust_for
from . import citations as al_cit, facts as al_facts
from . import ranking as al_rank, reviews as al_reviews, runstate
from . import gaps as al_gaps, planner as al_planner
from . import scraper as al_scraper, verify as al_verify
from .engine import _role_prompt, install, report_prompt

# Как часто отдавать живой счётчик длинной стадии. Реже — индикатор кажется
# зависшим, чаще — поток забивается служебными событиями.
_TICK_S = 3.0


async def _tick(coro, snapshot):
    """Гоняет корутину и отдаёт снимок прогресса, пока она не закончится."""
    task = asyncio.ensure_future(coro)
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=_TICK_S)
        if not done:
            try:
                yield _evt(snapshot())
            except Exception:
                pass
    await task

log = logging.getLogger(__name__)

# Отчёт отдаётся кусками, иначе UI получит его одним куском в конце и
# индикатор прогресса замрёт на минуты.
_CHUNK = 900


def _evt(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)


def _sources_ui(urls: list[str], pages: dict[str, str],
                cited: dict[str, dict] | None = None) -> list[dict]:
    """Карточки источников. Если передан cited — только процитированные."""
    cited = cited or {}
    out: list[dict] = []
    for i, url in enumerate(urls, 1):
        domain = urlparse(url).netloc.removeprefix("www.")
        text = pages.get(url, "")
        out.append({
            "n": i, "url": url,
            "title": (text.splitlines()[0][:80] if text else url[:80]).lstrip("# "),
            "domain": domain, "bank_slug": None,
            "trust_score": _trust_for(domain, url),
            "source_kind": _kind_for(domain, url),
            "excerpt": (cited.get(url, {}).get("excerpt") or text[:600]),
            "facts": cited.get(url, {}).get("facts") or [],
        })
    return out


async def stream_deep_research_gptr(question: str,
                                    history: list[dict] | None = None,
                                    ) -> AsyncIterator[str]:
    """Тот же контракт событий, что у stream_deep_research_v2."""
    import os

    from openai import AsyncOpenAI
    from gpt_researcher import GPTResearcher

    from ..v2.conductor import plan_research, normalize_question

    started = time.time()
    # Своё состояние на прогон: параллельные вопросы не должны видеть друг друга.
    state = runstate.new_run()
    question = normalize_question(question)
    yield _evt({"type": "mode", "value": "deep"})

    # ── План ─────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "planning"})
    yield _evt({"type": "stage_status", "stage": "conductor",
                "label": "Анализ вопроса и построение плана",
                "detail": "Кондуктор определяет интент, субъектов и агентов",
                "estimate_s": 8})
    try:
        install()
        client = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"],
                             api_key=os.environ["LLM_API_KEY"],
                             max_retries=4, timeout=180.0)
        plan = await plan_research(
            client,
            os.environ.get("LLM_MODEL_REASONING") or os.environ["LLM_MODEL_NAME"],
            question, history=history)
    except Exception as e:
        log.exception("gptr: планирование")
        yield _evt({"type": "text",
                    "chunk": f"\n\n⚠ **Не удалось построить план:** {e}\n"})
        yield _evt({"type": "done"})
        return

    fast = os.getenv("LLM_MODEL_SMART") or os.environ["LLM_MODEL_NAME"]
    attributes = await al_facts.plan_attributes(client, fast, question, plan)
    runstate.bind(state)          # см. runstate.bind: yield сбрасывает контекст
    subqueries, _doms = al_planner.plan_to_subqueries(plan, question,
                                                     attributes=attributes)
    al_planner.install(plan, question, attributes)
    if attributes.degraded:
        # Видимое предупреждение вместо тихой деградации: прогон продолжается,
        # но аудитор обязан знать, что рамка разбора не построена.
        yield _evt({"type": "stage_status", "stage": "degraded",
                    "level": "warn",
                    "label": "Рамка разбора не построена",
                    "detail": (f"модель недоступна ({attributes.degraded}); "
                               f"отчёт будет заметно беднее — взгляд со стороны "
                               f"и нормативная рамка собраны не будут"),
                    "estimate_s": 0})
    yield _evt({"type": "stage_status", "stage": "plan_ready",
                "label": f"План: {plan.intent}",
                "detail": plan.intent_summary[:120], "estimate_s": 0})
    yield _evt({"type": "plan", "steps": plan.to_ui_plan(),
                "question_nature": plan.question_nature,
                "subjects": plan.subjects,
                # Контракт: что обязаны закрыть. Списком, а не объектом —
                # иначе в поток уезжает repr и UI получает мусор.
                "attributes": list(attributes),
                "observed_attribute": attributes.observed,
                "regulatory_attribute": attributes.regulatory,
                "subqueries": subqueries,
                "degraded": attributes.degraded,
                "client_segment": plan.client_segment})

    # ── Сбор ─────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "research"})
    yield _evt({"type": "stage_status", "stage": "research",
                "label": "Сбор данных",
                "detail": "Поиск и чтение источников по подзапросам плана",
                "estimate_s": 60})
    runstate.bind(state)
    researcher = GPTResearcher(query=question, report_type="research_report",
                               agent="AuditLens",
                               role=_role_prompt(plan, question))
    try:
        # Сбор идёт минуту с лишним; без живого счётчика интерфейс показывает
        # неподвижный индикатор, и аудитор не понимает, работает ли система.
        async for ev in _tick(researcher.conduct_research(), lambda: {
                "type": "progress", "stage": "research",
                "pages": len(al_scraper.READ_PAGES),
                "blocked": len(al_scraper.UNREADABLE)}):
            yield ev
    except Exception as e:
        log.exception("gptr: сбор")
        yield _evt({"type": "text",
                    "chunk": f"\n\n⚠ **Сбор данных не удался:** {e}\n"})
        yield _evt({"type": "done"})
        return

    pages = dict(state.pages)
    unreadable = dict(state.unreadable)

    # Наблюдаемая сторона в первую очередь из собственного корпуса отзывов:
    # там живые жалобы с датами и ссылками, тогда как веб отдаёт обзоры.
    runstate.bind(state)
    review_records = al_reviews.collect(plan, attributes)
    review_pages = al_reviews.as_pages(review_records)
    if review_pages:
        pages.update(review_pages)
        yield _evt({"type": "stage_status", "stage": "reviews",
                    "label": f"Жалоб из корпуса: {len(review_pages)}",
                    "detail": "отзывы клиентов с датами и ссылками",
                    "estimate_s": 0})

    # ── Факты ────────────────────────────────────────────────────────────
    # Между чтением и письмом появляется типизированный слой: каждый факт
    # опирается на дословную цитату, и она проверяется подстрочным поиском.
    yield _evt({"type": "phase", "value": "extraction"})
    yield _evt({"type": "stage_status", "stage": "facts",
                "label": "Извлечение фактов",
                "detail": f"{len(pages)} страниц → проверяемые утверждения",
                "estimate_s": 40})
    runstate.bind(state)
    _reg_box: dict = {}

    async def _extract():
        _reg_box["r"] = await al_facts.build_registry(
            client, fast, pages=pages, attributes=attributes, plan=plan,
            keep_pages=set(review_pages),
            subject_hints=al_reviews.subject_hints())

    async for ev in _tick(_extract(), lambda: {
            "type": "progress", "stage": "facts",
            "pages_total": len(pages)}):
        yield ev
    registry = _reg_box.get("r") or al_facts.FactRegistry()
    if review_pages:
        al_reviews.stamp_dates(registry)
    by_stance = {"declared": 0, "observed": 0, "regulatory": 0}
    for f in registry.facts:
        by_stance[f.stance] = by_stance.get(f.stance, 0) + 1
    yield _evt({"type": "facts_summary", "total": len(registry.facts),
                "pages": len(pages), "by_stance": by_stance,
                "subjects": len({f.subject for f in registry.facts if f.subject})})
    yield _evt({"type": "stage_status", "stage": "facts_ready",
                "label": f"Фактов: {len(registry.facts)}",
                "detail": (f"заявлено {by_stance['declared']}, со стороны "
                           f"{by_stance['observed']}, норм {by_stance['regulatory']}"),
                "estimate_s": 0})

    # ── Отчёт ────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "writing"})
    yield _evt({"type": "stage_status", "stage": "analyst",
                "label": "Написание отчёта",
                "detail": "Аналитик собирает разделы, заказанные планом",
                "estimate_s": 90})
    labels = dict(getattr(plan, "subject_labels", None) or {})
    rank_rows = al_rank.build(plan, registry, attributes)
    if rank_rows:
        yield _evt({"type": "ranking",
                    "criterion": "полнота раскрытия характеристик контракта",
                    "rows": [r.to_ui() for r in rank_rows]})
    try:
        # Писатель получает НЕ ком контекста, а реестр фактов с якорями.
        ext = registry.render_for_writer(labels) if registry.facts else None
        if ext and rank_rows:
            ext = ext + "\n\n" + al_rank.render(rank_rows)
        # custom_prompt заменяет шаблон gpt-researcher целиком: иначе его
        # собственные правила цитирования перебивают наши якоря.
        report = await researcher.write_report(
            ext_context=ext,
            custom_prompt=report_prompt(
                plan, question,
                has_ranking=bool(rank_rows),
                has_regulatory=any(f.stance == "regulatory"
                                   for f in registry.facts)) if ext else "")
    except Exception as e:
        log.exception("gptr: написание")
        yield _evt({"type": "text",
                    "chunk": f"\n\n⚠ **Отчёт не сформирован:** {e}\n"})
        yield _evt({"type": "done"})
        return
    if not (report or "").strip():
        # Пустая строка вместо отчёта — известный отказ: провайдер отверг
        # параметр, а внутренний ретрай движка проглотил ошибку.
        yield _evt({"type": "text", "chunk":
                    "\n\n⚠ **Отчёт не сформирован:** модель вернула пустой "
                    "ответ. Проверьте совместимость параметров модели.\n"})
        yield _evt({"type": "done"})
        return

    # Якоря [f:id] → номера источников; в приложение идут ТОЛЬКО те, на кого
    # реально сослались. Источник, ничего не давший отчёту, исчезает сам.
    report, cited_src, cit_stats = al_cit.renumber(report, registry)
    log.info("цитаты: %s", cit_stats)

    cited_map = {c["url"]: {"facts": c["facts"],
                            "excerpt": (c["facts"][0]["verbatim"]
                                        if c["facts"] else "")}
                 for c in cited_src}
    sources = _sources_ui([c["url"] for c in cited_src], pages, cited_map)
    dropped = len(pages) - len(sources)
    if sources:
        high = sum(1 for s in sources if s["trust_score"] >= 0.85)
        mid = sum(1 for s in sources if 0.6 <= s["trust_score"] < 0.85)
        # dropped — прочитано, но НЕ процитировано. Это не «недоступен»:
        # поле failed UI рисует как «источники недоступны», поэтому шлём 0 и
        # отдаём честное число отдельным полем.
        yield _evt({"type": "sources", "sources": sources, "failed": 0,
                    "read_not_cited": dropped})
        yield _evt({"type": "coverage", "total_sources": len(sources),
                    "high_trust": high, "mid_trust": mid,
                    "low_trust": len(sources) - high - mid,
                    "read_total": len(pages), "unused": dropped,
                    "pdf_sources": sum(1 for s in sources
                                       if s["url"].lower().endswith(".pdf"))})

    for i in range(0, len(report), _CHUNK):
        yield _evt({"type": "text", "chunk": report[i:i + _CHUNK]})

    # ── Сверка и пробелы ─────────────────────────────────────────────────
    verification = al_verify.verify_report(report, registry, pages)
    verification.update({
        "фактов": len(registry.facts),
        "абзацев_без_якоря": al_cit.unanchored_claims(report),
        **cit_stats,
    })
    gap_lines = al_gaps.collect(plan, registry=registry, attributes=attributes,
                                pages=pages, unreadable=unreadable)
    tail = al_gaps.render(gap_lines)
    for i in range(0, len(tail), _CHUNK):
        yield _evt({"type": "text", "chunk": tail[i:i + _CHUNK]})

    yield _evt({"type": "verification",
                "method": "numbers_vs_read_pages",
                "numeric_checked": verification["numeric_checked"],
                "verified": verification["verified"],
                "unverified": verification["unverified"],
                "unverified_count": len(verification["unverified"]),
                "facts_total": len(registry.facts),
                "citations": cit_stats.get("цитирований", 0),
                "unanchored_paragraphs": verification["абзацев_без_якоря"],
                "manual_check": verification.get("manual_check") or [],
                "base": verification.get("база", ""),
                # Якоря на несуществующие факты — это и есть ошибки цитирования.
                # Раньше поле уезжало пустым, хотя число уже было посчитано.
                "citation_errors": ([
                    f"якорей на несуществующие факты: "
                    f"{cit_stats.get('якорей_в_никуда', 0)}"]
                    if cit_stats.get("якорей_в_никуда") else [])})

    yield _evt({"type": "gaps", "insufficient_banks": [],
                "missing": [{"attribute": g, "missing_banks": [], "all": False}
                            for g in gap_lines]})

    yield _evt({"type": "done", "elapsed_s": round(time.time() - started, 1)})
