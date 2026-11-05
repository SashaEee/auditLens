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
import os
import re
from urllib.parse import quote
from collections.abc import AsyncIterator
from datetime import datetime

from ..http import SourceHttp
from ..models import CollectorError, NotFound, Review, ReviewPage, SourceBranch, author_ref
from .base import Area, Capabilities, Collector

log = logging.getLogger(__name__)

ORG_URL = "https://yandex.ru/maps/org/{seo}/{oid}/reviews/"
# Без префикса региона страница сети отвечает 404. Префикс — только маршрут сайта:
# какие отделения попадут в выдачу, решает ll (проверено: /213/moscow/ + ll Екатеринбурга
# отдаёт отделения Екатеринбурга).
CHAIN_URL = "https://yandex.ru/maps/213/moscow/chain/sberbank/{chain}/"
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
        edited_at=_dt(x.get("updatedTime")),
        raw={"pinned": x.get("pinned"), "reactions": x.get("reactions"),
             "author_level": au.get("professionLevel")})


def cbr_features(it: dict) -> dict:
    """Данные ЦБ в карточке Яндекса (признаки с пометкой источника aqua_cbr_ru):
    nomer_sp_cbr — номер подразделения по ЦБ («1481/1948/115»), он совпадает с FILIAL_NUM
    реестра ЦБ и даёт точную привязку карточки к ВСП; office_type — для частных лиц,
    для бизнеса, для корпоративных клиентов; devision_type_bank — вид подразделения."""
    out: dict = {}
    for f in it.get("features") or []:
        fid, v = f.get("id"), f.get("value")
        if fid == "nomer_sp_cbr" and isinstance(v, str):
            out["cbr_num"] = v.strip()
        elif fid == "office_type" and isinstance(v, list):
            out["office_type"] = [x.get("id") for x in v if isinstance(x, dict)]
        elif fid == "devision_type_bank" and isinstance(v, list):
            out["division_type"] = [x.get("id") for x in v if isinstance(x, dict)]
        elif fid == "actualization_date_CB" and isinstance(v, str):
            out["cbr_actual"] = v
    return out


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
             "seoname": it.get("seoname"), "categories": [c.get("name") for c in it.get("categories") or []],
             **cbr_features(it)})


API_URL = "https://yandex.ru/maps/api/business/fetchReviews"
RANKINGS = ("by_time", "by_rating_asc", "by_rating_desc", "by_relevance_org")


def djb2x(s: str) -> int:
    """Подпись запроса s — тот же простой хэш, что считает код страницы Яндекс Карт."""
    h = 5381
    for ch in s:
        h = ((h * 33) ^ ord(ch)) & 0xFFFFFFFF
    return h


def signed_query(params: dict) -> str:
    items = sorted(((k, v) for k, v in params.items() if v is not None), key=lambda kv: kv[0].lower())
    q = "&".join(quote(str(k), safe="-_.~") + "=" + quote(str(v), safe="-_.~") for k, v in items)
    return f"{q}&s={djb2x(q)}"


PAGE_CAPS = Capabilities(
    method="page_state", sorts=("by_relevance_org",), full_history=False, max_reachable=600,
    replies=True, notes="страницы 1–12 по 50, по релевантности; >600 — режим VSP_YANDEX_SIGNED_API",
    extra={"newest_first": False})
# Внутренний сервис отзывов (fetchReviews) — с подписью, которую считает код страницы.
# Включается владельцем (VSP_YANDEX_SIGNED_API=1). Даёт «сначала новые» (by_time) и
# объединение четырёх сортировок по 600: у крупного отделения 1 318 из 1 944 (68 %)
# против 600 (31 %) страницами; срезы по аспектам — ещё до 76 % (не используем).
API_CAPS = Capabilities(
    method="internal_api", sorts=("union",), full_history=False, max_reachable=2400,
    replies=True, notes="fetchReviews: 4 сортировки × 600, объединение; by_time — для «только новых»",
    extra={"newest_sort": "by_time", "rankings": RANKINGS})


