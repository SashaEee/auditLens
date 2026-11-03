"""Поиск по жалобам: один конечный набор для ленты, счётчика и выгрузки
(обращение 29.10.2026: «дословных 38, по смыслу 4», а лента листалась до 140+)."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import bankiru_fts  # noqa: E402
from bank_audit.rag import reviews_dash as RD  # noqa: E402


def _setup(monkeypatch, head, words, keep=None):
    def fake_search(q, **kw):
        kw["_meta"].update({"terms": ["перевод"], "tsquery": "перевод"})
        return [dict(r) for r in head]
    monkeypatch.setattr(RD, "search_reviews", fake_search)
    monkeypatch.setattr(bankiru_fts, "is_ready", lambda: True)
    monkeypatch.setattr(bankiru_fts, "search_fts", lambda *a, **k: [dict(w) for w in words])
    monkeypatch.setattr(RD, "_keep_urls", lambda urls, **k: set(urls) if keep is None else keep)
    RD._SET_CACHE.clear()


def test_set_is_finite_and_counts_are_exact(monkeypatch):
    # верхушка слияния: 2 со словами, 30 только по смыслу; словесная нога — 50 попаданий
    head = ([{"url": f"w{i}", "via": "слова", "date": "2026-10-01", "text": "x" * 50} for i in range(2)]
            + [{"url": f"s{i}", "via": "смысл", "date": "2026-10-01", "text": "x" * 50} for i in range(30)])
    words = [{"url": f"w{i}", "date": "2026-09-01", "bank": "Сбербанк"} for i in range(50)]
    # жалоба, которую верхушка сочла «по смыслу», хотя слова в ней есть
    head[2]["url"] = "w40"
    _setup(monkeypatch, head, words)
    got = RD.search_set("переводы самому себе", "Сбербанк")
    m = got["meta"]
    assert m["n_words"] == 50 and m["n_sense"] == RD.SENSE_CAP and m["total"] == 50 + RD.SENSE_CAP
    assert len(got["rows"]) == m["total"]
    vias = {r["url"]: r["via"] for r in got["rows"]}
    assert vias["w40"] == "слова и смысл"
    assert sum(1 for v in vias.values() if v == "смысл") == RD.SENSE_CAP


def test_filters_apply_before_counting(monkeypatch):
    head = [{"url": "a", "via": "слова", "date": "2026-10-01", "text": "x" * 50}]
    words = [{"url": u, "date": "2026-10-01"} for u in ("a", "b", "c")]
    _setup(monkeypatch, head, words, keep={"a", "c"})
    m = RD.search_set("q", "Сбербанк")["meta"]
    assert m["n_words"] == 2 and m["total"] == 2


def test_feed_pages_through_the_same_set(monkeypatch):
    head = [{"url": f"s{i}", "via": "смысл", "date": "2026-10-01", "text": "x" * 50} for i in range(5)]
    words = [{"url": f"w{i}", "date": "2026-10-01"} for i in range(30)]
    _setup(monkeypatch, head, words)
    monkeypatch.setattr(bankiru_fts, "bodies_for", lambda rows: {r["url"]: {"text": "y" * 60} for r in rows})
    monkeypatch.setattr(bankiru_fts, "headline", lambda texts, tq: texts)
    monkeypatch.setattr(RD, "_attach_themes", lambda page: None)
    monkeypatch.setattr(RD, "resolve_bank", lambda b: b)
    seen, off = [], 0
    while True:
        r = RD.list_reviews_ex("Сбербанк", q="переводы", limit=20, offset=off)
        seen += [x["url"] for x in r["items"]]
        assert r["total"] == 35
        if not r["has_more"]:
            break
        off += 20
    assert len(seen) == 35 == len(set(seen))
