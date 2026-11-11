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
import os
import re
import time
from typing import AsyncIterator
from urllib.parse import urlparse

from ...ai.hermes_quick import PlainKeysStream, plain_keys
from ..v2.tools.web_tools import _kind_for, _trust_for
from . import citations as al_cit, critic as al_critic, facts as al_facts
from . import followup as al_followup
from . import own_data as al_own, reviews as al_reviews, runstate
from . import dossier as al_dossier
from . import viz as al_viz
from . import gaps as al_gaps, planner as al_planner
from . import lead as al_lead
from . import deck_html as al_deck_html
from . import memory as al_memory
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

# Заглушки сорванного прогона: по ним сохранение отличает сбой от отчёта —
# раньше «⚠ Не удалось построить план: module …» ложилось в историю отчётом
# с красивым названием (аудит 03.10, ИИ-09)
FAIL_PLAN = "⚠ **Не удалось построить план отчёта.**"
FAIL_COLLECT = "⚠ **Сбор данных не удался.**"
FAIL_REPORT = "⚠ **Отчёт не сформирован.**"
FAIL_PREFIXES = (FAIL_PLAN, FAIL_COLLECT, FAIL_REPORT,
                 "⚠ **Не удалось построить план:**", "⚠ **Сбор данных не удался:**",
                 "⚠ **Отчёт не сформирован:**")

# Отчёт отдаётся кусками, иначе UI получит его одним куском в конце и
# индикатор прогресса замрёт на минуты.
_CHUNK = 900


def _evt(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)


def _sources_ui(urls: list[str], pages: dict[str, str],
                cited: dict[str, dict] | None = None,
                dates: dict[str, str] | None = None,
                own: dict[str, dict] | None = None,
                kb: dict[str, dict] | None = None) -> list[dict]:
    """Карточки источников. Если передан cited — только процитированные.
    own — страницы собственных данных AuditLens: адрес ведёт на срез вкладки.
    kb — документы, взятые из базы знаний: дата загрузки и ссылка на карточку."""
    cited = cited or {}
    dates = dates or {}
    own = own or {}
    kb = kb or {}
    out: list[dict] = []
    for i, url in enumerate(urls, 1):
        text = pages.get(url, "")
        mine = own.get(url)
        if mine and mine.get("kind") != "review":
            out.append({
                "n": i, "url": url, "title": mine.get("title", "")[:120],
                "domain": "AuditLens", "bank_slug": None, "trust_score": 0.95,
                "source_kind": "auditlens",
                "excerpt": (cited.get(url, {}).get("excerpt") or text[:600]),
                "facts": cited.get(url, {}).get("facts") or [],
                "published": dates.get(url, ""), "dead": False})
            continue
        domain = urlparse(url).netloc.removeprefix("www.")
        out.append({
            "n": i, "url": url,
            "title": (text.splitlines()[0][:80] if text else url[:80]).lstrip("# "),
            "domain": domain, "bank_slug": None,
            "trust_score": _trust_for(domain, url),
            "source_kind": _kind_for(domain, url),
            "excerpt": (cited.get(url, {}).get("excerpt") or text[:600]),
            "facts": cited.get(url, {}).get("facts") or [],
            # Дата публикации, если источник её объявил. Без неё отчёт выглядит
            # одинаково свежим целиком, хотя часть страниц может быть старой.
            "published": dates.get(url, ""),
            "dead": bool(cited.get(url, {}).get("dead")),
        })
        if url in kb:
            # Из базы знаний: текст на дату загрузки, а не на сегодня.
            out[-1].update({"kb_doc": kb[url].get("link") or "", "kb_loaded": kb[url].get("loaded") or "",
                            # страница сегодня не открылась — в отчёте её сохранённая копия
                            "kb_fallback": bool(kb[url].get("fallback")),
                            "title": (kb[url].get("title") or out[-1]["title"])[:120]})
    return out


# Сколько ждём дослежку сверх основного сбора: обычно она успевает раньше.
_FOLLOWUP_WAIT = float(os.getenv("GPTR_FOLLOWUP_WAIT", "40"))
_FOREIGN = re.compile(
    r"(^|\.)(sber|vtb|alfa|gazprombank|tbank|tinkoff|raiffeisen|psbank|sovcombank|rshb|"
    r"mtsbank|domrf|otpbank|rosbank)[\w-]*\.(by|kz|uz|kg|am|az|ge|md|ua|tj)$", re.I)


