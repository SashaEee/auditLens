"""Дослежка сюжета: по конкретным сущностям из жалоб — событию, продавцу,
площадке, решению регулятора, правилу платёжной системы — несколько точных
поисков в вебе, пока идёт основной сбор.

ЗАЧЕМ. Аудит отчёта 26.09 («почему выросли жалобы на чарджбэк»): 8 из 14
жалоб недели — билеты на концерт на «Газпром Арене»; в самих жалобах —
продавец, дата концерта, заявление арены, предостережение Роспотребнадзора,
ссылки на правила ПС «Мир». Веб-поиск планировался ДО того, как эти данные
видны, и отчёт не нашёл ни заявления арены, ни новостей о концерте: причина
всплеска осталась «шаблонные отказы банка», а 70% жалоб из Петербурга —
необъяснёнными.

Запросы составляет модель по жалобам (это её работа: код не знает, что в
жалобах главное); страницы читает тот же скрапер, что основной сбор, и факты
из них извлекаются тем же слоем — с дословной цитатой и проверкой.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse

from . import runstate

log = logging.getLogger(__name__)

_MAX_QUERIES = int(os.getenv("GPTR_FOLLOWUP_QUERIES", "4"))
# Характеристика, под которую извлекаются факты страниц дослежки.
EVENT_ATTRIBUTE = ("Внешнее событие за жалобами: что произошло, заявления сторон "
                   "(организатор, площадка, продавец, регулятор), даты, правила")
_MAX_PAGES = int(os.getenv("GPTR_FOLLOWUP_PAGES", "8"))
_PAGE_TIMEOUT = float(os.getenv("GPTR_FOLLOWUP_PAGE_TIMEOUT", "25"))
# Отзывы уже пришли из разметки — повторно их не ищем.
_SKIP = re.compile(r"banki\.ru/services/responses|sravni\.ru/.*otzyv|otzovik|irecommend", re.I)

_SYSTEM = """Ты помогаешь аудитору понять, что стоит за жалобами клиентов банка.
По жалобам найди КОНКРЕТНЫЕ сущности, которые могут объяснить сюжет: событие (концерт,
сбой, акция), продавец или организатор, площадка, сервис, решение или предостережение
регулятора, правило платёжной системы, на которое ссылаются клиенты.
Составь до 4 поисковых запросов на русском, чтобы найти в открытых источниках новости,
официальные заявления и правила по этим сущностям. Каждый запрос — с названием и, если
есть, датой. Если конкретных сущностей в жалобах нет — верни пустой список.
Ответ — строго JSON: {"queries": ["...", ...]}"""


def material(pages: dict[str, str], meta: dict[str, dict], limit: int = 9000) -> str:
    """Сюжеты для модели: страницы аналитики (группы похожих) и тексты жалоб."""
    groups = [t for u, t in pages.items() if meta.get(u, {}).get("kind") == "complaints"
              and "Группа похожих" in t]
    reviews = [t for u, t in pages.items() if meta.get(u, {}).get("kind") == "review"]
    out = "\n\n".join(groups)
    for t in reviews:
        if len(out) > limit:
            break
        out += "\n\n---\n" + t[:700]
    return out[:limit]


async def queries_for(client, model: str, question: str, text_: str) -> list[str]:
    from .facts import call_model
    if not text_.strip():
        return []
    kw = {"model": model, "temperature": 0.0, "max_tokens": 400,
          "response_format": {"type": "json_object"},
          "messages": [{"role": "system", "content": _SYSTEM},
                       {"role": "user", "content": f"# Вопрос аудитора\n{question}\n\n"
                                                   f"# Жалобы\n{text_}"}],
          "extra_body": {"thinking": {"type": "disabled"}}}
    try:
        resp = await call_model(client, model, kw)
        data = json.loads(re.search(r"\{.*\}", resp.choices[0].message.content or "",
                                    re.S).group(0))
    except Exception as e:  # noqa: BLE001 — дослежка необязательна
        log.info("дослежка: запросы не составлены — %s", type(e).__name__)
        return []
    qs = [re.sub(r"\s+", " ", str(q)).strip() for q in data.get("queries") or []]
    return [q for q in dict.fromkeys(qs) if 8 <= len(q) <= 160][:_MAX_QUERIES]


async def collect(client, model: str, question: str, own, state=None) -> dict[str, str]:
    """Страницы дослежки: url → текст. Пусто, если в жалобах нет конкретики."""
    from ...rag.web_search import search
    from .scraper import AuditLensScraper
    state = state or runstate.current()
    queries = await queries_for(client, model, question, material(own.pages, own.meta))
    if not queries:
        return {}
    log.info("дослежка: %s", queries)
    found = await asyncio.gather(*(asyncio.to_thread(
        search, q, max_results=4, caller="deep-followup") for q in queries),
        return_exceptions=True)
    urls: list[str] = []
    for res in found:
        if isinstance(res, Exception):
            continue
        for r in res or []:
            u = (r.get("url") or "").strip()
            if (u.startswith("http") and u not in urls and u not in state.pages
                    and not _SKIP.search(u)):
                urls.append(u)
    # Не больше двух страниц с одного сайта: иначе один агрегатор съедает бюджет.
    per_host: dict[str, int] = {}
    picked = []
    for u in urls:
        h = urlparse(u).netloc
        if per_host.get(h, 0) >= 2:
            continue
        per_host[h] = per_host.get(h, 0) + 1
        picked.append(u)
        if len(picked) >= _MAX_PAGES:
            break

    async def read(u: str) -> tuple[str, str]:
        try:
            text_, _links, _title = await asyncio.wait_for(asyncio.to_thread(
                AuditLensScraper(u, state=state).scrape), _PAGE_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.info("дослежка %s: %s", u[:80], type(e).__name__)
            return u, ""
        return u, text_ if u in state.pages and u not in state.unreadable else ""

    pages = {u: t for u, t in await asyncio.gather(*(read(u) for u in picked)) if t}
    log.info("дослежка: запросов %d, ссылок %d, прочитано %d", len(queries), len(picked),
             len(pages))
    return pages
