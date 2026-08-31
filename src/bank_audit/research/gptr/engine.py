"""Сборка движка: план Кондуктора → цикл gpt-researcher → аудиторский отчёт.

Здесь склеиваются четыре адаптера (retriever / scraper / planner / compat),
сверка чисел и раздел честных пробелов. Наружу отдаётся то же, что нужно нашему
UI: текст отчёта, источники, результат сверки, время по стадиям.

Разделы отчёта задаёт ПЛАН, а не шаблон в коде: Кондуктор для вопроса про
качество обслуживания попросит раздел с жалобами, для вопроса про документ
регулятора — нет. Поэтому одинаково работает и для «сравни ставки», и для
«где проще оформить карту», и для «предельные значения ПСК».
"""
from __future__ import annotations

import logging
import os
import time

from . import compat, gaps as al_gaps, planner as al_planner
from . import scraper as al_scraper, verify as al_verify
from .retriever import FleetSearch

log = logging.getLogger(__name__)

_installed = False


def install() -> None:
    """Одноразовая подмена частей gpt-researcher нашими."""
    global _installed
    if _installed:
        return
    compat.install()
    # Без пробы писатель молча отдаёт пустой отчёт: провайдер отвергает
    # temperature у части моделей, а внутренний ретрай gpt-researcher гасит
    # ошибку десятью попытками и возвращает пустую строку.
    compat.probe_models(_configured_models(),
                        base_url=os.environ["OPENAI_BASE_URL"],
                        api_key=os.environ["OPENAI_API_KEY"])
    import gpt_researcher.retrievers as _r
    import gpt_researcher.retrievers.searx.searx as _rs
    _r.SearxSearch = FleetSearch
    _rs.SearxSearch = FleetSearch
    al_scraper.install()
    _installed = True


def _configured_models() -> list[str]:
    """Модели из настроек gpt-researcher: формат «openai:имя-модели»."""
    out = []
    for key in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM"):
        val = os.getenv(key) or ""
        if ":" in val:
            out.append(val.split(":", 1)[1])
    return out


def _role_prompt(plan, question: str) -> str:
    """Роль писателя: требования аудита + разделы, заказанные планом."""
    sections = list(getattr(plan, "output_sections", None) or [])
    intent = (getattr(plan, "intent_summary", "") or "").strip()
    lines = [
        "Ты — аналитик службы внутреннего аудита банка. Пишешь по-русски, "
        "для аудитора, который будет опираться на отчёт в проверке.",
        "",
        "ЖЁСТКИЕ ТРЕБОВАНИЯ:",
        "• Каждое число, условие и срок сопровождай ссылкой на источник, "
        "откуда оно взято. Число без источника не пиши вовсе.",
        "• Предпочитай официальный сайт организации агрегаторам и блогам; "
        "если факт есть только у агрегатора — так и укажи.",
        "• Если по какому-то из объектов данных не нашлось, напиши это прямо. "
        "Не заполняй пробел правдоподобными общими словами.",
        "• Не округляй и не пересчитывай числа источника.",
    ]
    if intent:
        lines += ["", f"ЧТО РЕАЛЬНО ХОЧЕТ УЗНАТЬ АУДИТОР: {intent}"]
    if sections:
        lines += [
            "",
            "РАЗДЕЛЫ ОТЧЁТА (заказаны планом исследования, назови их "
            "по-русски своими словами): " + ", ".join(sections),
        ]
    return "\n".join(lines)


async def run(question: str, *, history: list[dict] | None = None) -> dict:
    """Полный прогон: возвращает отчёт, источники, сверку и тайминги."""
    from openai import AsyncOpenAI
    from gpt_researcher import GPTResearcher
    from ..v2.conductor import plan_research

    install()
    t0 = time.time()

    client = AsyncOpenAI(api_key=os.environ["LLM_API_KEY"],
                         base_url=os.environ["LLM_BASE_URL"])
    plan = await plan_research(
        client, os.environ.get("LLM_MODEL_REASONING") or os.environ["LLM_MODEL_NAME"],
        question, history=history)
    al_planner.install(plan, question)
    t_plan = time.time()

    al_scraper.READ_PAGES.clear()      # доказательная база именно этого прогона
    researcher = GPTResearcher(
        query=question, report_type="research_report",
        agent="AuditLens", role=_role_prompt(plan, question))
    await researcher.conduct_research()
    t_res = time.time()

    report = await researcher.write_report()
    t_write = time.time()

    pages = dict(al_scraper.READ_PAGES)
    verification = al_verify.verify_report(report, pages)
    report += al_gaps.render(al_gaps.collect(plan, pages, verification))

    return {
        "вопрос": question,
        "отчёт": report,
        "план": plan,
        "источники": researcher.get_source_urls(),
        "страницы": pages,
        "сверка": verification,
        "тайминги": {
            "план": round(t_plan - t0, 1),
            "сбор": round(t_res - t_plan, 1),
            "отчёт": round(t_write - t_res, 1),
            "всего": round(time.time() - t0, 1),
        },
    }
