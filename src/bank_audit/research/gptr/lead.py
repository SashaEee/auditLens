"""Руководитель исследования: хватает ли собранного для ответа, и добор.

ЗАЧЕМ. Отчёт шёл конвейером в один проход: спланировал, собрал, написал из
того, что нашлось. Аудит 11.11: 42 % резюме начинались с «нельзя», «не
найдено», «нет данных» — писатель честно сообщал о пробеле, который никто не
пытался закрыть. Человек-аналитик поступает иначе: смотрит, что собрано,
видит, какого числа или документа не хватает, и ищет именно его.

Здесь это и происходит — один раз, после первого сбора:
  1. assess — сильная модель видит вопрос и указатель фактов (с началом цитат)
     и отвечает: достаточно ли для ответа по существу; если нет — чего именно
     не хватает и какими НОВЫМИ запросами это искать;
  2. fill — до нескольких запросов, до десятка страниц, факты из них выписывает
     тот же слой, что и из остальных.
Пишет отчёт уже писатель; здесь ничего не сочиняется и не считается.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import brief as al_brief
from . import facts as al_facts
from . import runstate

log = logging.getLogger(__name__)

MAX_QUERIES = int(os.getenv("GPTR_LEAD_QUERIES", "4"))
MAX_PAGES = int(os.getenv("GPTR_LEAD_PAGES", "8"))
PER_QUERY = 3
READ_TIMEOUT = float(os.getenv("GPTR_LEAD_READ_S", "25"))
# Предел на выписку фактов из добранных страниц: добор — доводка, отчёт его не ждёт
# дольше минуты. Что успели выписать к этому моменту — остаётся в реестре.
EXTRACT_TIMEOUT = float(os.getenv("GPTR_LEAD_EXTRACT_S", "45"))
_INDEX_LINES = 220


def enabled() -> bool:
    return os.getenv("GPTR_LEAD", "1").lower() in ("1", "true", "yes")


def model_for() -> str:
    return (os.getenv("GPTR_LEAD_MODEL") or os.getenv("GPTR_PLAN_MODEL")
            or os.getenv("LLM_MODEL_REASONING") or os.environ.get("LLM_MODEL_NAME", ""))


@dataclass
class Need:
    """Что решил руководитель после первого сбора."""
    enough: bool = True
    missing: str = ""                                   # чего не хватает — одной фразой
    queries: list[dict] = field(default_factory=list)   # {"q": …, "site": …}


@dataclass
class Fill:
    queries: int = 0
    pages: int = 0
    facts: int = 0
    urls: list[str] = field(default_factory=list)


_SYSTEM = """Ты — руководитель исследования в службе внутреннего аудита Сбербанка. Группа собрала
факты по вопросу аудитора. Реши: хватает ли их, чтобы ответить ПО СУЩЕСТВУ, или главного не
нашли и нужно поискать ещё.

Верни строго JSON:
{"enough": true|false, "missing": "чего не хватает — одной фразой", "queries": [{"q": "...", "site": ""}]}

Как решать:
  • Смотри на суть вопроса: что аудитор хочет узнать. Если на это есть прямой ответ в фактах
    (нужное число с датой, условие, документ, перечень) — "enough": true, queries пустой.
    Мелкие пробелы не повод искать дальше: добор стоит времени.
  • Проверяй, О ЧЁМ факт: после стороны стоит начало цитаты. Число про другой предмет ответом
    не считается, как бы ни называлась его характеристика.
  • "enough": false — только когда не найдено ГЛАВНОЕ: ключевое число или ряд, сам документ
    регулятора, условия продукта, о котором спрашивают, данные по названной организации.
  • Тогда дай 2–4 НОВЫХ поисковых запроса — не повторяй уже сделанные (список ниже) и не
    перефразируй их. Ищи иначе: другое название показателя, первичный источник (форма
    отчётности, статистический бюллетень, годовой отчёт, пресс-релиз, тарифный сборник),
    другой сайт. "site" — домен, если знаешь, где это лежит (cbr.ru, consultant.ru,
    nalog.gov.ru, сайт банка, frankrg.com, raexpert.ru); иначе "".
  • Страницы из списка «не прочитано» повторно не запрашивай.
