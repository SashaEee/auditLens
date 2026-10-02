"""Шлюз поиска, цепочка бэкендов, копии страниц — без сети и без БД."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from bank_audit.rag import search_gateway as gw
from bank_audit.rag import web_search as ws

XML_OK = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response><results><grouping><group>
<doc id="1"><url>https://www.sberbank.ru/ru/person/credits/home/family</url>
<domain>www.sberbank.ru</domain>
<title><hlword>Семейная</hlword> ипотека &amp; условия</title>
<modtime>20260921T033736</modtime>
<saved-copy-url>https://yandexwebcache.net/yandbtm?fmode=inject&amp;tm=1790273607&amp;la=1789961856&amp;url=x</saved-copy-url>
<passages><passage>Ставка от <hlword>6%</hlword></passage><passage>Взнос от 20%</passage></passages>
</doc></group></grouping></results></response></yandexsearch>"""

XML_EMPTY = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response><error code="15">Sorry, there are no results</error></response></yandexsearch>"""

XML_BAD_CODE = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response><error code="55">limit</error></response></yandexsearch>"""


@pytest.fixture
def fresh_gw(monkeypatch):
    """Настроенный шлюз с чистым ограничителем, размыкателями и без телеметрии."""
    monkeypatch.setenv("SEARCH_GATEWAY_URL", "https://gw.test")
    monkeypatch.setenv("SEARCH_GATEWAY_API_KEY", "k")
    monkeypatch.setenv("SEARCH_GATEWAY_RPS", "50")
    monkeypatch.delenv("SEARCH_PRIMARY", raising=False)
    monkeypatch.setattr(gw, "_bucket", gw._Bucket())
    monkeypatch.setattr(gw, "_copy_bucket", gw._Bucket(lambda: 50))
    monkeypatch.setattr(gw, "_breaker", gw._Breaker("t"))
    monkeypatch.setattr(gw, "_copy_breaker", gw._Breaker("t2"))
    monkeypatch.setattr(gw, "emit", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_META", gw.OrderedDict())
    return gw


def _mock_client(monkeypatch, handler, attr="_client"):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gw.test")
    monkeypatch.setattr(gw, attr, client)
    return client


# ── Запрос ───────────────────────────────────────────────────────────────────
def test_build_query_sites_or_and_freshness():
    q = gw.build_query("Сбербанк сбой OR инцидент site:www.sberbank.ru", ["cbr.ru", "sberbank.ru"],
                       fresh_hours=48)
    assert "OR" not in q and " | " in q
    assert "(site:sberbank.ru | site:cbr.ru)" in q
    assert "date:>" in q
    one = gw.build_query("вклад на 6 месяцев", ["https://www.vtb.ru/personal/"])
    assert one.endswith("site:vtb.ru")


def test_build_query_drops_unbalanced_syntax_and_fits_limit():
    q = gw.build_query("ипотека (новостройка \"семья", None)
    assert "(" not in q and '"' not in q
    long = gw.build_query("слово " * 200, ["cbr.ru"])
    assert len(long) <= 400 and long.endswith("site:cbr.ru")


# ── Разбор ответа ────────────────────────────────────────────────────────────
def test_parse_xml_fields():
    status, items, _ = gw.parse_xml(XML_OK)
    assert status == gw.OK and len(items) == 1
    it = items[0]
    assert it["title"] == "Семейная ипотека & условия"
    assert it["snippet"] == "Ставка от 6% … Взнос от 20%"
    assert it["date"] == "2026-09-21"
    assert it["domain"] == "sberbank.ru"
    assert it["cache_url"].startswith("https://yandexwebcache.net/") and "&amp;" not in it["cache_url"]


def test_parse_xml_error_codes():
    assert gw.parse_xml(XML_EMPTY)[0] == gw.EMPTY          # «ничего не найдено» — не сбой
    assert gw.parse_xml(XML_BAD_CODE)[0] == gw.ERROR
    assert gw.parse_xml("<oops")[0] == gw.ERROR
    assert gw.parse_xml("")[0] == gw.ERROR


# ── Ограничитель и ошибки шлюза ──────────────────────────────────────────────
def test_bucket_limits_and_waits():
    b = gw._Bucket(lambda: 2.0)
    assert b.acquire(0) and b.acquire(0)
    assert not b.acquire(0)                 # ведро пусто, ждать нельзя
    t0 = time.monotonic()
    assert b.acquire(1.0)                   # дождались токена
    assert time.monotonic() - t0 >= 0.3


def test_429_then_success_retries_once(fresh_gw, monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.05"})
        return httpx.Response(200, json={"raw_text": XML_OK})
    _mock_client(monkeypatch, handler)
    res = gw.yandex_search("семейная ипотека")
    assert res.status == gw.OK and len(calls) == 2


def test_repeated_429_is_limited_not_error(fresh_gw, monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(429, headers={"retry-after": "0.05"}))
    assert gw.yandex_search("x").status == gw.LIMITED
    assert not gw._breaker.is_open()        # лимит — не поломка


@pytest.mark.parametrize("code", [401, 403, 402])
def test_auth_and_quota_trip_breaker(fresh_gw, monkeypatch, code):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(code)
    _mock_client(monkeypatch, handler)
    assert gw.yandex_search("x").status == gw.DOWN
    assert gw._breaker.is_open()
    assert gw.yandex_search("x").status == gw.DOWN
    assert len(calls) == 1                  # второй раз в шлюз не ходили


def test_server_errors_trip_after_streak(fresh_gw, monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(502))
    for _ in range(2):
        assert gw.yandex_search("x").status == gw.DOWN
    assert not gw._breaker.is_open()
    gw.yandex_search("x")
    assert gw._breaker.is_open()


def test_no_slot_goes_limited_without_http(fresh_gw, monkeypatch):
    monkeypatch.setattr(gw, "_bucket", gw._Bucket(lambda: 1.0))
    gw._bucket.acquire(0)
    called = []
    _mock_client(monkeypatch, lambda req: called.append(1) or httpx.Response(200, json={"raw_text": XML_OK}))
    assert gw.yandex_search("x", max_wait=0).status == gw.LIMITED
    assert not called


def test_search_sends_site_in_query_and_remembers_meta(fresh_gw, monkeypatch):
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        assert req.headers["X-Api-Key"] == "k"
        return httpx.Response(200, json={"raw_text": XML_OK})
    _mock_client(monkeypatch, handler)
    res = gw.yandex_search("ипотека", sites=["sberbank.ru"])
    assert seen["query"]["queryText"] == "ипотека site:sberbank.ru"
    meta = gw.meta_for(res.items[0]["url"])
    assert meta["date"] == "2026-09-21" and meta["cache_url"]


def test_not_configured_is_down(monkeypatch):
    monkeypatch.delenv("SEARCH_GATEWAY_URL", raising=False)
    monkeypatch.setattr(gw, "emit", lambda *a, **k: None)
    assert gw.yandex_search("x").status == gw.DOWN
    assert not gw.yandex_first()


# ── Копии страниц ────────────────────────────────────────────────────────────
COPY_HTML = ("<html><head><title>Кредит на образование</title></head><body>"
             "<div id=\"yandex-cache-hdr\">Это сохранённая копия страницы</div>"
             "<main><h1>Кредит на образование</h1><p>" + "Ставка 3%. " * 80 + "</p></main></body></html>")


def test_cached_copy_via_url_operator(fresh_gw, monkeypatch):
    queries = []

    def gw_handler(req):
        queries.append(json.loads(req.content)["query"]["queryText"])
        return httpx.Response(200, json={"raw_text": XML_OK})
    _mock_client(monkeypatch, gw_handler)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, text=COPY_HTML), attr="_copy_client")
    copy = gw.read_cached_copy("https://www.sberbank.ru/ru/person/credits/home/family")
    assert queries == ["url:https://www.sberbank.ru/ru/person/credits/home/family"]
    assert copy is not None and copy.copy_date == "2026-09-21"
    assert "сохранённая копия" not in copy.content.decode()   # шапку Яндекса вырезали
    assert "Ставка 3%" in copy.content.decode()