def _unverified_item(x) -> dict:
    if isinstance(x, dict):
        return x
    try:
        v = float(x)
        num = (f"{v:,.0f}".replace(",", " ") if v == int(v)
               else f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ","))
    except (TypeError, ValueError):
        num = str(x)
    return {"claim": f"число {num}",
            "issue": "не найдено в источнике рядом с цитатой — вероятно, посчитано "
                     "в отчёте; сверить вручную"}


def _foreign_affiliate(url: str) -> bool:
    return bool(_FOREIGN.search(urlparse(url).netloc.split(":")[0]))


async def stream_deep_research_gptr(question: str,
                                    history: list[dict] | None = None,
                                    fresh: bool = False,
                                    ) -> AsyncIterator[str]:
    """Тот же контракт событий, что у stream_deep_research_v2.

    fresh — аудитор попросил собрать заново: память прошлого отчёта не берём."""
    import os

    from openai import AsyncOpenAI
    from gpt_researcher import GPTResearcher

    from ..v2.conductor import plan_research, normalize_question

    started = time.time()
    # Своё состояние на прогон: параллельные вопросы не должны видеть друг друга.
    state = runstate.new_run()
    marks = {"t": started}

    def lap(name: str) -> None:
        """Сколько занял шаг: аудит 11.11 — учёта времени по шагам не было, и
        никто не видел, что план занимает минуту из пяти."""
        now = time.time()
        state.timings[name] = round(state.timings.get(name, 0.0) + now - marks["t"], 1)
        marks["t"] = now
    question = normalize_question(question)
    yield _evt({"type": "mode", "value": "deep"})
    # Что уже собрано прошлым отчётом этого разговора (memory.py): если новый
    # вопрос продолжает тему, начнём с этих фактов и доберём недостающее.
    prior = None
    try:
        if not fresh:
            prior = await asyncio.to_thread(al_memory.latest, state.origin.get("session_id"),
                                            state.origin.get("username"))
    except Exception as e:  # noqa: BLE001 — память необязательна
        log.info("память исследования: %s", type(e).__name__)
    runstate.bind(state)

    # ── План ─────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "planning"})
    yield _evt({"type": "stage_status", "stage": "conductor",
                "label": "Разбираю вопрос",
                "detail": "какая задача, о ком речь и что нужно найти",
                "estimate_s": 8})
    try:
        install()
        client = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"],
                             api_key=os.environ["LLM_API_KEY"],
                             max_retries=4, timeout=180.0)
        plan = await plan_research(
            client,
            # План — первый шаг, его ждёт каждый отчёт: своя модель (GPTR_PLAN_MODEL),
            # чтобы её можно было менять отдельно от замысла и письма.
            (os.environ.get("GPTR_PLAN_MODEL") or os.environ.get("LLM_MODEL_REASONING")
             or os.environ["LLM_MODEL_NAME"]),
            question, history=history,
            prior=al_memory.brief(prior) if prior else None,
            prior_subjects=({s_: ((prior.get("plan") or {}).get("subject_labels") or {}).get(s_, s_)
                             for s_ in (prior.get("plan") or {}).get("subjects") or []}
                            if prior else None))
    except Exception:
        log.exception("gptr: планирование")
        # текст исключения пользователю не показываем — он в логе (аудит 03.10, ИИ-09)
        yield _evt({"type": "text",
                    "chunk": f"\n\n{FAIL_PLAN} Модель не ответила — повторите запрос через минуту.\n"})
        yield _evt({"type": "done"})
        return

    # Продолжение: планировщик счёл вопрос той же темой, предмет совпадает и
    # новых организаций нет (по новой организации нужен обычный сбор).
    cont = bool(prior and getattr(plan, "continues", False)
                and al_memory.same_topic(plan, prior)
                and not al_memory.new_subjects(plan, prior))
    fast = os.getenv("LLM_MODEL_SMART") or os.environ["LLM_MODEL_NAME"]
    attributes = await al_facts.plan_attributes(client, fast, question, plan)
    lap("plan")
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
                "label": "План готов",
                "detail": plan.intent_summary[:160], "estimate_s": 0})
    yield _evt({"type": "plan", "steps": plan.to_ui_plan(),
                "question_nature": plan.question_nature,
                "subjects": plan.subjects,
                # Контракт: что обязаны закрыть. Списком, а не объектом —
                # иначе в поток уезжает repr и UI получает мусор.
                "attributes": list(attributes),
                "observed_attribute": attributes.observed,
                "regulatory_attribute": attributes.regulatory,
                "subqueries": [] if cont else subqueries,
                "degraded": attributes.degraded,
                # Продолжение прошлого отчёта: что уже собрано и когда.
                "continues": cont,
                "prior": ({"title": prior.get("title") or prior.get("question") or "",
                           "date": al_memory.when(prior),
                           "facts": len(prior.get("facts") or []),
                           "sources": len(prior.get("sources") or {})} if cont else None),
                "client_segment": plan.client_segment,
                # Задача и рамка — чтобы интерфейс показал, как понят вопрос.
                "task": getattr(plan, "task", ""), "scope": getattr(plan, "scope", ""),
                "checks": getattr(plan, "checks", ""),
                "deliverable": getattr(plan, "deliverable", "report"),
                "period": getattr(plan, "period", ""),
                "subject_labels": dict(getattr(plan, "subject_labels", None) or {})})

    # ── Сбор ─────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "research"})
    registry = al_facts.FactRegistry()
    collecting = {"on": True}
    prior_ids: set = set()
    prior_support = {"exact": 0, "close": 0}
    followup_task = None
    eager_pages: dict = {}
    runstate.bind(state)
    if cont:
        # Продолжение прошлого отчёта: его проверенные факты — сразу в реестр,
        # общий сбор по сети не идёт. Свежие данные AuditLens читаются как
        # обычно; чего не хватит — доберёт руководитель исследования (lead.py).
        n_prior, prior_support = al_memory.preload(registry, state, prior)
        prior_ids = {f.id for f in registry.facts}
        yield _evt({"type": "stage_status", "stage": "memory",
                    "label": "Продолжаю прошлое исследование",
                    "detail": (f"уже собрано: фактов {n_prior}, источников "
                               f"{len(prior.get('sources') or {})}"
                               + (f" · отчёт от {al_memory.when(prior)}"
                                  if al_memory.when(prior) else "")),
                    "estimate_s": 10})
        runstate.bind(state)
        own_task = asyncio.ensure_future(al_own.collect(client, fast, question, plan,
                                                          state=state))
        await asyncio.wait({own_task})
        runstate.bind(state)
        pages = dict(state.pages)
        unreadable = dict(state.unreadable)
    else:
        yield _evt({"type": "stage_status", "stage": "research",
                    "label": "Ищу и читаю источники",
                    "detail": "сайты банков, регулятор, обзоры и новости",
                    "estimate_s": 60})
        own_task = asyncio.ensure_future(al_own.collect(client, fast, question, plan,
                                                          state=state))
        runstate.bind(state)
        researcher = GPTResearcher(query=question, report_type="research_report",
                                   agent="AuditLens",
                                   role=_role_prompt(plan, question))
        # Три вещи, которые раньше шли по очереди, теперь идут вместе: сбор,
        # разбор уже прочитанных страниц объектов и собственные данные AuditLens
        # (аналитика жалоб, жалобы из разметки, лазейки). Им ждать сбора незачем.

        async def _followup() -> dict[str, str]:
            # Дослежка сюжета из жалоб — как только готовы собственные данные,
            # параллельно основному сбору (см. followup.py).
            od = await own_task
            runstate.bind(state)
            if not od.complaints:
                return {}
            return await al_followup.collect(client, fast, question, od, state=state)
        followup_task = asyncio.ensure_future(_followup())
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
        except Exception:
            collecting["on"] = False
            log.exception("gptr: сбор")
            yield _evt({"type": "text",
                        "chunk": f"\n\n{FAIL_COLLECT} Поиск или чтение источников не ответили — "
                                 f"повторите запрос через минуту.\n"})
            yield _evt({"type": "done"})
            return

        eager_pages = await eager_task
        pages = dict(state.pages)
        unreadable = dict(state.unreadable)
    lap("collect")

    # Наблюдаемая сторона — прежде всего собственные данные: аналитика жалоб
    # и жалобы из разметки тем же слоем, что вкладка «Отзывы»; веб даёт обзоры.
    runstate.bind(state)
    own = await own_task
    review_pages: dict[str, str] = {}
    if own.complaints or own.loopholes or own.pages or own.knowledge or own.knowledge_live:
        sc = own.scope or {}
        got = [f"страниц из базы знаний перечитано сегодня {own.knowledge_live}"
               if own.knowledge_live else "",
               f"документов базы знаний {own.knowledge}" if own.knowledge else "",
               f"жалоб {own.complaints}" if sc.get("complaints", True) else "",
               f"лазеек {own.loopholes}" if sc.get("loopholes", True) else "",
               f"предложений рынка {own.market}" if own.market else "",
               f"новостей {own.news}" if own.news else ""]
        themes = [al_own.T.theme_label(k) for k in sc.get("themes") or []]
        yield _evt({"type": "stage_status", "stage": "reviews",
                    "label": "Данные AuditLens: " + ", ".join(x for x in got if x),
                    "detail": ("срез: " + ", ".join(x for x in (
                        sc.get("product") or "все продукты",
                        f"{sc.get('days')} дн" if sc.get("days") else "",
                        ("темы: " + ", ".join(themes)) if themes else "")
                        if x)),
                    "estimate_s": 0})
    if (not cont and not own.complaints and getattr(plan, "subjects", None)
            and (own.scope or {}).get("complaints", True)):
        # Запасной путь — старый корпус banki.ru: объект вне разметки или сбой слоя.
        review_records = await asyncio.to_thread(al_reviews.collect, plan, attributes)
        runstate.bind(state)
        review_pages = al_reviews.as_pages(review_records)
        if review_pages:
            pages.update(review_pages)
            yield _evt({"type": "stage_status", "stage": "reviews",
                        "label": f"Жалоб из старого корпуса: {len(review_pages)}",
                        "detail": "разметки по объектам нет — запасной источник",
                        "estimate_s": 0})

    fu_pages: dict[str, str] = {}
    if followup_task is not None:
        try:
            fu_pages = await asyncio.wait_for(asyncio.shield(followup_task),
                                              timeout=_FOLLOWUP_WAIT)
        except Exception as e:  # noqa: BLE001 — дослежка необязательна
            log.info("дослежка: %s", type(e).__name__)
            followup_task.cancel()
            fu_pages = {}
    runstate.bind(state)
    if fu_pages:
        pages.update(fu_pages)
        yield _evt({"type": "stage_status", "stage": "reviews",
                    "label": f"Проверяю, о чём пишут клиенты: прочитано {len(fu_pages)}",
                    "detail": "событие, продавец, правила — по тому, что названо в жалобах",
                    "estimate_s": 0})
    for u in [u for u in pages if _foreign_affiliate(u)]:
        # Сбер Банк Беларусь — другое юрлицо: его адреса и сроки попадали в
        # отчёт как «сведения о Сбере» и порождали мнимые расхождения.
        log.info("исключено: зарубежная дочка/одноимённый банк — %s", u[:90])
        pages.pop(u, None)

    # ── Факты ────────────────────────────────────────────────────────────
    # Между чтением и письмом появляется типизированный слой: каждый факт
    # опирается на дословную цитату, и она проверяется подстрочным поиском.
    # В продолжении читаем только новое: документы базы знаний, которых не было
    # в прошлом отчёте. «Страницы» памяти — это уже выписанные цитаты.
    to_read = ({u: t for u, t in pages.items()
                if u in state.preloaded and u not in (prior.get("sources") or {})}
               if cont else pages)
    yield _evt({"type": "phase", "value": "extraction"})
    yield _evt({"type": "stage_status", "stage": "facts",
                "label": "Выписываю факты с цитатами",
                "detail": f"страниц в разборе: {len(to_read)}",
                "estimate_s": 40})
    runstate.bind(state)
    async def _extract():
        # Страницы дослежки — мимо общего отбора и со своей характеристикой:
        # отбор меряет близость к характеристикам продукта («сроки», «основания
        # отказа»), и новость о событии за жалобами ему заведомо проигрывала —
        # прочитанные 8 страниц не дали отчёту ни одного факта (26.09).
        await asyncio.gather(
            al_facts.build_registry(
                client, fast, pages={u: t for u, t in to_read.items() if u not in fu_pages},
                attributes=attributes, plan=plan,
                # документы базы знаний и жалобы корпуса разбираются вне очереди
                keep_pages=set(review_pages) | {
                    u for u in state.preloaded
                    # документ базы знаний без привязки к банку проходит общий отбор
                    if u not in state.kb_meta or state.kb_meta[u].get("subject")},
                subject_hints=al_reviews.subject_hints(),
                reg=registry, already=eager_pages),
            al_facts.extract_into(
                registry, client, fast, pages=fu_pages,
                attributes=[*attributes, al_followup.EVENT_ATTRIBUTE], plan=plan))

    async for ev in _tick(_extract(), lambda: {
            "type": "progress", "stage": "facts",
            "pages_total": len(to_read), "facts": len(registry.facts),
            "ahead": len(eager_pages)}):
        yield ev
    # Собственные данные — после извлечения: их факты собрал код (числа среза,
    # пересказ и сверенная цитата разметки), пересказывать их моделью незачем.
    runstate.bind(state)
    # В продолжении сводки AuditLens могли остаться с прошлого отчёта — тот же
    # факт дважды не кладём.
    have = {(f.url, f.verbatim) for f in registry.facts} if cont else set()
    for kw in own.facts:
        if (kw.get("url"), kw.get("verbatim")) in have:
            continue
        registry.add(**kw)
    pages.update(own.pages)
    if review_pages:
        al_reviews.stamp_dates(registry)
    lap("facts")
    # ── Критик ───────────────────────────────────────────────────────────
    # Слой извлечения намеренно ничего не отбраковывал — судит отдельный
    # модуль. Дешёвая проверка подтверждает очевидное бесплатно (500 фактов за
    # 3 мс), модель зовётся ТОЛЬКО на остаток: пересказ другими словами
    # счётчиком слов не опознать, а снимать подлинное нельзя.
    # Параллельно критику руководитель исследования решает, хватает ли собранного
    # для ответа по существу; если главного не нашли — один прицельный добор.
    need_task = (asyncio.ensure_future(al_lead.assess(
        client, al_lead.model_for(), question=question, plan=plan, registry=registry, state=state))
        if al_lead.enabled() and getattr(plan, "task", None) is not None else None)
    if cont:
        # Факты памяти прошли критика в своём прогоне — судим только новое.
        verdict = await al_critic.review_added(
            client, fast, registry, pages, prior_ids,
            al_critic.Verdict(exact=prior_support["exact"], close=prior_support["close"]))
    else:
        verdict = await al_critic.review(client, fast, registry, pages)
    lap("critic")
    filled = None
    checked_ids = {f.id for f in registry.facts} | set(verdict.cut_ids)
    if need_task is not None:
        runstate.bind(state)
        need = await need_task
        if not need.enough:
            yield _evt({"type": "stage_status", "stage": "lead",
                        "label": "Главного не хватает — ищу ещё",
                        "detail": (need.missing or "уточняю поиск")[:160], "estimate_s": 40})
            runstate.bind(state)
            filled = await al_lead.fill(client, fast, need, plan=plan, registry=registry,
                                        attributes=attributes, state=state)
            runstate.bind(state)
            pages.update({u: state.pages[u] for u in filled.urls if u in state.pages})
            unreadable = dict(state.unreadable)
            if filled.facts:
                verdict = await al_critic.review_added(client, fast, registry, pages,
                                                       checked_ids, verdict)
            yield _evt({"type": "stage_status", "stage": "lead",
                        "label": (f"Добор: прочитано {filled.pages}, фактов +{filled.facts}"
                                  if filled.pages else "Добор: новых страниц не нашлось"),
                        "detail": (need.missing or "")[:160], "estimate_s": 0})
        lap("lead")
    if verdict.cut or verdict.mislabeled:
        yield _evt({"type": "stage_status", "stage": "critic",
                    "label": f"Снято утверждений без опоры на источник: {verdict.cut}",
                    "detail": (f"проверено {verdict.checked}, дословных "
                               f"{verdict.exact}, пересказов {verdict.close}"),
                    "estimate_s": 0})

    by_stance = {"declared": 0, "observed": 0, "regulatory": 0, "loophole": 0}
    for f in registry.facts:
        by_stance[f.stance] = by_stance.get(f.stance, 0) + 1
    yield _evt({"type": "facts_summary", "total": len(registry.facts),
                "pages": len(pages), "by_stance": by_stance,
                "critic": verdict.to_ui(),
                "subjects": len({f.subject for f in registry.facts if f.subject})})
    yield _evt({"type": "stage_status", "stage": "facts_ready",
                "label": f"Фактов: {len(registry.facts)}",
                "detail": (f"заявлено {by_stance['declared']}, со стороны "
                           f"{by_stance['observed']}, норм {by_stance['regulatory']}"
                           + (f", лазеек {by_stance['loophole']}" if by_stance["loophole"] else "")),
                "estimate_s": 0})

    # ── Отчёт ────────────────────────────────────────────────────────────
    yield _evt({"type": "phase", "value": "writing"})
    yield _evt({"type": "stage_status", "stage": "analyst",
                "label": "Пишу отчёт",
                "detail": "сначала главный ответ, затем разделы вокруг него",
                "estimate_s": 90})
    writer_model = (os.getenv("SMART_LLM") or "").split(":", 1)[-1] or (
        os.getenv("LLM_MODEL_ANALYST") or os.environ["LLM_MODEL_NAME"])
    # Отчёт пишется ПО РАЗДЕЛАМ (dossier.py): у каждого раздела свои факты
    # целиком, а не «по три на ячейку». Тело стримится по мере написания,
    # резюме и план проверки пишутся последними и вставляются наверх.
    # Якоря перенумеровываются одним перенумеровщиком на все разделы —
    # нумерация по первому упоминанию, в порядке потока.
    renum = al_cit.StreamRenumberer(registry)
    guard = al_viz.MarkerGuard()      # маркер не должен родиться из обрывков и якоря
    # Служебные ключи тем (chargeback, card_block…) — на подписи, как в быстром
    # режиме; адреса ссылок не трогаем.
    keys = PlainKeysStream()
    _ttl = al_dossier.titles(plan)     # заголовки уточнит бриф (событие titles)
    gaps_preview = al_gaps.render(al_gaps.collect(
        plan, registry=registry, attributes=attributes,
        pages=pages, unreadable=unreadable,
        cached_copies=dict(state.cached_copies))) if registry.facts else ""
    # Текст собираем в ПОРЯДКЕ ЧТЕНИЯ: тело стримится по мере написания, а
    # резюме с планом проверки приходят последними и встают наверх. У
    # перенумеровщика порядок подачи, и полагаться на его text нельзя —
    # сохранённый отчёт получил бы резюме в конце.
    body_parts: list[str] = []
    lead_text = ""
    report_name = ""
    try:
        async for kind, payload in al_dossier.write_dossier(
                client, writer_model, question=question, plan=plan,
                registry=registry, gaps_text=gaps_preview, state=state):
            if kind == "titles":
                _ttl.update(payload)
            elif kind == "report_title":
                report_name = payload
                yield _evt({"type": "report_title", "title": payload})
            elif kind == "outline":
                # Оглавление — после брифа: состав разделов теперь зависит от
                # вопроса, а не от шаблона.
                yield _evt({"type": "outline", "sections": payload})
            elif kind == "section":
                yield _evt({"type": "stage_status", "stage": "analyst",
                            "label": f"Пишу раздел: {_ttl.get(payload, payload)}",
                            "detail": ""})
            elif kind == "chunk":
                ready = keys.feed(guard.feed(renum.feed(payload)))
                if ready:
                    body_parts.append(ready)
                    yield _evt({"type": "text", "chunk": ready})
            elif kind == "marker":
                # Место блока визуализации. Сначала сбрасываем придержанный
                # хвост перенумеровщика, иначе якорь вылез бы после маркера.
                tail = keys.feed(guard.feed(renum.finish()) + guard.finish()) + keys.finish()
                if tail:
                    body_parts.append(tail)
                    yield _evt({"type": "text", "chunk": tail})
                mk = al_viz.marker(payload)
                body_parts.append(mk)
                yield _evt({"type": "text", "chunk": mk})
            elif kind == "status":
                yield _evt({"type": "stage_status", "stage": "analyst",
                            "label": payload})
            elif kind == "viz":
                # Блок дизайнера: якоря нумеруются тем же счётчиком, что и
                # текст, поэтому [n] на картинке ведёт на тот же источник.
                html_out, reason = "", payload.get("reason") or ""
                if payload.get("html"):
                    try:
                        html_out = al_viz.finalize(payload["html"], payload.get("logos") or {},
                                                   cite=renum.cite, known=renum.known)
                    except al_viz.VizRejected as e:
                        reason = str(e)
                        log.info("визуализация %s: финал — %s", payload["section"], e)
                yield _evt({"type": "viz", "n": payload["n"],
                            "section": payload["section"],
                            "html": html_out, "reason": reason})
            elif kind == "lead":
                # Резюме и план проверки — целиком, наверх. Перенумеровщик
                # тот же: якоря получат следующие номера, но каждый ведёт на
                # свой источник. finish() сбрасывает придержанный хвост тела
                # ДО подачи резюме, чтобы обрывок якоря не приклеился к нему.
                tail = keys.feed(guard.feed(renum.finish()) + guard.finish()) + keys.finish()
                if tail:
                    body_parts.append(tail)
                    yield _evt({"type": "text", "chunk": tail})
                lead_guard = al_viz.MarkerGuard()
                lead_text = plain_keys(al_viz.restore_lead_markers(
                    lead_guard.feed(renum.feed(payload) + renum.finish()) + lead_guard.finish()))
                yield _evt({"type": "lead", "chunk": lead_text})
    except Exception:
        log.exception("gptr: написание")
        # Написанное НЕ выбрасываем. Одна перегрузка провайдера в середине
        # последнего раздела обнуляла двадцать минут работы: аудитор видел
        # «Отчёт не сформирован» вместо готовых разделов, источников и
        # проверок. Отдаём собранное с честной пометкой и идём дальше —
        # источники и сохранение отчёта отрабатывают как обычно.
        note = ("\n\n⚠ **Отчёт неполный:** написание прервано. "
                "Ниже — разделы, которые успели собраться; "
                "повторите запрос, чтобы получить отчёт целиком.\n")
        if not "".join(body_parts).strip() and not lead_text.strip():
            yield _evt({"type": "text",
                        "chunk": f"\n\n{FAIL_REPORT} Модель прервала ответ — повторите запрос "
                                 f"через минуту.\n"})
            yield _evt({"type": "done"})
            return
        body_parts.append(note)
        yield _evt({"type": "text", "chunk": note})
    rest = keys.feed(guard.feed(renum.finish()) + guard.finish()) + keys.finish()
    if rest:
        body_parts.append(rest)
        yield _evt({"type": "text", "chunk": rest})
    lap("write")
    report = lead_text + "".join(body_parts)
    if not report.strip():
        yield _evt({"type": "text", "chunk":
                    f"\n\n{FAIL_REPORT} Модель вернула пустой ответ — повторите запрос "
                    f"через минуту.\n"})
        yield _evt({"type": "done"})
        return

    # Источники и метрики берём у потокового перенумеровщика: в приложение
    # идут ТОЛЬКО те, на кого реально сослались.
    cited_src, cit_stats = renum.sources(), renum.stats()
    # Ссылки на отзывы приходят из корпуса и в прогоне НИКЕМ не открываются:
    # отзыв, удалённый или перенесённый на banki.ru после сбора, давал в отчёте
    # живую с виду ссылку на 404 (аудиторы сообщали о ссылках на несуществующую
    # страницу. Проверяем ТОЛЬКО процитированные — их единицы.
    corpus_urls = [c["url"] for c in cited_src if c["url"] in review_pages
                   or state.own_meta.get(c["url"], {}).get("kind") == "review"]
    if corpus_urls:
        dead = await asyncio.to_thread(al_reviews.check_alive, corpus_urls)
    else:
        dead = set()
    log.info("цитаты: %s", cit_stats)

    cited_map = {c["url"]: {"facts": c["facts"],
                            "dead": c["url"] in dead,
                            "excerpt": (c["facts"][0]["verbatim"]
                                        if c["facts"] else "")}
                 for c in cited_src}
    # Дата отзыва живёт в метаданных корпуса, дата статьи — в разметке.
    pub_dates = dict(state.page_dates)
    for u, meta in state.review_meta.items():
        if meta.get("date"):
            pub_dates.setdefault(u, str(meta["date"])[:10])
    sources = _sources_ui([c["url"] for c in cited_src], pages, cited_map,
                          pub_dates, dict(state.own_meta), dict(state.kb_meta))
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
    report_plain = al_viz.strip_markers(report)   # маркеры — не утверждения
    verification = al_verify.verify_report(report_plain, registry, pages)
    verification.update({
        "фактов": len(registry.facts),
        "абзацев_без_якоря": al_cit.unanchored_claims(report_plain),
        # якорь, чей источник не содержит ни слова фразы, — к ручной проверке
        "якорь_не_тот": al_cit.anchor_mismatches(report_plain, cited_src),
        **cit_stats,
    })
    gap_lines = al_gaps.collect(plan, registry=registry, attributes=attributes,
                                pages=pages, unreadable=unreadable,
                                cached_copies=dict(state.cached_copies))
    # Снятое критиком — не «ничего не нашлось», а «нашлось, но не подтвердилось».
    # Аудитор обязан видеть разницу.
    gap_lines.extend(verdict.notes)
    # Искали в собственных данных и не нашли — тоже пробел, а не молчание.
    gap_lines.extend(own.notes)
    if dead:
        gap_lines.append(
            f"Ссылок на отзывы, недоступных на момент отчёта: {len(dead)} — "
            f"цитата и дата в силе, страница источника не открывается.")
    tail = al_gaps.render(gap_lines)
    for i in range(0, len(tail), _CHUNK):
        yield _evt({"type": "text", "chunk": tail[i:i + _CHUNK]})

    yield _evt({"type": "verification",
                "method": "numbers_vs_read_pages",
                "numeric_checked": verification["numeric_checked"],
                "verified": verification["verified"],
                # Интерфейс и PDF ждут записи {claim, issue}; голые числа давали
                # «4 утверждения требуют проверки» с пустыми «» (26.09).
                "unverified": ([_unverified_item(x) for x in verification["unverified"]]
                               + verification["якорь_не_тот"]),
                "unverified_count": (len(verification["unverified"])
                                     + len(verification["якорь_не_тот"])),
                "anchor_mismatch": len(verification["якорь_не_тот"]),
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

    lap("verify")
    # Просили презентацию — собираем её по готовому отчёту и отдаём файлом:
    # план слайдов пишет модель, числа с отчётом сверяет код (deck.py).
    if getattr(plan, "deliverable", "") == "deck":
        yield _evt({"type": "stage_status", "stage": "deck",
                    "label": "Рисую презентацию по отчёту",
                    "detail": "макет под этот отчёт — обычно две-три минуты", "estimate_s": 150})
        try:
            from ...ai import agent_files
            data, info = await asyncio.wait_for(al_deck_html.build(
                client, question=question, title=report_name, report=report,
                sources=sources), timeout=420)
            stored = await asyncio.to_thread(
                agent_files.store, al_deck_html.file_name(report_name or question), data)
            unknown = info.get("unknown") or []
            yield _evt({"type": "text", "chunk": (
                f"\n\n## Презентация\n\n{info['slides']} слайдов и список источников, в стиле "
                f"AuditLens. Один файл: открывается в браузере без интернета, листается "
                f"стрелками, F — на весь экран, P — печать в PDF."
                + (" Числа на слайдах сверены с текстом отчёта." if not unknown else
                   f" Сверьте вручную числа, которых нет в отчёте дословно: {', '.join(unknown[:8])}.")
                + f"\n\n{stored['marker']}\n")})
            yield _evt({"type": "files", "ids": [stored["id"]]})
        except Exception as e:  # noqa: BLE001 — отчёт готов и без файла
            log.warning("презентация: не собрана — %s", str(e)[:160])
        lap("deck")
    # Снимок для памяти исследования — служебное событие: веб-слой сохранит его
    # вместе с отчётом и клиенту не отдаст.
    if al_memory.enabled() and registry.facts:
        try:
            yield _evt({"type": al_memory.EVENT,
                        "data": al_memory.dump(registry, state, plan, question, attributes)})
        except Exception as e:  # noqa: BLE001 — память необязательна
            log.info("память исследования: снимок не собран — %s", type(e).__name__)
    yield _evt({"type": "done", "elapsed_s": round(time.time() - started, 1),
                "timings": dict(state.timings), "task": getattr(plan, "task", "")})