Без пояснений вне JSON."""


def _digest(registry, labels: dict[str, str], own: dict) -> str:
    return al_brief.facts_digest(list(registry.facts), labels, own, limit=_INDEX_LINES)


def parse(raw: str, done: list[str]) -> Need:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or data.get("enough") is not False:
        return Need()
    seen = {re.sub(r"\s+", " ", re.sub(r"site:\S+", "", q)).strip().lower() for q in done}
    queries: list[dict] = []
    for it in data.get("queries") or []:
        if isinstance(it, str):
            it = {"q": it}
        if not isinstance(it, dict):
            continue
        q = re.sub(r"\s+", " ", re.sub(r"\bsite:\S+", "", str(it.get("q") or ""))).strip()[:200]
        site = re.sub(r"/.*$", "", re.sub(r"^(https?://)?(www\.)?", "",
                                          str(it.get("site") or "").strip().lower()))
        if not re.fullmatch(r"[a-z0-9а-яё.-]+\.[a-zа-яё]{2,}", site or ""):
            site = ""
        if len(q) < 6 or q.lower() in seen:
            continue
        seen.add(q.lower())
        queries.append({"q": q, "site": site})
        if len(queries) >= MAX_QUERIES:
            break
    missing = re.sub(r"\s+", " ", str(data.get("missing") or "")).strip()[:240]
    return Need(enough=not queries, missing=missing, queries=queries)


async def assess(client, model: str, *, question: str, plan, registry, state=None) -> Need:
    """Хватает ли собранного. Любой сбой — «хватает»: отчёт пишется как раньше."""
    state = state or runstate.current()
    labels = dict(getattr(plan, "subject_labels", None) or {})
    attrs_text, _ = al_brief.attributes_digest(list(registry.facts))
    done = list(state.subqueries)
    unread = list(state.unreadable)[:12]
    from ...clock import today_msk
    user = "\n".join([
        f"# Сегодня\n{today_msk().strftime('%d.%m.%Y')}",
        f"# Вопрос аудитора\n{question}",
        f"\n# Что хочет узнать\n{getattr(plan, 'intent_summary', '') or '—'}",
        f"\n# Что собрано: характеристики (число фактов)\n{attrs_text or '— ничего'}",
        "\n# Уже сделанные запросы\n" + ("\n".join(f"- {q}" for q in done[:20]) or "—"),
        "\n# Не прочитано\n" + ("\n".join(f"- {u}" for u in unread) or "—"),
        "\n# Факты",
        _digest(registry, labels, state.own_meta) or "— фактов нет",
    ])
    kw = {"model": model, "temperature": 0.0, "max_tokens": 1500,
          "messages": [{"role": "system", "content": _SYSTEM},
                       {"role": "user", "content": user}],
          "extra_body": {"reasoning_effort": os.getenv("GPTR_LEAD_REASONING_EFFORT", "low")}}
    try:
        resp = await asyncio.wait_for(al_facts.call_model(client, model, kw),
                                      float(os.getenv("GPTR_LEAD_ASSESS_S", "35")))
        need = parse(resp.choices[0].message.content or "", done)
    except Exception as e:  # noqa: BLE001 — добор необязателен
        log.info("руководитель: %s — пишем по собранному", type(e).__name__)
        return Need()
    log.info("руководитель: %s%s", "достаточно" if need.enough else f"не хватает — {need.missing}",
             "" if need.enough else f"; запросов {len(need.queries)}")
    return need


def _search(query: str, site: str) -> list[str]:
    from .retriever import WebSearch
    q = f"{query} site:{site}" if site else query
    try:
        return [r["href"] for r in WebSearch(q).search(max_results=6) if r.get("href")]
    except Exception as e:  # noqa: BLE001
        log.info("добор, поиск «%s»: %s", query[:60], type(e).__name__)
        return []


def _read(url: str, state) -> str:
    from .scraper import AuditLensScraper
    try:
        return AuditLensScraper(url, state=state).scrape()[0] or ""
    except Exception as e:  # noqa: BLE001
        log.info("добор, чтение %s: %s", url[:70], type(e).__name__)
        return ""


async def fill(client, model: str, need: Need, *, plan, registry, attributes, state=None) -> Fill:
    """Прицельный добор: новые запросы → новые страницы → факты в общий реестр."""
    state = state or runstate.current()
    out = Fill(queries=len(need.queries))
    if not need.queries:
        return out
    found = await asyncio.gather(*(asyncio.to_thread(_search, q["q"], q["site"])
                                   for q in need.queries))
    known = set(state.pages) | set(state.unreadable)
    picked: list[str] = []
    hosts: dict[str, int] = {}
    for urls in found:                       # по очереди от каждого запроса
        took = 0
        for u in urls:
            host = urlparse(u).netloc
            if u in known or u in picked or hosts.get(host, 0) >= 3:
                continue
            picked.append(u)
            hosts[host] = hosts.get(host, 0) + 1
            took += 1
            if took >= PER_QUERY:
                break
    picked = picked[:MAX_PAGES]
    if not picked:
        return out
    state.subqueries.extend(q["q"] + (f" site:{q['site']}" if q["site"] else "")
                            for q in need.queries)
    tasks = [asyncio.ensure_future(asyncio.to_thread(_read, u, state)) for u in picked]
    done, pending = await asyncio.wait(tasks, timeout=READ_TIMEOUT)
    for t in pending:
        t.cancel()
    pages: dict[str, str] = {}
    for u, t in zip(picked, tasks):
        if t in done and not t.cancelled() and t.exception() is None:
            text = t.result()
            if len(text) >= 400 and u not in state.unreadable:
                pages[u] = text
    out.pages, out.urls = len(pages), list(pages)
    if not pages:
        return out
    before = len(registry.facts)
    runstate.bind(state)

    async def one(url: str, text: str) -> None:
        # По странице на вызов: медленная страница не отнимает факты у быстрых.
        try:
            await asyncio.wait_for(
                al_facts.extract_into(registry, client, model, pages={url: text},
                                      attributes=list(attributes), plan=plan),
                EXTRACT_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("добор: выписка из %s не уложилась в %.0f с", url[:70], EXTRACT_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — добор необязателен
            log.info("добор: выписка из %s — %s", url[:70], type(e).__name__)

    await asyncio.gather(*(one(u, t) for u, t in pages.items()))
    out.facts = len(registry.facts) - before
    log.info("добор: запросов %d, страниц %d, фактов +%d", out.queries, out.pages, out.facts)
    return out