class YandexMapsCollector(Collector):
    source = "yandex"
    capabilities = PAGE_CAPS

    def __init__(self, settings, http: SourceHttp | None = None, signed: bool | None = None):
        super().__init__(settings)
        self.http = http or SourceHttp("yandex", rps=self.cfg.rps, timeout_s=self.cfg.timeout_s,
                                       max_retries=self.cfg.max_retries, user_agent=settings.user_agent)
        self._seo: dict[str, str] = {}
        self.signed = os.getenv("VSP_YANDEX_SIGNED_API", "0") == "1" if signed is None else signed
        self.capabilities = API_CAPS if self.signed else PAGE_CAPS
        self._sess: dict[str, dict] = {}

    async def close(self) -> None:
        await self.http.close()

    async def _org_page(self, oid: str, page: int) -> dict:
        url = ORG_URL.format(seo=self._seo.get(oid, "sberbank"), oid=oid)
        html = await self.http.get(url, params={"page": page} if page > 1 else None, expect_json=False)
        st = page_state(html)
        it = org_item(st)
        if not it or str(it.get("id")) != str(oid):
            raise NotFound(f"yandex: организации {oid} нет")
        if it.get("seoname"):
            self._seo[oid] = it["seoname"]
        if page == 1:
            cfg = st.get("config") or {}
            self._sess[oid] = {
                "url": url, "csrf": cfg.get("csrfToken"),
                "sessionId": ((cfg.get("counters") or {}).get("analytics") or {}).get("sessionId"),
                "reqId": it.get("requestId") or (st["stack"][0].get("results") or {}).get("requestId")}
        return it

    async def _api_page(self, oid: str, ranking: str, page: int) -> dict:
        """Одна страница fetchReviews (50 отзывов). Обновлённый csrf — повтор один раз."""
        sess = self._sess.get(oid)
        if not sess:
            await self._org_page(oid, 1)
            sess = self._sess[oid]
        for _ in range(2):
            q = signed_query({"ajax": "1", "businessId": oid, "csrfToken": sess["csrf"], "locale": "ru_RU",
                              "page": str(page), "pageSize": "50", "ranking": ranking,
                              "reqId": sess["reqId"], "sessionId": sess["sessionId"]})
            j = await self.http.get(f"{API_URL}?{q}", headers={
                "Accept": "application/json", "Referer": sess["url"], "X-Retpath-Y": sess["url"]})
            if isinstance(j, dict) and "csrfToken" in j and "data" not in j:
                sess["csrf"] = j["csrfToken"]
                continue
            return j if isinstance(j, dict) else {}
        raise CollectorError("yandex: csrf-токен не принят дважды")

    async def branch(self, source_branch_id: str) -> SourceBranch:
        return to_source_branch(await self._org_page(source_branch_id, 1))

    async def pages(self, source_branch_id: str, *, sort: str | None = None,
                    cursor: str | None = None) -> AsyncIterator[ReviewPage]:
        if self.signed:
            async for p in self._api_pages(source_branch_id, sort=sort or "union", cursor=cursor):
                yield p
            return
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

    async def _api_pages(self, oid: str, *, sort: str, cursor: str | None) -> AsyncIterator[ReviewPage]:
        """Сортировки по очереди (union) или одна; курсор «номер_сортировки:страница».
        Дубли между сортировками отбрасываются внутри прохода."""
        ranks = list(RANKINGS) if sort == "union" else [sort]
        ri, page = (int(x) for x in (cursor or "0:1").split(":"))
        seen: set[str] = set()
        await self._org_page(oid, 1)                         # сессия и заявленное число
        n = 0
        while ri < len(ranks):
            j = await self._api_page(oid, ranks[ri], page)
            data = j.get("data") or {}
            items = data.get("reviews") or []
            params = data.get("params") or {}
            if j.get("error") or not items:
                if j.get("error") and page == 1:
                    raise CollectorError(f"yandex: fetchReviews {ranks[ri]}: {j.get('error')}")
                ri, page = ri + 1, 1                         # потолок 600 или конец — к следующей
                continue
            fresh = [x for x in items if str(x.get("reviewId")) not in seen]
            seen.update(str(x.get("reviewId")) for x in items)
            total_pages = min(int(params.get("totalPages") or 0), MAX_PAGES)
            last_of_rank = page >= total_pages
            nri, npage = (ri + 1, 1) if last_of_rank else (ri, page + 1)
            nxt = f"{nri}:{npage}" if nri < len(ranks) else None
            yield ReviewPage([parse_review(x, oid, self.settings.author_salt) for x in fresh], page_no=n,
                             next_cursor=nxt, declared_total=params.get("count"),
                             meta={"ranking": ranks[ri], "page": page, "dupes": len(items) - len(fresh)})
            n += 1
            if nxt is None:
                return
            ri, page = nri, npage

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
