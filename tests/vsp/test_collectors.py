"""Адаптеры 2ГИС и Яндекса на подменённых ответах (без сети, синтетические данные)."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from bank_audit.vsp.collectors.twogis import TwoGisCollector, is_sber_office, ssr_branches, to_source_branch
from bank_audit.vsp.collectors.yandex import YandexMapsCollector, djb2x, signed_query
from bank_audit.vsp.config import Settings
from bank_audit.vsp.http import SourceHttp
from bank_audit.vsp.models import SourceBlocked


def http_for(handler, source="t"):
    return SourceHttp(source, rps=0, timeout_s=5, max_retries=0, user_agent="test",
                      client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def collect(gen):
    return [p async for p in gen]


# ── 2ГИС ─────────────────────────────────────────────────────────────────────

def _2gis_review(i, answer=False):
    return {"id": str(1000 + i), "rating": 5 - i % 5, "text": f"текст {i}",
            "date_created": f"2025-0{1 + i % 9}-01T10:00:00+03:00", "date_edited": None,
            "official_answer": {"text": "Спасибо", "date_created": "2025-02-01T10:00:00+03:00"} if answer else None,
            "provider": "2gis", "is_hidden": False, "likes_count": 0, "comments_count": 0,
            "user": {"public_id": f"u{i}", "name": "Автор", "reviews_count": 1}}


def test_twogis_pages_until_next_link_ends():
    total = 120

    def handler(req: httpx.Request):
        if req.url.host == "2gis.ru":
            return httpx.Response(200, text='<script>var __customcfg={"reviewApiKey":"00000000-0000-0000-0000-000000000000"}</script>')
        q = parse_qs(urlparse(str(req.url)).query)
        off, lim = int(q["offset"][0]), int(q["limit"][0])
        items = [_2gis_review(i, answer=i == 3) for i in range(off, min(off + lim, total))]
        nxt = f"https://x/?offset={off + lim}" if off + lim < total else None
        return httpx.Response(200, json={"meta": {"branch_reviews_count": total, "next_link": nxt},
                                         "reviews": items})
    c = TwoGisCollector(Settings(), http=http_for(handler, "2gis"))
    pages = asyncio.run(collect(c.pages("7000")))
    revs = [r for p in pages for r in p.reviews]
    assert len(pages) == 3 and len(revs) == total and pages[-1].next_cursor is None
    r3 = next(r for r in revs if r.source_review_id == "1003")
    assert r3.organization_reply == "Спасибо" and r3.reply_date is not None
    assert r3.author_ref and "Автор" not in json.dumps(r3.raw, ensure_ascii=False)
    assert r3.raw["author_reviews_count"] == 1
    card = asyncio.run(c.branch("7000"))
    assert card.declared_reviews == total


def test_twogis_ssr_parsing_and_atm_filter():
    def obj(i, org, rubric):
        return {"id": str(70000001000000000 + i), "type": "branch", "name": "СберБанк",
                "address_name": f"улица Ленина, {i}", "point": {"lat": 55.0 + i / 1000, "lon": 37.0},
                "org": {"id": "1", "name": org, "branch_count": 3},
                "reviews": {"general_rating": 4.5, "general_review_count": 10 * i,
                            "general_review_count_with_stars": 20 * i},
                "adm_div": [{"type": "region", "name": "Владимирская область"}, {"type": "city", "name": "Муром"}],
                "rubrics": [{"name": rubric}]}
    data = {f"{70000001000000000 + i}": {"data": obj(i, o, r), "meta": {}}
            for i, o, r in ((1, "СберБанк, ПАО СберБанк", "Банки"), (2, "СберБанк, банкомат", "Банкоматы"),
                            (3, "ВТБ", "Банки"))}
    html = ("<html><script>window.__state=" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            + ";</script></html>")
    items = ssr_branches(html)
    assert len(items) == 3
    offices = [b for b in items if is_sber_office(b)]
    assert [b["id"] for b in offices] == ["70000001000000001"]
    sb = to_source_branch(offices[0], "murom")
    assert (sb.city, sb.region, sb.declared_reviews, sb.raw["ratings_with_stars"]) == \
        ("Муром", "Владимирская область", 10, 20)


# ── Яндекс ───────────────────────────────────────────────────────────────────

def _ya_state(oid, page, count, per=50, total_pages=None):
    revs = [{"reviewId": f"r{(page - 1) * per + i}", "businessId": oid, "text": "т", "rating": 4,
             "updatedTime": "2026-01-01T00:00:00.000Z", "author": {"publicId": f"p{i}", "name": "Имя"}}
            for i in range(min(per, max(0, count - (page - 1) * per)))]
    item = {"id": oid, "title": "СберБанк", "seoname": "sberbank", "fullAddress": "Москва, улица Мира, 1",
            "compositeAddress": {"locality": "Москва"}, "coordinates": [37.6, 55.7],
            "ratingData": {"reviewCount": count, "ratingValue": 4.5, "ratingCount": count * 2},
            "reviewResults": {"reviews": revs, "params": {"count": count, "page": page,
                                                           "totalPages": total_pages or -(-count // per)}}}
    st = {"config": {"csrfToken": "tok", "counters": {"analytics": {"sessionId": "sid"}}},
          "stack": [{"results": {"items": [item], "requestId": "rq"}}]}
    return f'<html><script type="application/json" class="state-view">{json.dumps(st, ensure_ascii=False)}</script></html>'


def test_yandex_pages_stop_at_twelve():
    def handler(req: httpx.Request):
        page = int(parse_qs(urlparse(str(req.url)).query).get("page", ["1"])[0])
        return httpx.Response(200, text=_ya_state("42", page, 1944, total_pages=39))
    c = YandexMapsCollector(Settings(), http=http_for(handler, "yandex"), signed=False)
    pages = asyncio.run(collect(c.pages("42")))
    assert len(pages) == 12 and sum(len(p.reviews) for p in pages) == 600
    assert pages[0].declared_total == 1944 and pages[-1].next_cursor is None
    assert c.capabilities.full_history is False and c.capabilities.max_reachable == 600


def test_yandex_signed_union_dedupes_and_stops_on_captcha():
    calls = []

    def handler(req: httpx.Request):
        u = str(req.url)
        if "/maps/org/" in u:
            return httpx.Response(200, text=_ya_state("42", 1, 120))
        q = parse_qs(urlparse(u).query)
        calls.append((q["ranking"][0], q["page"][0]))
        # подпись — тот же хэш по строке без s
        raw = u.split("?", 1)[1].rsplit("&s=", 1)
        assert str(djb2x(raw[0])) == raw[1]
        page = int(q["page"][0])
        # у каждой сортировки свои 60 отзывов, половина пересекается с by_time
        base = {"by_time": 0, "by_rating_asc": 30, "by_rating_desc": 60, "by_relevance_org": 0}[q["ranking"][0]]
        revs = [{"reviewId": f"r{base + (page - 1) * 50 + i}", "text": "т", "rating": 3,
                 "updatedTime": "2026-01-01T00:00:00Z", "author": {"publicId": "x"}}
                for i in range(min(50, 60 - (page - 1) * 50))]
        return httpx.Response(200, json={"data": {"reviews": revs, "params": {"count": 120, "totalPages": 2}}})
    c = YandexMapsCollector(Settings(), http=http_for(handler, "yandex"), signed=True)
    assert c.capabilities.extra["newest_sort"] == "by_time"
    pages = asyncio.run(collect(c.pages("42", sort="union")))
    ids = [r.source_review_id for p in pages for r in p.reviews]
    assert len(ids) == len(set(ids)) == 120                  # 0–59, 30–89, 60–119 → 120 разных
    assert len(calls) == 8                                    # 4 сортировки × 2 страницы

    def blocked(req: httpx.Request):
        if "/maps/org/" in str(req.url):
            return httpx.Response(200, text=_ya_state("42", 1, 120))
        return httpx.Response(200, json={"type": "captcha", "captcha": {"captcha-page": "https://x/showcaptcha"}})
    c2 = YandexMapsCollector(Settings(), http=http_for(blocked, "yandex"), signed=True)
    with pytest.raises(SourceBlocked):
        asyncio.run(collect(c2.pages("42", sort="by_time")))


def test_signed_query_sorted_case_insensitive():
    q = signed_query({"pageSize": "50", "ajax": "1", "businessId": "7", "csrfToken": "a:b"})
    assert q.startswith("ajax=1&businessId=7&csrfToken=a%3Ab&pageSize=50&s=")



def test_twogis_settlements_from_page_config():
    page = ('{"18577971268640094":{"id":"18577971268640094","region_id":"132","segment_id":"0","name":"Муром",'
            '"country_code":"ru","alias":"murom","default_pos":{"zoom":13}},'
            '"1":{"id":"1","region_id":"32","segment_id":"0","name":"Москва","country_code":"ru","alias":"moscow"}}')

    def handler(req):
        return httpx.Response(200, text=page)
    c = TwoGisCollector(Settings(), http=http_for(handler, "2gis"))
    from bank_audit.vsp.collectors.base import Area
    assert asyncio.run(c.alias_for(Area(city="Муром"))) == "murom"
    assert asyncio.run(c.alias_for(Area(city="moscow"))) == "moscow"


def test_yandex_discover_uses_region_prefix_and_filters_chain():
    seen = []

    def handler(req: httpx.Request):
        seen.append(str(req.url))
        st = {"stack": [{"results": {"items": [
            {"id": "1", "title": "СберБанк", "seoname": "sberbank", "fullAddress": "Екатеринбург, улица Щорса, 29",
             "compositeAddress": {"locality": "Екатеринбург"}, "coordinates": [60.62, 56.81],
             "chain": {"id": "6003612"}, "ratingData": {"reviewCount": 499}},
            {"id": "2", "title": "СберБанк, банкомат", "seoname": "sberbank", "fullAddress": "Екатеринбург, улица Щорса, 31",
             "compositeAddress": {"locality": "Екатеринбург"}, "coordinates": [60.62, 56.81],
             "chain": {"id": "6003611"}, "ratingData": {"reviewCount": 3}}]}}]}
        return httpx.Response(200, text=f'<script type="application/json" class="state-view">{json.dumps(st)}</script>')
    from bank_audit.vsp.collectors.base import Area
    c = YandexMapsCollector(Settings(), http=http_for(handler, "yandex"), signed=False)
    got = asyncio.run(collect(c.discover(Area(city="Екатеринбург", lat=56.81, lon=60.61))))
    assert [b.source_branch_id for b in got] == ["1"] and got[0].declared_reviews == 499
    assert "/maps/213/moscow/chain/sberbank/6003612/" in seen[0] and "ll=60.610000%2C56.810000" in seen[0]


def test_yandex_card_carries_cbr_number():
    from bank_audit.vsp.collectors.yandex import to_source_branch
    it = {"id": "1128554212", "title": "СберБанк", "fullAddress": "Москва, Лубянский проезд, 17",
          "compositeAddress": {"locality": "Москва"}, "coordinates": [37.63, 55.75], "chain": {"id": "6003612"},
          "features": [{"id": "nomer_sp_cbr", "value": "1481/1948/115", "aref": "#aqua_cbr_ru", "type": "text"},
                       {"id": "office_type", "type": "enum", "value": [{"id": "office_retail"}, {"id": "office_sme"}]},
                       {"id": "devision_type_bank", "type": "enum", "value": [{"id": "additional_bank"}]},
                       {"id": "actualization_date_CB", "type": "text", "value": "04.10.2026"},
                       {"id": "wifi", "type": "bool", "value": True}]}
    b = to_source_branch(it)
    assert b.raw["cbr_num"] == "1481/1948/115" and b.raw["office_type"] == ["office_retail", "office_sme"]
    assert b.raw["division_type"] == ["additional_bank"] and b.raw["cbr_actual"] == "04.10.2026"
