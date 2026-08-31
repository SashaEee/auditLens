"""Наш планировщик как источник подзапросов для движка gpt-researcher.

ЗАЧЕМ. Штатный планировщик gpt-researcher на вопрос про три банка выдал три
общих подзапроса вида «сравнение оформления дебетовой карты Сбер Т-Банк Альфа
2026» — и в итоге не прочитал ни одной страницы sberbank.ru: поиск по такому
запросу отдаёт обзорные статьи агрегаторов и SEO-блоги (замер 31.08.2026:
20 источников, 0 страниц Сбера, зато zaimi.ru и vc.ru).

Наш Кондуктор раскладывает вопрос по СУБЪЕКТАМ и знает официальные домены —
поэтому здесь его план превращается в подзапросы: на каждый субъект свой
запрос с доменным таргетингом, плюс общие сравнительные. Правил под тип
вопроса тут нет: формулировку даёт сам план, мы лишь размножаем её по
субъектам, кем бы они ни были — банками, регулятором или продуктом.
"""
from __future__ import annotations

import logging

from ..entity_extractor import _BANK_DOMAINS

log = logging.getLogger(__name__)


def plan_to_subqueries(plan, question: str, *, per_subject: int = 1,
                       max_queries: int = 12) -> tuple[list[str], list[str]]:
    """План Кондуктора → (подзапросы, доверенные домены).

    Возвращаем и домены: гейтвей умеет фильтровать их на своей стороне, а
    движок gpt-researcher пробрасывает query_domains в ретривер.
    """
    subjects = list(getattr(plan, "subjects", None) or [])
    labels = dict(getattr(plan, "subject_labels", None) or {})
    product = (getattr(plan, "product", "") or "").strip()
    summary = (getattr(plan, "intent_summary", "") or "").strip()

    # Что именно спрашиваем — берём из плана, а не из шаблона: для вопроса про
    # ставки это «ставка по вкладу», для процессного — «порядок оформления».
    topic = product or summary or question

    domains: list[str] = []
    queries: list[str] = []

    for slug in subjects:
        name = labels.get(slug) or slug
        dom = _BANK_DOMAINS.get(slug)
        if dom and dom not in domains:
            domains.append(dom)
        # Адресный запрос к первоисточнику: именно его не хватало их
        # планировщику, чтобы дойти до сайта банка.
        queries.append(f"{name} {topic} site:{dom}" if dom
                       else f"{name} {topic}")
        if per_subject > 1:
            queries.append(f"{name} {topic} условия документы сроки")

    # Общие запросы: сравнение и то, чего нет на сайтах банков (обзоры, жалобы).
    if len(subjects) > 1:
        names = ", ".join(labels.get(s, s) for s in subjects[:4])
        queries.append(f"{topic} сравнение {names}")
    if not queries:
        queries.append(question)

    # Дедуп с сохранением порядка: адресные запросы должны идти первыми.
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        k = q.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= max_queries:
            break
    return out, domains


def install(plan, question: str) -> None:
    """Подменяет планировщик gpt-researcher нашим планом на этот прогон."""
    from gpt_researcher.skills.researcher import ResearchConductor

    subqueries, domains = plan_to_subqueries(plan, question)

    async def plan_research(self, query, query_domains=None):
        log.info("gptr-planner: %d подзапросов из плана Кондуктора, "
                 "доверенных доменов %d", len(subqueries), len(domains))
        return list(subqueries)

    ResearchConductor.plan_research = plan_research
    return domains
