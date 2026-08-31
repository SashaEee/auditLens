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
from . import runstate
from ..v2.tools.web_tools import REGULATOR_DOMAINS

log = logging.getLogger(__name__)


# Куда целиться за нормами. Первые три покрывают подавляющее большинство:
# указания и положения ЦБ, официальные тексты ФЗ, толкования правовых баз.
_REG_SITES = ("cbr.ru", "pravo.gov.ru", "consultant.ru")


def plan_to_subqueries(plan, question: str, *, attributes=None,
                       per_subject: int = 1,
                       max_queries: int = 16) -> tuple[list[str], list[str]]:
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

    # Миссии Кондуктора — источник ДОПОЛНИТЕЛЬНЫХ разрезов темы: жалобы,
    # регуляторные требования, рейтинг. Что именно искать, решает план, а не
    # список тем в коде: для вопроса про ПСК тут окажется миссия regulatory,
    # для вопроса про качество обслуживания — reviews.
    mission_queries: list[str] = []
    for m in (getattr(plan, "missions", None) or []):
        goal = (getattr(m, "focus", "") or getattr(m, "goal", "") or "").strip()
        if not goal or getattr(m, "agent_id", "") == "researcher":
            continue          # основной сбор уже покрыт адресными запросами
        m_subjects = list(getattr(m, "subjects", None) or subjects)
        for slug in (m_subjects[:3] or [None]):
            name = labels.get(slug, slug) if slug else ""
            mission_queries.append(f"{name} {goal}".strip()[:300])

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

    queries.extend(mission_queries)

    # Общие запросы: сравнение и то, чего нет на сайтах банков (обзоры, жалобы).
    if len(subjects) > 1:
        names = ", ".join(labels.get(s, s) for s in subjects[:4])
        queries.append(f"{topic} сравнение {names}")
    # Нормативная рамка ищется ТАМ, ГДЕ ЖИВЁТ. Прежде миссия regulatory
    # схлопывалась в обычную поисковую строку, и запрос «Сбербанк требования к
    # идентификации» уводил на страницы банка: за весь прогон 31.08 в отчёт не
    # попало ни одного регуляторного источника. Наводим на публикаторов норм
    # тем же механизмом site:, что и на сайты банков.
    reg_attr = getattr(attributes, "regulatory", "") if attributes else ""
    if reg_attr:
        for site in _REG_SITES:
            queries.append(f"{reg_attr} {topic} site:{site}"[:300])
        queries.append(f"{reg_attr} {topic} закон требования"[:300])

    # Запросы по ХАРАКТЕРИСТИКАМ контракта, без доменного фильтра.
    #
    # Брать первые три характеристики было ошибкой: наблюдаемая и нормативная
    # добавляются в конец списка, и веб-запросов про жалобы не формировалось
    # НИКОГДА — дыру закрывал только корпус отзывов, а для объектов вне корпуса
    # взгляд со стороны не искался вовсе. Наблюдаемую берём явно, а не по месту.
    attr_list = list(attributes) if attributes else []
    obs = getattr(attributes, "observed", "") if attributes else ""
    regular = [a for a in attr_list if a not in (obs, reg_attr)]
    picks = regular[:2] + ([obs] if obs else [])
    for attr in picks:
        for slug in subjects[:3]:
            queries.append(f"{labels.get(slug, slug)} {attr}".strip()[:300])

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


def install(plan, question: str, attributes=None) -> list[str]:
    """Кладёт подзапросы в состояние прогона и подменяет планировщик.

    Подмена ставится ОДИН РАЗ на процесс, а подзапросы читаются из состояния
    текущего прогона: иначе замыкание последнего вопроса перебивало план того,
    кто ещё считает, и в поиск уходили чужие site:домены.
    """
    from gpt_researcher.skills.researcher import ResearchConductor

    subqueries, domains = plan_to_subqueries(plan, question,
                                             attributes=attributes)
    runstate.current().subqueries = list(subqueries)

    if not getattr(ResearchConductor, "_auditlens_patched", False):
        async def plan_research(self, query, query_domains=None):
            subs = list(runstate.current().subqueries)
            log.info("gptr-planner: %d подзапросов из плана Кондуктора",
                     len(subs))
            return subs or [query]

        ResearchConductor.plan_research = plan_research
        ResearchConductor._auditlens_patched = True
    return domains
