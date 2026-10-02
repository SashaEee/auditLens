"""Ретривер gpt-researcher поверх нашей цепочки веб-поиска.

Штатный searx-ретривер gpt-researcher делает GET к открытому инстансу. Мы
идём через rag/web_search.search(): Яндекс через корпоративный шлюз, затем
шлюз fleet-searxng на резидентских прокси, затем остальные запасные.

Раньше здесь был прямой вызов одного fleet-шлюза без запасного пути: любой
его сбой (401, квота, таймаут) или пустой ответ давал отчёт без веб-источников,
а 22.09.2026 шлюз трижды отклонил токен. Теперь у отчёта два живых поисковика,
кэш выдачи и широкий повтор, если по заданным сайтам ничего не нашлось.

Контракт возврата тот же, что у их ретриверов: [{"href": ..., "body": ...}].
"""
from __future__ import annotations

import logging
import re

from ..v2.tools.web_tools import _trust_for

log = logging.getLogger(__name__)

# Ниже этого доверия источник не читаем никогда: форумы, доски объявлений,
# офтоп-поддомены. Проверено: pikabu.ru = 0.20.
_HARD_MIN = 0.35
# Первоисточник (сайт организации, регулятор) — по нашей же шкале.
_PRIMARY = 0.85
# Сколько непервоисточных оставляем, когда первоисточник найден: они несут
# наблюдаемую сторону (жалобы, разборы), без которой аудит однобок.
_KEEP_OTHER = 3


def _prefer_primary(results: list[dict]) -> list[dict]:
    """Отсев мусора с СОХРАНЕНИЕМ обеих сторон доказательства.

    Прошлая версия при наличии первоисточника выбрасывала всё остальное — и
    вместе с SEO-блогами уносила жалобы клиентов и сторонние разборы. В отчёте
    31.08 это дало «взгляд только со слов банков»: три собранные жалобы не
    дожили даже до писателя.

    Теперь: мусор (форумы, доски объявлений) выбрасываем всегда, а из
    непервоисточных оставляем несколько лучших — они несут наблюдаемую
    сторону, которой на сайте банка по определению нет. Списка «плохих
    доменов» по-прежнему нет: решают оценка доверия и состав выдачи.
    """
    from urllib.parse import urlparse
    scored = []
    for r in results:
        url = r.get("href") or ""
        dom = urlparse(url).netloc.removeprefix("www.")
        scored.append((_trust_for(dom, url), r))
    kept = [(t, r) for t, r in scored if t >= _HARD_MIN]
    primary = [(t, r) for t, r in kept if t >= _PRIMARY]
    other = sorted([(t, r) for t, r in kept if t < _PRIMARY],
                   key=lambda p: -p[0])
    if primary:
        other = other[:_KEEP_OTHER]   # оставляем взгляд со стороны, но немного
    kept = primary + other
    dropped = len(results) - len(kept)
    if dropped:
        log.info("fleet: отсеяно %d из %d (первоисточников %d, со стороны %d)",
                 dropped, len(results), len(primary), len(other))
    kept.sort(key=lambda p: -p[0])
    return [r for _t, r in kept]


class WebSearch:
    """Совместимый с gpt-researcher поиск через нашу цепочку бэкендов."""

    def __init__(self, query: str, query_domains=None):
        # `site:` вырезаем из текста и передаём списком: цепочка сама решает,
        # как объяснить домены каждому поисковику (Яндексу и fleet — оператором
        # в тексте). Фильтр действует только на ТОТ запрос, где site: стоял —
        # иначе общий сравнительный запрос тоже запирается на сайты банков и
        # отчёт остаётся без обзоров и жалоб (замер 31.08.2026: контекст упал
        # с 24 800 до 4 600 символов).
        sites = re.findall(r"site:(\S+)", query, flags=re.IGNORECASE)
        clean = re.sub(r"site:\S+", " ", query, flags=re.IGNORECASE)
        self.query = re.sub(r"\s+", " ", clean).strip() or query
        doms: list[str] = []
        for d in list(sites) + list(query_domains or []):
            h = d.split("/")[0].lower().removeprefix("www.")
            if h and h not in doms:
                doms.append(h)
        self.query_domains = doms

    def search(self, max_results: int = 10) -> list[dict]:
        from ...rag import web_search
        want = max(1, int(max_results or 8)) * 2          # с запасом — часть отсеется
        res = web_search.search(self.query, max_results=want,
                                site_filter=self.query_domains or None,
                                caller="deep_research")
        if not res and self.query_domains:
            # По сайтам пусто у всех поисковиков — лучше взгляд со стороны, чем
            # ничего. Чужие домены потом всё равно проходят оценку доверия.
            log.info("поиск: по %s пусто, широкий повтор без доменов",
                     ", ".join(self.query_domains[:3]))
            res = web_search.search(self.query, max_results=want,
                                    caller="deep_research_broad")
        out = [{"href": r["url"], "body": r.get("snippet") or ""}
               for r in res if r.get("url")]
        return _prefer_primary(out)[:max_results]


# Старое имя: так класс знают движок и тесты.
FleetSearch = WebSearch
