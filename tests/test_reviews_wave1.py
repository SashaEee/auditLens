"""«Аудит отзывов», волна 1 аудита 03.10: лента без потолка, рост против рынка,
сбой радара не выглядит как «спокойно». БД не нужна — сессия поддельная."""
from __future__ import annotations

import datetime as dt
import os

import pytest

from bank_audit.rag import reviews_dash as RD


class _Res:
    def __init__(self, rows=None, one=None):
        self._rows, self._one = rows or [], one

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        return self._one


class _Sess:
    """Отдаёт строки выборки по LIMIT/OFFSET, как Postgres."""

    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def execute(self, sql, p=None):
        q = str(sql)
        if "count(*)" in q:
            return _Res(one=(len(self.rows), 0))
        self.calls += 1
        pos, chunk = p["pos"], p["chunk"]
        return _Res(rows=self.rows[pos:pos + chunk])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _rows(n, dup_every=0):
    base = dt.datetime(2026, 10, 1)
    out = []
    for i in range(n):
        out.append({"url": f"u{i:05d}", "review_id": i, "source": "bankiru", "bank": "Сбербанк",
                    "product": None, "dt": base - dt.timedelta(minutes=i), "city": None, "rating": 1})
    return out


def _texts(rows, dup_every=0, short=()):
    t = {}
    for i, r in enumerate(rows):
        if i in short:
            t[r["url"]] = {"text": "коротко"}
        elif dup_every and i % dup_every == 1:
            t[r["url"]] = {"text": f"Копия жалобы номер {i - 1}: списали деньги без согласия, верните немедленно"}
        else:
            t[r["url"]] = {"text": f"Копия жалобы номер {i}: списали деньги без согласия, верните немедленно"}
    return t


@pytest.fixture
def feed(monkeypatch):
    def setup(rows, texts):
        sess = _Sess(rows)
        monkeypatch.setattr(RD.db, "session", lambda: sess)
        from bank_audit.rag import bankiru_fts
        monkeypatch.setattr(bankiru_fts, "bodies_for", lambda rs: {r["url"]: texts[r["url"]] for r in rs})
        monkeypatch.setattr(RD, "_attach_themes", lambda page: None)
        return sess
    return setup


def _walk(limit=20):
    items, cur, pages = [], 0, 0
    while True:
        r = RD._feed_page("TRUE", "", "", "i.dt DESC", {}, limit, cur, False)
        assert r["error"] is None
        items += r["items"]
        pages += 1
        if not r["has_more"]:
            return items, pages, r
        cur = r["next"]
        assert pages < 500


def test_feed_pages_past_600_without_losing_or_repeating(feed):
    """ОТЗ-01: раньше выборка упиралась в 600 строк и «Показать ещё» пропадала."""
    rows = _rows(2451)
    feed(rows, _texts(rows))
    first = RD._feed_page("TRUE", "", "", "i.dt DESC", {}, 20, 0, False)
    assert first["total"] == 2451 and first["has_more"] and first["next"] == 20
    items, pages, _ = _walk()
    urls = [i["url"] for i in items]
    assert len(urls) == 2451 and len(set(urls)) == 2451


def test_copies_and_short_texts_are_skipped_but_counted(feed):
    rows = _rows(300)
    feed(rows, _texts(rows, dup_every=2, short={5, 6, 7}))
    items, _, _ = _walk(limit=25)
    assert len({i["url"] for i in items}) == len(items)
    assert all(len(i["text"]) >= 40 for i in items)
    assert sum(i["similar"] for i in items) >= 140          # копии посчитаны, а не показаны


def test_vs_market_colors_growth_only_when_bank_outpaces_market():
    """ОТЗ-04: «+20%» при росте рынка на 12% — не «стало хуже на 20%»."""
    # снимок 03.10: Сбер 2451 против ~2035, остальные 22 818 против ~20 317
    vm = RD._vs_market(2451, 2035, 22818, 20317)
    assert vm["market_delta_pct"] == pytest.approx(12.3, abs=0.1)
    assert vm["rel_pct"] == pytest.approx(7.2, abs=0.3)
    assert vm["vs_market"] == "above"                      # опережение есть и значимо
    same = RD._vs_market(1200, 1000, 24000, 20000)          # +20% у обоих
    assert same["vs_market"] == "same" and same["rel_pct"] == pytest.approx(0.0, abs=0.1)
    below = RD._vs_market(1000, 1000, 30000, 20000)         # рынок +50%, банк на месте
    assert below["vs_market"] == "below"
    assert RD._vs_market(10, 0, 100, 90) == {}             # нет базы — нет сравнения


def test_volume_item_follows_market(monkeypatch):
    ov = {"total": 1200, "prev": 1000, "delta_pct": 20.0, "market_delta_pct": 20.0,
          "vs_market": "same", "delta_vs_market_pct": 0.0, "esc_n": 0, "esc_prev_n": 0}
    monkeypatch.setattr(RD, "resolve_bank", lambda b: "Сбербанк")
    monkeypatch.setattr(RD, "overview", lambda *a, **k: ov)
    monkeypatch.setattr(RD, "themes", lambda *a, **k: {"themes": []})
    monkeypatch.setattr(RD, "weekly_signals", lambda *a, **k: {"signals": []})
    vol = next(i for i in RD.changes("Сбербанк")["items"] if i["kind"] == "volume")
    assert vol["dir"] == "flat" and vol["rise"] is True and "рынок +20%" in vol["text"]
    ov.update(vs_market="above", market_delta_pct=12.0)
    vol = next(i for i in RD.changes("Сбербанк")["items"] if i["kind"] == "volume")
    assert vol["dir"] == "up" and "рынок +12%" in vol["text"]


def test_radar_failure_is_not_calm(monkeypatch):
    """ОТЗ-02: упавший расчёт сигналов показывался зелёной галочкой «аномалий нет»."""
    import asyncio
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from bank_audit.web import app as A
    monkeypatch.setattr(RD, "weekly_signals", lambda *a, **k: None)
    out = asyncio.run(A.reviews_anomalies("Сбербанк"))
    assert out["calm"] is False and out["error"] == "signals_failed"
