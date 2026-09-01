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
from . import citations as al_cit, critic as al_critic, facts as al_facts
from . import reviews as al_reviews, runstate
from . import gaps as al_gaps, planner as al_planner
from . import scraper as al_scraper, verify as al_verify
from .engine import (_role_prompt, install, report_prompt,
                     stream_report as engine_stream_report)

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
    # Три вещи, которые раньше шли по очереди, теперь идут вместе: сбор,
    # разбор уже прочитанных страниц объектов и выгрузка жалоб из корпуса.
    # Отзывы зависят только от плана и контракта — ждать сбора им незачем.
    registry = al_facts.FactRegistry()
    collecting = {"on": True}
    reviews_task = asyncio.ensure_future(asyncio.to_thread(
        al_reviews.collect, plan, attributes))
    eager_task = asyncio.ensure_future(al_facts.extract_while_collecting(
        registry, client, fast, state=state, attributes=attributes, plan=plan,
        running=lambda: collecting["on"],
        cap=max(1, int(os.getenv("GPTR_EXTRACT_PAGES", "35")) // 2)))

    async def _collect():
        try:
            await researcher.conduct_research()
        finally:
            collecting["on"] = False

    try:
        # Сбор идёт минуту с лишним; без живого счётчика интерфейс показывает
        # неподвижный индикатор, и аудитор не понимает, работает ли система.
        async for ev in _tick(_collect(), lambda: {
                "type": "progress", "stage": "research",
                "pages": len(state.pages),
                "facts": len(registry.facts),
                "blocked": len(state.unreadable)}):
            yield ev
    except Exception as e:
        collecting["on"] = False
        log.exception("gptr: сбор")
        yield _evt({"type": "text",
                    "chunk": f"\n\n⚠ **Сбор данных не удался:** {e}\n"})
        yield _evt({"type": "done"})
        return

    eager_pages = await eager_task
    pages = dict(state.pages)
    unreadable = dict(state.unreadable)

    # Наблюдаемая сторона в первую очередь из собственного корпуса отзывов:
    # там живые жалобы с датами и ссылками, тогда как веб отдаёт обзоры.
    runstate.bind(state)
    review_records = await reviews_task
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
    async def _extract():
        await al_facts.build_registry(
            client, fast, pages=pages, attributes=attributes, plan=plan,
            keep_pages=set(review_pages),
            subject_hints=al_reviews.subject_hints(),
            reg=registry, already=eager_pages)

    async for ev in _tick(_extract(), lambda: {
            "type": "progress", "stage": "facts",
            "pages_total": len(pages), "facts": len(registry.facts),
            "ahead": len(eager_pages)}):
        yield ev
    if review_pages:
        al_reviews.stamp_dates(registry)
    # ── Критик ───────────────────────────────────────────────────────────
    # Слой извлечения намеренно ничего не отбраковывал — судит отдельный
    # модуль. Дешёвая проверка подтверждает очевидное бесплатно (500 фактов за
    # 3 мс), модель зовётся ТОЛЬКО на остаток: пересказ другими словами
    # счётчиком слов не опознать, а снимать подлинное нельзя.
    verdict = await al_critic.review(client, fast, registry, pages)
    if verdict.cut or verdict.mislabeled:
        yield _evt({"type": "stage_status", "stage": "critic",
                    "label": f"Снято без опоры: {verdict.cut}",
                    "detail": (f"проверено {verdict.checked}, дословных "
                               f"{verdict.exact}, пересказов {verdict.close}"),
                    "estimate_s": 0})

    by_stance = {"declared": 0, "observed": 0, "regulatory": 0}
    for f in registry.facts:
        by_stance[f.stance] = by_stance.get(f.stance, 0) + 1
    yield _evt({"type": "facts_summary", "total": len(registry.facts),
                "pages": len(pages), "by_stance": by_stance,
                "critic": verdict.to_ui(),
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
    ext = registry.render_for_writer(labels) if registry.facts else ""
    writer_model = (os.getenv("SMART_LLM") or "").split(":", 1)[-1] or (
        os.getenv("LLM_MODEL_ANALYST") or os.environ["LLM_MODEL_NAME"])
    # Отчёт отдаётся ПО МЕРЕ написания, а якоря перенумеровываются на лету:
    # нумерация идёт по первому упоминанию, то есть ровно в порядке потока.
    renum = al_cit.StreamRenumberer(registry)
    try:
        async for piece in engine_stream_report(
                client, writer_model, question=question, plan=plan,
                context=ext,
                needs_ranking=bool(getattr(plan, "needs_ranking", False)),
                has_regulatory=any(f.stance == "regulatory"
                                   for f in registry.facts)):
            ready = renum.feed(piece)
            if ready:
                yield _evt({"type": "text", "chunk": ready})
    except Exception as e:
        log.exception("gptr: написание")
        yield _evt({"type": "text",
                    "chunk": f"\n\n⚠ **Отчёт не сформирован:** {e}\n"})
        yield _evt({"type": "done"})
        return
    rest = renum.finish()
    if rest:
        yield _evt({"type": "text", "chunk": rest})
    report = renum.text
    if not report.strip():
        yield _evt({"type": "text", "chunk":
                    "\n\n⚠ **Отчёт не сформирован:** модель вернула пустой "
                    "ответ. Проверьте совместимость параметров модели.\n"})
        yield _evt({"type": "done"})
        return

    # Источники и метрики берём у потокового перенумеровщика: в приложение
    # идут ТОЛЬКО те, на кого реально сослались.
    cited_src, cit_stats = renum.sources(), renum.stats()
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

    # ── Сверка и пробелы ─────────────────────────────────────────────────
    verification = al_verify.verify_report(report, registry, pages)
    verification.update({
        "фактов": len(registry.facts),
        "абзацев_без_якоря": al_cit.unanchored_claims(report),
        **cit_stats,
    })
    gap_lines = al_gaps.collect(plan, registry=registry, attributes=attributes,
                                pages=pages, unreadable=unreadable)
    # Снятое критиком — не «ничего не нашлось», а «нашлось, но не подтвердилось».
    # Аудитор обязан видеть разницу.
    gap_lines.extend(verdict.notes)
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
                "critic": verdict.to_ui(),
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
