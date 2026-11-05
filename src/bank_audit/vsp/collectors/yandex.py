"""Яндекс Карты: отзывы отделений со страниц организаций (данные страницы, без подписи).

    GET https://yandex.ru/maps/org/{seoname}/{id}/reviews/?page=N
      → HTML; в <script class="state-view"> JSON: stack[0].results.items[0] —
        {id, title, address, compositeAddress{locality,street,house}, coordinates[lon,lat],
         ratingData{ratingCount, ratingValue, reviewCount}, chain{id}, status,
         reviewResults{reviews[≤50], params{offset, limit, count, page, totalPages}}}
      отзыв: {reviewId, businessId, text, rating, updatedTime, author{publicId,…},
              businessComment?}

Замер 05.11.2026 (124 запроса, ни одной капчи): по 50 отзывов на страницу, порядок —
«по релевантности» (сортировка в адресе игнорируется), страницы 1–12, дальше пусто:
ПОТОЛОК 600 отзывов на отделение. Отделения до 600 отзывов собираются целиком
(277 из 277, 134 из 134, 57 из 57); у крупного — 600 из 1 944 (31 %).
Внутренний сервис fetchReviews требует подпись запроса, которую считает код страницы;
её мы не воспроизводим (сбор крупных отделений — через браузер, deep_reviews).
Ответов банка на Яндексе Сбербанк практически не даёт (0 из ~1 950 отзывов).

Поиск отделений: сеть «Сбербанк России, отделения» — chain 6003612 (6003611 —
банкоматы, не берём). Страница сети с центром и масштабом отдаёт до 15 ближайших
отделений в данных страницы — без подписи.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from ..http import SourceHttp
from ..models import CollectorError, NotFound, Review, ReviewPage, SourceBranch, author_ref
from .base import Area, Capabilities, Collector

log = logging.getLogger(__name__)

ORG_URL = "https://yandex.ru/maps/org/{seo}/{oid}/reviews/"
CHAIN_URL = "https://yandex.ru/maps/chain/sberbank/{chain}/"
SBER_CHAIN = "6003612"
MAX_PAGES = 12                      # 12 × 50 = 600 — потолок выдачи страниц организации
_STATE = re.compile(r'<script type="application/json" class="state-view">(.*?)</script>', re.S)


def page_state(html: str) -> dict:
    m = _STATE.search(html)
    if not m:
        raise CollectorError("yandex: нет данных страницы (state-view) — изменилась вёрстка?")
    return json.loads(m.group(1))


def org_item(state: dict) -> dict | None:
    try:
        return state["stack"][0]["results"]["items"][0]
    except (KeyError, IndexError, TypeError):
        return None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_review(x: dict, oid: str, salt: str) -> Review:
    bc = x.get("businessComment") or {}
    au = x.get("author") or {}
    return Review(
        source="yandex", source_review_id=str(x["reviewId"]), source_branch_id=oid,
        rating=float(x["rating"]) if x.get("rating") is not None else None,
        review_text=(x.get("text") or None),
        review_date=_dt(x.get("updatedTime")),      # у Яндекса только дата обновления
        author_ref=author_ref("yandex", au.get("publicId"), salt),
        organization_reply=(bc.get("text") or None),
        reply_date=_dt(bc.get("updatedTime")),
        source_url=f"https://yandex.ru/maps/org/sberbank/{oid}/reviews/",
        collection_method="page_state",
        raw={"pinned": x.get("pinned"), "reactions": x.get("reactions"),
             "author_level": au.get("professionLevel")})


def to_source_branch(it: dict) -> SourceBranch:
    ca = it.get("compositeAddress") or {}
    coords = it.get("coordinates") or [None, None]
    rd = it.get("ratingData") or {}
    status = (it.get("status") or "open").lower()
    return SourceBranch(
        source="yandex", source_branch_id=str(it["id"]),
        name=it.get("title") or it.get("shortTitle"),
        address=it.get("fullAddress") or it.get("address"),
        city=ca.get("locality"), region=(it.get("region") or {}).get("names", {}).get("nominative")
        if isinstance(it.get("region"), dict) else None,
        lat=coords[1] if len(coords) > 1 else None, lon=coords[0] if coords else None,
        url=f"https://yandex.ru/maps/org/{it.get('seoname') or 'sberbank'}/{it['id']}/",
        declared_reviews=rd.get("reviewCount"), declared_rating=rd.get("ratingValue"),
        status="closed" if "closed" in status else "open",
        raw={"rating_count": rd.get("ratingCount"), "chain": (it.get("chain") or {}).get("id"),
             "seoname": it.get("seoname"), "categories": [c.get("name") for c in it.get("categories") or []]})


class YandexMapsCollector(Collector):
    source = "yandex"
    capabilities = Capabilities(
        method="page_state", sorts=("by_relevance_org",), full_history=False, max_reachable=600,
        replies=True, notes="страницы 1–12 по 50, по релевантности; >600 — только через браузер",
        extra={"newest_first": False})

    def __init__(self, settings, http: SourceHttp | None = None):
        super().__init__(settings)
        self.http = http or SourceHttp("yandex", rps=self.cfg.rps, timeout_s=self.cfg.timeout_s,
                                       max_retries=self.cfg.max_retries, user_agent=settings.user_agent)
        self._seo: dict[str, str] = {}

    async def close(self) -> None:
        await self.http.close()

    async def _org_page(self, oid: str, page: int) -> dict:
        url = ORG_URL.format(seo=self._seo.get(oid, "sberbank"), oid=oid)
        html = await self.http.get(url, params={"page": page} if page > 1 else None, expect_json=False)
        it = org_item(page_state(html))
        if not it or str(it.get("id")) != str(oid):
            raise NotFound(f"yandex: организации {oid} нет")
        if it.get("seoname"):
            self._seo[oid] = it["seoname"]
        return it

    async def branch(self, source_branch_id: str) -> SourceBranch:
        return to_source_branch(await self._org_page(source_branch_id, 1))

    async def pages(self, source_branch_id: str, *, sort: str | None = None,
                    cursor: str | None = None) -> AsyncIterator[ReviewPage]:
        page = int(cursor or 1)
        while page <= MAX_PAGES:
            it = await self._org_page(source_branch_id, page)
            rr = it.get("reviewResults") or {}
            params = rr.get("params") or {}
            items = rr.get("reviews") or []
            revs = [parse_review(x, source_branch_id, self.settings.author_salt) for x in items]
            total_pages = int(params.get("totalPages") or 0)
            more = items and page < min(total_pages, MAX_PAGES)
            yield ReviewPage(revs, page_no=page, next_cursor=str(page + 1) if more else None,
                             declared_total=params.get("count") or (it.get("ratingData") or {}).get("reviewCount"),
                             meta={"total_pages": total_pages, "capped": total_pages > MAX_PAGES})
            if not more:
                return
            page += 1

    async def discover(self, area: Area) -> AsyncIterator[SourceBranch]:
        """Отделения сети рядом с точкой (до 15 ближайших в данных страницы сети).
        Нужны координаты: для полного обхода — сетка точек по городу или точки реестра."""
        if area.lat is None or area.lon is None:
            raise CollectorError("yandex: для поиска отделений нужны координаты (Area.lat/lon)")
        z = 16 if (area.radius_m or 500) <= 1000 else 13
        html = await self.http.get(CHAIN_URL.format(chain=SBER_CHAIN), expect_json=False,
                                   params={"ll": f"{area.lon:.6f},{area.lat:.6f}", "z": z})
        st = page_state(html)
        items = ((st.get("stack") or [{}])[0].get("results") or {}).get("items") or []
        for it in items:
            if str((it.get("chain") or {}).get("id")) != SBER_CHAIN:
                continue
            yield to_source_branch(it)
