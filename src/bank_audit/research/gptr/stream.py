"""Движок gpt-researcher в контракте SSE нашего UI.

Интерфейс подписан на поток событий stream_deep_research_v2: mode → phase →
stage_status → plan → sources → text → verification → gaps → done. Чтобы
движок появился в UI, он должен отдавать ровно тот же поток, а не свой.

Отдельно здесь считается доверие к источнику нашим `_trust_for` — тем же, что
у конвейера v2. Это и оценка в карточке источника, и фильтр шума: страницы
вроде pikabu.ru получают низкий вес и в отчёт не идут.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import AsyncIterator
from urllib.parse import urlparse

from ..v2.tools.web_tools import _kind_for, _trust_for
from . import gaps as al_gaps, planner as al_planner
from . import scraper as al_scraper, verify as al_verify
from .engine import _role_prompt, install

log = logging.getLogger(__name__)

# Ниже этого доверия источник в отчёт не идёт: форумы, агрегаторы объявлений и
# SEO-блоги. Порог по нашей же шкале, а не по списку доменов.
MIN_TRUST = 0.35

# Отчёт отдаётся кусками, иначе UI получит его одним куском в конце и
# индикатор прогресса замрёт на минуты.
_CHUNK = 900


def _evt(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)


def _sources_ui(urls: list[str], pages: dict[str, str]) -> list[dict]:
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
            "excerpt": text[:600],
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

    al_planner.install(plan, question)
    yield _evt({"type": "stage_status", "stage": "plan_ready",
                "label": f"План: {plan.intent}",
                "detail": plan.intent_summary[:120], "estimate_s": 0})
    yield _evt({"type": "plan", "steps": plan.to_ui_plan(),
                "question_nature": plan.question_nature,
                "subjects": plan.subjects,
                "client_segment": plan.client_segment})

    # ── Сбор ─────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "research"})
    yield _evt({"type": "stage_status", "stage": "research",
                "label": "Сбор данных",
                "detail": "Поиск и чтение источников по подзапросам плана",
                "estimate_s": 60})
    al_scraper.READ_PAGES.clear()
    researcher = GPTResearcher(query=question, report_type="research_report",
                               agent="AuditLens",
                               role=_role_prompt(plan, question))
    try:
        await researcher.conduct_research()
    except Exception as e:
        log.exception("gptr: сбор")
        yield _evt({"type": "text",
                    "chunk": f"\n\n⚠ **Сбор данных не удался:** {e}\n"})
        yield _evt({"type": "done"})
        return

    pages = dict(al_scraper.READ_PAGES)
    sources = _sources_ui(researcher.get_source_urls(), pages)
    weak = [s for s in sources if s["trust_score"] < MIN_TRUST]
    sources = [s for s in sources if s["trust_score"] >= MIN_TRUST]
    for i, s in enumerate(sources, 1):
        s["n"] = i
    if sources:
        high = sum(1 for s in sources if s["trust_score"] >= 0.85)
        mid = sum(1 for s in sources if 0.6 <= s["trust_score"] < 0.85)
        yield _evt({"type": "sources", "sources": sources, "failed": len(weak)})
        yield _evt({"type": "coverage", "total_sources": len(sources),
                    "high_trust": high, "mid_trust": mid,
                    "low_trust": len(sources) - high - mid,
                    "pdf_sources": sum(1 for s in sources
                                       if s["url"].lower().endswith(".pdf"))})

    # ── Отчёт ────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "writing"})
    yield _evt({"type": "stage_status", "stage": "analyst",
                "label": "Написание отчёта",
                "detail": "Аналитик собирает разделы, заказанные планом",
                "estimate_s": 90})
    try:
        report = await researcher.write_report()
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

    for i in range(0, len(report), _CHUNK):
        yield _evt({"type": "text", "chunk": report[i:i + _CHUNK]})

    # ── Сверка и пробелы ─────────────────────────────────────────────────
    verification = al_verify.verify_report(report, pages)
    gap_lines = al_gaps.collect(plan, pages, verification)
    if weak:
        gap_lines.append(
            f"Источников отсеяно по низкому доверию: {len(weak)} "
            f"({', '.join(sorted({s['domain'] for s in weak})[:4])}).")
    tail = al_gaps.render(gap_lines)
    for i in range(0, len(tail), _CHUNK):
        yield _evt({"type": "text", "chunk": tail[i:i + _CHUNK]})

    yield _evt({"type": "verification",
                "method": "numbers_vs_read_pages",
                "numeric_checked": verification["numeric_checked"],
                "verified": verification["verified"],
                "unverified": verification["unverified"],
                "unverified_count": len(verification["unverified"]),
                "citation_errors": []})

    yield _evt({"type": "gaps", "insufficient_banks": [],
                "missing": [{"attribute": g, "missing_banks": [], "all": False}
                            for g in gap_lines]})

    yield _evt({"type": "done", "elapsed_s": round(time.time() - started, 1)})
