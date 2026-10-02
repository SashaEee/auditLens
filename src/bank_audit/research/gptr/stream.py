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
                cited: dict[str, dict] | None = None,
                dates: dict[str, str] | None = None,
                own: dict[str, dict] | None = None) -> list[dict]:
    """Карточки источников. Если передан cited — только процитированные.
    own — страницы собственных данных AuditLens: адрес ведёт на срез вкладки."""
    cited = cited or {}
    dates = dates or {}
    own = own or {}
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
    # разбор уже прочитанных страниц объектов и собственные данные AuditLens
    # (аналитика жалоб, жалобы из разметки, лазейки). Им ждать сбора незачем.
    registry = al_facts.FactRegistry()
    collecting = {"on": True}
    own_task = asyncio.ensure_future(al_own.collect(client, fast, question, plan,
                                                      state=state))

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

    # Наблюдаемая сторона — прежде всего собственные данные: аналитика жалоб
    # и жалобы из разметки тем же слоем, что вкладка «Отзывы»; веб даёт обзоры.
    runstate.bind(state)
    own = await own_task
    review_pages: dict[str, str] = {}
    if own.complaints or own.loopholes or own.pages:
        sc = own.scope or {}
        got = [f"жалоб {own.complaints}" if sc.get("complaints", True) else "",
               f"лазеек {own.loopholes}" if sc.get("loopholes", True) else "",
               f"предложений рынка {own.market}" if own.market else ""]
        themes = [al_own.T.theme_label(k) for k in sc.get("themes") or []]
        yield _evt({"type": "stage_status", "stage": "reviews",
                    "label": "Данные AuditLens: " + ", ".join(x for x in got if x),
                    "detail": ("срез: " + ", ".join(x for x in (
                        sc.get("product") or "все продукты",
                        f"{sc.get('days')} дн" if sc.get("days") else "",
                        ("темы: " + ", ".join(themes)) if themes else "")
                        if x)),
                    "estimate_s": 0})
    if (not own.complaints and getattr(plan, "subjects", None)
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

    try:
        fu_pages = await asyncio.wait_for(asyncio.shield(followup_task), timeout=_FOLLOWUP_WAIT)
    except Exception as e:  # noqa: BLE001 — дослежка необязательна
        log.info("дослежка: %s", type(e).__name__)
        followup_task.cancel()
        fu_pages = {}
    runstate.bind(state)
    if fu_pages:
        pages.update(fu_pages)
        yield _evt({"type": "stage_status", "stage": "reviews",
                    "label": f"Дослежка сюжета из жалоб: прочитано {len(fu_pages)}",
                    "detail": "событие, продавец, правила — по тому, что называют клиенты",
                    "estimate_s": 0})
    for u in [u for u in pages if _foreign_affiliate(u)]:
        # Сбер Банк Беларусь — другое юрлицо: его адреса и сроки попадали в
        # отчёт как «сведения о Сбере» и порождали мнимые расхождения.
        log.info("исключено: зарубежная дочка/одноимённый банк — %s", u[:90])
        pages.pop(u, None)

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
        # Страницы дослежки — мимо общего отбора и со своей характеристикой:
        # отбор меряет близость к характеристикам продукта («сроки», «основания
        # отказа»), и новость о событии за жалобами ему заведомо проигрывала —
        # прочитанные 8 страниц не дали отчёту ни одного факта (26.09).
        await asyncio.gather(
            al_facts.build_registry(
                client, fast, pages={u: t for u, t in pages.items() if u not in fu_pages},
                attributes=attributes, plan=plan,
                keep_pages=set(review_pages),
                subject_hints=al_reviews.subject_hints(),
                reg=registry, already=eager_pages),
            al_facts.extract_into(
                registry, client, fast, pages=fu_pages,
                attributes=[*attributes, al_followup.EVENT_ATTRIBUTE], plan=plan))

    async for ev in _tick(_extract(), lambda: {
            "type": "progress", "stage": "facts",
            "pages_total": len(pages), "facts": len(registry.facts),
            "ahead": len(eager_pages)}):
        yield ev
    # Собственные данные — после извлечения: их факты собрал код (числа среза,
    # пересказ и сверенная цитата разметки), пересказывать их моделью незачем.
    runstate.bind(state)
    for kw in own.facts:
        registry.add(**kw)
    pages.update(own.pages)
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
                "label": "Написание отчёта",
                "detail": "Аналитик собирает разделы, заказанные планом",
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
    try:
        async for kind, payload in al_dossier.write_dossier(
                client, writer_model, question=question, plan=plan,
                registry=registry, gaps_text=gaps_preview, state=state):
            if kind == "titles":
                _ttl.update(payload)
            elif kind == "outline":
                # Оглавление — после брифа: состав разделов теперь зависит от
                # вопроса, а не от шаблона.
                yield _evt({"type": "outline", "sections": payload})
            elif kind == "section":
                yield _evt({"type": "stage_status", "stage": "analyst",
                            "label": f"Пишу раздел: {_ttl.get(payload, payload)}",
                            "detail": "разделы пишутся вокруг главного ответа"})
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
    except Exception as e:
        log.exception("gptr: написание")
        # Написанное НЕ выбрасываем. Одна перегрузка провайдера в середине
        # последнего раздела обнуляла двадцать минут работы: аудитор видел
        # «Отчёт не сформирован» вместо готовых разделов, источников и
        # проверок. Отдаём собранное с честной пометкой и идём дальше —
        # источники и сохранение отчёта отрабатывают как обычно.
        note = (f"\n\n⚠ **Отчёт неполный:** написание прервано ({e}). "
                f"Ниже — разделы, которые успели собраться; "
                f"повторите запрос, чтобы получить отчёт целиком.\n")
        if not "".join(body_parts).strip() and not lead_text.strip():
            yield _evt({"type": "text",
                        "chunk": f"\n\n⚠ **Отчёт не сформирован:** {e}\n"})
            yield _evt({"type": "done"})
            return
        body_parts.append(note)
        yield _evt({"type": "text", "chunk": note})
    rest = keys.feed(guard.feed(renum.finish()) + guard.finish()) + keys.finish()
    if rest:
        body_parts.append(rest)
        yield _evt({"type": "text", "chunk": rest})
    report = lead_text + "".join(body_parts)
    if not report.strip():
        yield _evt({"type": "text", "chunk":
                    "\n\n⚠ **Отчёт не сформирован:** модель вернула пустой "
                    "ответ. Проверьте совместимость параметров модели.\n"})
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
                          pub_dates, dict(state.own_meta))
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
        **cit_stats,
    })
    gap_lines = al_gaps.collect(plan, registry=registry, attributes=attributes,
                                pages=pages, unreadable=unreadable,
                                cached_copies=dict(state.cached_copies))
    # Снятое критиком — не «ничего не нашлось», а «нашлось, но не подтвердилось».
    # Аудитор обязан видеть разницу.
    gap_lines.extend(verdict.notes)
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
                "unverified": [_unverified_item(x) for x in verification["unverified"]],
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
