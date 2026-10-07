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
