"""2ГИС: отзывы отделений через публичный сервис отзывов, которым пользуется сам 2gis.ru.

    GET https://public-api.reviews.2gis.com/3.0/branches/{branch_id}/reviews
        ?limit=50&offset=N&sort_by=date_edited&locale=ru_RU&key=<reviewApiKey>
        &fields=meta.providers,meta.branch_rating,meta.branch_reviews_count,
                meta.total_count,reviews.hiding_reason
      → {meta: {branch_rating, branch_reviews_count, total_count?, next_link, providers},
         reviews: [{id, rating, text, date_created, date_edited, official_answer,
                    provider: 2gis|flamp|…, is_hidden, hiding_reason, user: {...}}]}

Ключ — тот, что 2gis.ru отдаёт любому посетителю в настройках страницы (reviewApiKey);
берём его со страницы при запуске, не храним в коде (VSP_2GIS_REVIEW_KEY — ручная
замена). Пагинация — offset до конца (next_link пропадает): замер 05.11.2026 —
заявлено 331, получено 331, с 2021 года; полная история доступна.

Автор: хранится только ссылка (хэш public_id). Число отзывов автора — в raw:
по нему видно «одноразовых» авторов (проверка всплесков положительных отзывов).
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from ..http import SourceHttp
from ..models import CollectorError, NotFound, Review, ReviewPage, SourceBranch, author_ref
from .base import Area, Capabilities, Collector

log = logging.getLogger(__name__)

REVIEWS_URL = "https://public-api.reviews.2gis.com/3.0/branches/{bid}/reviews"
FIELDS = ("meta.providers,meta.branch_rating,meta.branch_reviews_count,meta.total_count,"
          "reviews.hiding_reason,reviews.trust_factors")
FIRST_PAGE_TTL = 60.0                 # с: первая страница из branch() годится для pages()
SBER_ORG_ID = "4504136498537001"     # организация «Сбербанк» в каталоге 2ГИС


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_review(x: dict, bid: str, salt: str) -> Review:
    ans = x.get("official_answer") or {}
    user = x.get("user") or {}
    return Review(
        source="2gis", source_review_id=str(x["id"]), source_branch_id=bid,
        rating=float(x["rating"]) if x.get("rating") is not None else None,
        review_text=(x.get("text") or None),
        review_date=_dt(x.get("date_created")),
        author_ref=author_ref("2gis", user.get("public_id") or user.get("id"), salt),
        organization_reply=(ans.get("text") or None),
        reply_date=_dt(ans.get("date_created")),
        source_url=f"https://2gis.ru/firm/{bid}/tab/reviews",
        collection_method="public_api",
        edited_at=_dt(x.get("date_edited")) or _dt(x.get("date_created")),
        raw={"provider": x.get("provider"), "date_edited": x.get("date_edited"),
             "is_hidden": x.get("is_hidden"), "hiding_reason": x.get("hiding_reason"),
             "likes": x.get("likes_count"), "comments": x.get("comments_count"),
             "author_reviews_count": user.get("reviews_count"),
             "is_rated": x.get("is_rated"),
             # «Отзыв подтверждён»: операция подтверждена провайдером (sbol — СберБанк Онлайн)
             "confirmed_by": _confirmed_by(x.get("trust_factors"))})


def _confirmed_by(tf) -> list[str] | None:
    if not isinstance(tf, dict):
        return None
    out = sorted({f.get("transaction_provider") or f.get("type") for f in tf.get("factors") or []
                  if isinstance(f, dict) and (f.get("transaction_provider") or f.get("type"))})
    return out or None


class TwoGisCollector(Collector):
    source = "2gis"
    capabilities = Capabilities(
        method="public_api", sorts=("date_edited", "date_created", "friends"), full_history=True,
        notes="offset до конца выдачи; провайдеры 2gis и flamp в одной ленте")

    def __init__(self, settings, http: SourceHttp | None = None):
        super().__init__(settings)
        self._first: dict[tuple[str, str], tuple[float, dict]] = {}
        self.http = http or SourceHttp("2gis", rps=self.cfg.rps, timeout_s=self.cfg.timeout_s,
                                       max_retries=self.cfg.max_retries, user_agent=settings.user_agent,
                                       headers={"Referer": "https://2gis.ru/", "Origin": "https://2gis.ru"})
        self._key: str | None = None

    async def close(self) -> None:
        await self.http.close()

    async def review_key(self) -> str:
        """reviewApiKey со страницы 2gis.ru (то, что получает любой посетитель)."""
        import os
        if self._key:
            return self._key
        self._key = os.getenv("VSP_2GIS_REVIEW_KEY")
        if not self._key:
            html = await self.http.get("https://2gis.ru/moscow", expect_json=False)
            m = re.search(r'"reviewApiKey":"([0-9a-f-]{36})"', html)
            if not m:
                # /museum — заглушка «устаревший браузер»: обновите VSP_USER_AGENT
                raise CollectorError("2gis: не нашёл reviewApiKey в настройках страницы "
                                     "(страница-заглушка? проверьте VSP_USER_AGENT)")
            self._key = m.group(1)
        return self._key

    async def _page(self, bid: str, *, sort: str, offset: int, limit: int) -> dict:
        key = await self.review_key()
        return await self.http.get(REVIEWS_URL.format(bid=bid), params={
            "limit": limit, "offset": offset, "sort_by": sort, "locale": "ru_RU",
            "fields": FIELDS, "key": key})

    async def branch(self, source_branch_id: str) -> SourceBranch:
        # карточка = meta первой страницы; страница кладётся в кэш, и pages() её не перезапрашивает
        sort = self.capabilities.sorts[0]
        d = await self._page(source_branch_id, sort=sort, offset=0, limit=min(self.cfg.page_size, 50))
        self._first[(source_branch_id, sort)] = (time.monotonic(), d)
        meta = d.get("meta") or {}
        if meta.get("code") and int(meta["code"]) == 404:
            raise NotFound(f"2gis: отделения {source_branch_id} нет")
        return SourceBranch("2gis", source_branch_id,
                            url=f"https://2gis.ru/firm/{source_branch_id}",
                            declared_reviews=meta.get("branch_reviews_count"),
                            declared_rating=meta.get("branch_rating"),
                            raw={"providers": meta.get("providers"), "total_count": meta.get("total_count")})

    async def pages(self, source_branch_id: str, *, sort: str | None = None,
                    cursor: str | None = None) -> AsyncIterator[ReviewPage]:
        sort = sort or self.capabilities.sorts[0]
        limit = min(self.cfg.page_size, 50)
        offset = int(cursor or 0)
        n = 0
        while True:
            hit = self._first.pop((source_branch_id, sort), None) if offset == 0 else None
            if hit and time.monotonic() - hit[0] < FIRST_PAGE_TTL:
                d = hit[1]
            else:
                d = await self._page(source_branch_id, sort=sort, offset=offset, limit=limit)
            meta = d.get("meta") or {}
            items = d.get("reviews") or []
            revs = [parse_review(x, source_branch_id, self.settings.author_salt) for x in items]
            nxt = meta.get("next_link")
            next_offset = None
            if nxt and items:
                q = parse_qs(urlparse(nxt).query)
                next_offset = int(q.get("offset", [offset + len(items)])[0])
            yield ReviewPage(revs, page_no=n, next_cursor=str(next_offset) if next_offset is not None else None,
                             declared_total=meta.get("branch_reviews_count"),
                             meta={"total_count": meta.get("total_count")})
            if next_offset is None or next_offset <= offset:
                return
            offset, n = next_offset, n + 1

    # ── поиск отделений ─────────────────────────────────────────────────────
    # Каталог 2ГИС (catalog.api) подписан — закрыт, подпись не воспроизводим. Отделения
    # берём со страниц поиска, которые 2gis.ru отдаёт любому посетителю:
    #   https://2gis.ru/{alias}/search/СберБанк/page/{N} — по 12 объектов, в данных страницы
    #   у каждого: id, адрес, координаты, организация, число отзывов с текстом и оценок.
    # Банкоматы — отдельные организации («СберБанк, банкомат») — отбрасываются.
    async def settlements(self) -> list[dict]:
        """Населённые пункты, встроенные в страницу 2gis.ru: {id, region_id, segment_id,
        name, alias}. Замер 05.11.2026: 1 111 пунктов России."""
        if getattr(self, "_settl", None) is None:
            html = await self.http.get("https://2gis.ru/moscow", expect_json=False)
            seen, out = set(), []
            for m in re.finditer(r'\{"id":"(\d+)","region_id":"(\d+)","segment_id":"(\d+)","name":"([^"]+)",'
                                 r'"(?:country_code|cc)":"ru","alias":"([a-z0-9_-]+)"', html):
                if m.group(5) not in seen:
                    seen.add(m.group(5))
                    out.append({"id": m.group(1), "region_id": m.group(2), "segment_id": m.group(3),
                                "name": m.group(4), "alias": m.group(5)})
            self._settl = out
        return self._settl

    async def alias_for(self, area: Area) -> str:
        city = area.city.strip()
        if re.fullmatch(r"[a-z0-9_-]+", city):
            return city
        want = city.lower().replace("ё", "е")
        for x in await self.settlements():
            if x["name"].lower().replace("ё", "е") == want:
                return x["alias"]
        raise NotFound(f"2gis: населённый пункт «{area.city}» не найден среди городов 2ГИС")

    async def discover(self, area: Area) -> AsyncIterator[SourceBranch]:
        alias = await self.alias_for(area)
        seen: set[str] = set()
        for n in range(1, 301):
            try:
                html = await self.http.get(f"https://2gis.ru/{alias}/search/СберБанк/page/{n}",
                                           expect_json=False)
            except NotFound:
                return
            items = [b for b in ssr_branches(html) if b["id"] not in seen]
            if not items:
                return
            for b in items:
                seen.add(b["id"])
                if is_sber_office(b):
                    yield to_source_branch(b, alias)


def ssr_branches(html: str) -> list[dict]:
    """Объекты-отделения из данных страницы 2gis.ru (JSON внутри HTML)."""
    out, seen = [], set()
    for m in re.finditer(r'"(\d{14,20})":\{"data":\{', html):
        start = m.end() - 1
        depth, i, instr, esc = 0, start, False, False
        while i < len(html):
            ch = html[i]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        try:
            d = json.loads(html[start:i + 1])
        except ValueError:
            continue
        if d.get("type") != "branch" or str(d.get("id")) in seen:
            continue
        seen.add(str(d.get("id")))
        out.append(d)
    return out


def is_sber_office(d: dict) -> bool:
    """Отделение Сбербанка, а не банкомат/терминал и не дочерняя компания."""
    org = (d.get("org") or {}).get("name") or ""
    name = d.get("name") or ""
    rubrics = {r.get("name") for r in d.get("rubrics") or []}
    # решает организация карточки; название — только если организации нет
    if not (org.startswith("СберБанк") if org else name.startswith("СберБанк")):
        return False
    if re.search(r"банкомат|терминал|инфокиоск", org + " " + name, re.I):
        return False
    return "Банки" in rubrics or not rubrics


def to_source_branch(d: dict, alias: str) -> SourceBranch:
    adm = {a.get("type"): a for a in d.get("adm_div") or []}
    pt = d.get("point") or {}
    rv = d.get("reviews") or {}
    org = d.get("org") or {}
    ext = (d.get("name_ex") or {}).get("extension")
    return SourceBranch(
        source="2gis", source_branch_id=str(d["id"]),
        name=d.get("name") if not ext or ext.lower() in (d.get("name") or "").lower()
        else f"{d.get('name')}, {ext}",
        address=d.get("address_name"),
        city=(adm.get("city") or adm.get("settlement") or {}).get("name"),
        region=(adm.get("region") or {}).get("name"),
        lat=pt.get("lat"), lon=pt.get("lon"),
        url=f"https://2gis.ru/{alias}/firm/{d['id']}",
        declared_reviews=rv.get("general_review_count"),
        declared_rating=rv.get("general_rating"),
        status="open",
        raw={"org_id": org.get("id"), "org_name": org.get("name"), "org_branch_count": org.get("branch_count"),
             "ratings_with_stars": rv.get("general_review_count_with_stars"), "alias": alias,
             "rubrics": [r.get("name") for r in d.get("rubrics") or []]})
