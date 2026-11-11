"""Поиск по теме «происшествия со Сбером» в потоке новостей (digest/newsflow).

Фильтры — всегда; проход целиком — на живом Postgres:
NEWSFLOW_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db (миграцию 071 тест
накатывает сам, нужен pgvector). Без переменной этот тест пропускается.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.digest import newsflow as nf  # noqa: E402

PG = os.getenv("NEWSFLOW_PG_TEST_URL")


def test_filters_hosts_and_title():
    skip = lambda u: bool(nf._SEARCH_SKIP.search(nf._host_of(u)))  # noqa: E731
    assert skip("https://dzen.ru/a/xyz") and skip("https://m.vk.ru/wall1") and skip("https://www.sber.ru/x")
    assert skip("https://sudact.ru/regular/doc/1") and skip("https://www.banki.ru/news/lenta/?id=1")
    assert not skip("https://kam.business-gazeta.ru/news/1") and not skip("https://rt.rbc.ru/tatarstan/1")
    assert not skip("https://realnoevremya.ru/news/1")
    assert nf._og_title('<meta property="og:title" content="У &quot;Сбера&quot; похитили 60 млн">') \
        == 'У "Сбера" похитили 60 млн'
    assert nf._SBER_RE.search("УФСБ: путём обмана сотрудников «Сбера»")


@pytest.fixture
def pg(monkeypatch):
    if not PG:
        pytest.skip("нужна живая база: NEWSFLOW_PG_TEST_URL")
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    from bank_audit.config import ROOT
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute((ROOT / "migrations" / "071_news_flow.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    with eng.begin() as c:
        c.execute(text("DELETE FROM news_item"))
        c.execute(text("DELETE FROM news_source_state"))
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    yield eng
    eng.dispose()


def test_collect_search_keeps_only_fresh_sber_news(pg, monkeypatch):
    from sqlalchemy import text

    from bank_audit.rag import search_gateway as sg
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    items = [
        {"url": "https://realnoevremya.ru/news/1", "title": "УФСБ: у «Сбера» увели 60 млн…", "snippet": "схема"},
        {"url": "https://vluki.ru/news/145839", "title": "Работницу Сбербанка оштрафовали", "snippet": ""},
        {"url": "https://example-news.ru/undated", "title": "Сбербанк: хищение", "snippet": ""},
        {"url": "https://dzen.ru/a/1", "title": "Сбербанк похитили", "snippet": ""},
        {"url": "https://ria.ru/20261006/x.html", "title": "В Севастополе осудили сотрудницу банка", "snippet": ""},
    ]
    pages = {
        "https://realnoevremya.ru/news/1":
            f'<meta property="og:title" content="УФСБ: в Татарстане путём обмана сотрудников «Сбера» увели 60 млн рублей">'
            f'<script type="application/ld+json">{{"datePublished":"{fresh}"}}</script>',
        "https://vluki.ru/news/145839": '<meta property="article:published_time" content="2014-11-06T10:00:00+03:00">',
        "https://example-news.ru/undated": "<html>без даты</html>",
    }
    calls = []
    monkeypatch.setattr(sg, "enabled", lambda: True)
    monkeypatch.setattr(sg, "yandex_search", lambda q, **k: calls.append((q, k)) or
                        SimpleNamespace(status="ok", items=items, detail=""))
    monkeypatch.setattr(nf.nm, "_get", lambda u: SimpleNamespace(status_code=200, text=pages.get(u, "")))

    out = nf.collect_search()
    assert out["added"] == 1 and out["dropped"]["old"] == 1 and out["dropped"]["no_date"] == 1
    assert all(k.get("fresh_hours") == nf.SEARCH_MAX_AGE_H for _q, k in calls)
    with pg.begin() as c:
        rows = c.execute(text("SELECT source, title, url FROM news_item")).all()
    assert rows == [("web_sber", "УФСБ: в Татарстане путём обмана сотрудников «Сбера» увели 60 млн рублей",
                     "https://realnoevremya.ru/news/1")]
    assert nf.collect_search() == {"skipped": "рано"}            # раз в SEARCH_EVERY_MIN
    again = nf.collect_search(force=True)
    assert again["added"] == 0                                    # уже виденное не качается и не дублируется


def test_junk_tld_and_search_rel_cap(monkeypatch):
    """Мусорные домены — мимо; не происшествие из поиска — rel не выше 4."""
    assert nf._JUNK_TLD.search(nf._host_of("https://meshlink.mom/blog/x"))
    assert not nf._JUNK_TLD.search(nf._host_of("https://realnoevremya.ru/news/1"))
    import asyncio
    from types import SimpleNamespace
    rows = [(1, "web_sber", "Сбербанк изменил правила для всех", ""),
            (2, "web_sber", "У Сбера похитили 60 млн", ""),
            (3, "ria_novosti", "Сбербанк изменил тарифы", "")]
    saved = []

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None):
            if p is None or isinstance(p, dict):
                return SimpleNamespace(all=lambda: rows)
            saved.extend(p)
    monkeypatch.setattr(nf.db, "session", lambda: S())

    async def chat(*a, **k):
        return ('{"items":[{"n":1,"rel":9,"type":"sber"},{"n":2,"rel":9,"type":"fraud"},'
                '{"n":3,"rel":7,"type":"sber"}]}', 1, 1)
    monkeypatch.setattr(nf, "_chat", chat)
    asyncio.run(nf.stage1())
    by = {p["id"]: p["rel"] for p in saved}
    assert by == {1: 4, 2: 9, 3: 7}


# ── разбор 11.11.2026: поиск находил поздно и не доходил до оценки ───────────

def test_search_skips_job_sites_forums_and_daily_outage_pages():
    skip = lambda u: bool(nf._SEARCH_SKIP.search(nf._host_of(u)))  # noqa: E731
    for u in ("https://hh.ru/vacancy/1", "https://changellenge.com/vacancy/2", "https://otvet.mail.ru/question/3",
              "https://forum.ixbt.com/topic.cgi?id=4", "https://forumrieltorov.ru/t/5", "https://meshok.net/item/6",
              "https://www.sberleasing.ru/realizaciya/7"):
        assert skip(u), u
    for u in ("https://www.yugopolis.ru/news/1", "https://www.bnkomi.ru/data/news/2", "https://news.mail.ru/3",
              "https://platforum.ru/news/4", "https://www.kommersant.ru/doc/5"):
        assert not skip(u), u
    daily = nf._SEARCH_DAILY.search
    assert daily("Сегодня, 6 ноября, у многих пользователей не работает Сбербанк")
    assert daily("Сегодня, 19 октября, в работе Сбербанка фиксируются перебои")
    assert daily("Сбербанк 26 октября работает с перебоями")
    assert not daily("Сбербанк признал повторное списание средств со счетов клиентов")
    assert not daily("У жителей Якутска второй день не работает Сбербанк Онлайн")


def test_search_takes_the_whole_page_in_both_orders(monkeypatch):
    """Раньше брались первые 10 документов «по релевантности»: свежее ждало, пока
    состарится до первых мест, и находилось через полтора суток."""
    from bank_audit.rag import search_gateway as sg
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    by_rel = [{"url": f"https://old-news.example/{i}", "title": f"Сбербанк: старая новость номер {i}", "snippet": ""}
              for i in range(30)]
    late = {"url": "https://www.yugopolis.ru/news/9", "snippet": "",
            "title": "Сбербанк признал повторное списание средств со счетов клиентов"}
    calls = []

    def search(q, **k):
        calls.append((q, k))
        return SimpleNamespace(status="ok", detail="", items=([late] if k.get("by_time") else by_rel + [late]))

    written = []

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None):
            if isinstance(p, list):
                written.extend(p)
                return SimpleNamespace(rowcount=len(p), all=lambda: [])
            return SimpleNamespace(rowcount=0, all=lambda: [], scalar=lambda: None)
    monkeypatch.setattr(nf.db, "session", lambda: S())
    monkeypatch.setattr(sg, "enabled", lambda: True)
    monkeypatch.setattr(sg, "yandex_search", search)
    monkeypatch.setattr(nf, "SEARCH_QUERIES", ("Сбербанк ошибочное списание",))
    monkeypatch.setattr(nf.nm, "_get", lambda u: SimpleNamespace(
        status_code=200, text=f'<script type="application/ld+json">{{"datePublished":"{fresh}"}}</script>'))
    out = nf.collect_search(force=True)
    assert [k.get("by_time") for _q, k in calls] == [True, False]
    assert all(k["page_size"] == nf.SEARCH_PAGE and k["max_results"] == nf.SEARCH_PAGE
               and k["fresh_hours"] == nf.SEARCH_MAX_AGE_H for _q, k in calls)
    assert out["candidates"] == 31                       # тридцать первая по релевантности не отрезана
    assert written[0]["u"] == "https://www.yugopolis.ru/news/9"   # найденное по времени — первым в очереди на чтение


def _ev(eid, items, value, cat):
    return {"event_id": eid, "lead": items[0], "items": items, "n_sources": len({x["source"] for x in items}),
            "rel": 9, "rtype": "incident", "s2": {"category": cat, "headline": "h"}, "value": value,
            "ts": max(x["ts"] for x in items), "fresh": nf.fresh_ts(items)}


def _it(source, url, hours_ago, seen_hours_ago=None):
    now = datetime.now(timezone.utc)
    return {"id": 1, "source": source, "url": url, "title": "t", "body": "", "rel": 9, "rtype": "incident",
            "value": None, "s2": None, "image": None, "ts": now - timedelta(hours=hours_ago),
            "first_seen": now - timedelta(hours=hours_ago if seen_hours_ago is None else seen_hours_ago)}


def test_age_of_a_search_find_counts_from_the_moment_it_was_found():
    feed = [_it("ria_novosti", "https://ria.ru/1", 40)]
    found = [_it("web_sber", "https://www.bnkomi.ru/1", 40, 3)]
    now = datetime.now(timezone.utc)
    assert (now - nf.fresh_ts(feed)).total_seconds() / 3600 > 39
    assert (now - nf.fresh_ts(found)).total_seconds() / 3600 < 4
    assert nf.search_only({"items": found}) and not nf.search_only({"items": feed + found})


def test_issue_takes_late_search_finds_and_keeps_a_single_site_incident_out_of_the_lead(monkeypatch):
    theft = _ev(1, [_it("web_sber", "https://www.bnkomi.ru/1", 40, 3)], 8, "fraud")
    solo = _ev(2, [_it("web_sber", "https://info-site.example/2", 30, 2)], 9, "incident")
    two = _ev(3, [_it("web_sber", "https://www.yugopolis.ru/3", 30, 2),
                  _it("web_sber", "https://kuban24.example/3", 28, 2)], 9, "incident")
    in_feeds = _ev(4, [_it("web_sber", "https://www.yuga.ru/4", 20, 2), _it("tg_banksta", "https://t.me/banksta/4", 5)],
                   9, "incident")
    stale_feed = _ev(5, [_it("ria_novosti", "https://ria.ru/5", 40)], 8, "fraud")
    monkeypatch.setattr(nf, "_event_rows", lambda **k: [theft, solo, two, in_feeds, stale_feed])

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=None): return SimpleNamespace(scalar=lambda: None)
    monkeypatch.setattr(nf.db, "session", lambda: S())
    got = {e["event_id"]: e["value"] for e in nf.day_events()}
    assert got == {1: 8, 2: nf.SEARCH_SOLO_CAP, 3: 9, 4: 9}     # событие из лент старше суток — мимо, как и раньше
    assert nf.SEARCH_SOLO_CAP < 7                                # поводом дня становится новость от 7