def test_cached_copy_uses_meta_from_search(fresh_gw, monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"raw_text": XML_OK}))
    gw.yandex_search("ипотека")
    queries = []
    _mock_client(monkeypatch, lambda req: queries.append(1) or httpx.Response(500))
    _mock_client(monkeypatch, lambda req: httpx.Response(200, text=COPY_HTML), attr="_copy_client")
    assert gw.read_cached_copy("https://www.sberbank.ru/ru/person/credits/home/family") is not None
    assert not queries                      # ссылка на копию уже была в выдаче


def test_cached_copy_captcha_pauses(fresh_gw, monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={"raw_text": XML_OK}))
    _mock_client(monkeypatch, lambda req: httpx.Response(
        200, text="<html>" + "x" * 600 + "showcaptcha</html>"), attr="_copy_client")
    assert gw.read_cached_copy("https://www.sberbank.ru/a") is None
    assert gw._copy_breaker.is_open()
    assert not gw._breaker.is_open()        # поиск при этом работает


# ── Цепочка web_search ───────────────────────────────────────────────────────
@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(ws.rag_cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(ws.rag_cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(gw, "emit", lambda *a, **k: None)
    monkeypatch.setenv("SEARCH_GATEWAY_URL", "https://gw.test")
    monkeypatch.setenv("SEARCH_GATEWAY_API_KEY", "k")
    monkeypatch.setenv("FLEET_SEARXNG_URL", "https://fleet.test")
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    order = []

    def mk(name, result):
        def fn(query, **kw):
            order.append((name, kw))
            return result
        return fn
    return order, mk


def _item(url):
    return {"title": "t", "url": url, "snippet": "s", "domain": ws.urlparse(url).hostname}


def test_yandex_first_then_fleet_on_failure(chain, monkeypatch):
    order, mk = chain
    monkeypatch.setattr(ws, "_search_gw_yandex", mk("yandex_gw", []))
    monkeypatch.setattr(ws, "_search_fleet", mk("fleet", [_item("https://cbr.ru/a")]))
    monkeypatch.setattr(ws, "_search_ddgs", mk("ddgs", [_item("https://x.ru")]))
    res = ws.search("ПСК требования", site_filter=["cbr.ru"], fresh_hours=48)
    assert [n for n, _ in order] == ["yandex_gw", "fleet"]
    assert order[0][1]["fresh_hours"] == 48
    assert res[0]["url"] == "https://cbr.ru/a"


def test_primary_fleet_swaps_order(chain, monkeypatch):
    order, mk = chain
    monkeypatch.setenv("SEARCH_PRIMARY", "fleet")
    monkeypatch.setattr(ws, "_search_gw_yandex", mk("yandex_gw", [_item("https://a.ru")]))
    monkeypatch.setattr(ws, "_search_fleet", mk("fleet", []))
    ws.search("вклады")
    assert [n for n, _ in order] == ["fleet", "yandex_gw"]


def test_fleet_gets_site_in_text_not_include_domains(monkeypatch):
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json={"results": []})       # по сайту пусто
        return httpx.Response(200, json={"results": [
            {"url": "https://other.ru/x", "domain": "other.ru", "title": "t", "content": "c",
             "published_at": "2026-09-20T10:00:00Z"}]})
    real = httpx.Client
    monkeypatch.setattr(ws.httpx, "Client",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    out = ws._fleet_v1_query("https://fleet.test", "ПСК требования site:cbr.ru",
                             max_results=8, site_filter=["consultant.ru"])
    assert "include_domains" not in bodies[0]
    assert bodies[0]["query"] == "ПСК требования (site:cbr.ru OR site:consultant.ru)"
    assert bodies[1]["query"] == "ПСК требования"                 # широкий повтор
    assert out[0]["off_domain"] and out[0]["date"] == "2026-09-20"


# ── Ретривер глубокого отчёта ────────────────────────────────────────────────
def test_retriever_broad_retry_when_sites_empty(monkeypatch):
    from bank_audit.research.gptr import retriever as R
    calls = []

    def fake_search(query, **kw):
        calls.append(kw)
        if kw.get("site_filter"):
            return []
        return [_item("https://www.cbr.ru/explan/"), _item("https://forum.pikabu.ru/x")]
    monkeypatch.setattr(ws, "search", fake_search)
    out = R.WebSearch("ПСК требования site:cbr.ru").search(max_results=5)
    assert calls[0]["site_filter"] == ["cbr.ru"] and "site_filter" not in calls[1]
    assert [o["href"] for o in out] == ["https://www.cbr.ru/explan/"]   # форум отсеян доверием
    assert R.FleetSearch is R.WebSearch


# ── Скрапер: копия раньше браузера ───────────────────────────────────────────
STUB = b"<html><head><title>sberbank.ru</title></head><body>Please enable JavaScript</body></html>"


def _scraper(monkeypatch, copy):
    from bank_audit.research.gptr import runstate, scraper as S
    browser_calls = []

    def fake_fetch(url, prefer_browser=False, force_refresh=False, **kw):
        if prefer_browser:
            browser_calls.append(url)
        return SimpleNamespace(content=STUB, content_type="text/html", final_url=url)
    monkeypatch.setattr(S.fetcher, "fetch", fake_fetch)
    monkeypatch.setattr(gw, "read_cached_copy", lambda url, **k: copy)
    state = runstate.RunState()
    sc = S.AuditLensScraper("https://www.sberbank.ru/ru/person/credits/money/credit_na_obrazovanie",
                            state=state)
    text, _, _ = sc.scrape()
    return text, state, browser_calls


def test_scraper_reads_copy_before_browser(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    copy = gw.CachedCopy(COPY_HTML.encode(), today, "https://yandexwebcache.net/x")
    text, state, browser_calls = _scraper(monkeypatch, copy)
    assert "Ставка 3%" in text
    assert state.cached_copies and not browser_calls
    assert state.pages and not state.unreadable


def test_scraper_skips_stale_copy_and_uses_browser(monkeypatch):
    copy = gw.CachedCopy(COPY_HTML.encode(), "2020-01-01", "https://yandexwebcache.net/x")
    _text, state, browser_calls = _scraper(monkeypatch, copy)
    assert browser_calls and not state.cached_copies


def test_gaps_mention_copies():
    from bank_audit.research.gptr import gaps
    registry = SimpleNamespace(by_cell=lambda: {}, facts=[SimpleNamespace(stance="observed", subject="")])
    lines = gaps.collect(SimpleNamespace(subjects=[], subject_labels={}), registry=registry,
                         attributes=[], pages={}, unreadable={},
                         cached_copies={"https://www.sberbank.ru/a": "2026-09-21"})
    assert any("сохранённой копии Яндекса" in ln and "2026-09-21" in ln for ln in lines)


# ── «Обзор»: дата из выдачи — последний довод ────────────────────────────────
def test_news_uses_search_date_hint_last(monkeypatch):
    from bank_audit.digest import news
    monkeypatch.setattr(news, "_page_date", lambda url: None)
    items = [{"url": "https://example.ru/news/item", "title": "Сбой", "snippet": "",
              "ts": None, "ts_hint": "2026-09-23", "source": "web_search"}]
    kept = news._resolve_undated(items)
    assert kept and kept[0]["ts"].date().isoformat() == "2026-09-23"
    assert "ts_hint" not in kept[0]
