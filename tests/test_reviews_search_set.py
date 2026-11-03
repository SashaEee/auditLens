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
    monkeypatch.setattr(bankiru_fts, "search_near", lambda *a, **k: ([dict(w) for w in words], False))
    monkeypatch.setattr(RD, "_keep_urls", lambda urls, **k: set(urls) if keep is None else keep)
    monkeypatch.setattr(RD, "_excluded", lambda urls, words: set())
    monkeypatch.setattr(RD, "_verify_sense", lambda q, rows: (rows, True))
    from bank_audit.rag import reviews_query
    monkeypatch.setattr(reviews_query, "expand", lambda q, use_llm=False: {"terms": [], "tsquery": q})
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


def test_near_window():
    """«Со словами запроса» — все значимые слова рядом, а не любое из них."""
    pos = bankiru_fts._positions("'навяза':3 'мне':4 'страховк':5 'кредит':9")
    assert bankiru_fts.near_window(pos, ["навяза", "страховк"])          # «навязали мне страховку»
    far = bankiru_fts._positions("'навяза':1 'страховк':20")
    assert not bankiru_fts.near_window(far, ["навяза", "страховк"])
    assert not bankiru_fts.near_window(pos, ["навяза", "ипотек"])        # слова нет вовсе
    assert bankiru_fts.near_window(pos, ["кредит"])                      # одно слово — где угодно
    pos3 = bankiru_fts._positions("'перевод':2,40 'сам':41 'себ':42")
    assert bankiru_fts.near_window(pos3, ["перевод", "сам", "себ"])


def test_near_window_matches_word_forms():
    """«не зачисляют выручку» находит «не зачислили выручку»: основы разные, начало общее."""
    pos = bankiru_fts._positions("'зачисл':4 'выручк':5")
    assert bankiru_fts.near_window(pos, ["зачисля", "выручк"])


def test_query_syntax():
    p = RD.parse_query('"переводы самому себе" -сбп навязали «страховку»')
    assert p["phrases"] == ["переводы самому себе"] and p["excl"] == ["сбп"]
    assert p["plain"] == "навязали страховку" and "переводы самому себе" in p["text"]


def test_words_first_then_checked_sense(monkeypatch):
    """Сначала все со словами, затем похожие по смыслу — только прошедшие проверку."""
    head = [{"url": "s1", "via": "смысл", "date": "2026-10-01", "text": "x" * 50},
            {"url": "w1", "via": "слова", "date": "2026-10-01", "text": "x" * 50},
            {"url": "s2", "via": "смысл", "date": "2026-10-01", "text": "x" * 50}]
    words = [{"url": u, "date": "2026-10-01"} for u in ("w1", "w2", "w3", "w4", "w5")]
    _setup(monkeypatch, head, words)
    monkeypatch.setattr(RD, "_verify_sense", lambda q, rows: ([r for r in rows if r["url"] == "s2"], True))
    got = RD.search_set("q", "Сбербанк")
    assert [r["url"] for r in got["rows"]][:1] == ["w1"]           # выше в слиянии — первым
    assert [r["url"] for r in got["rows"]][-1] == "s2" and got["meta"]["n_sense"] == 1
    assert all(r["via"] != "смысл" for r in got["rows"][:5])


def test_failures_are_visible_and_not_cached(monkeypatch):
    """Сбой — не «ничего не нашлось»: фильтры не применились → пустой набор с failed,
    словесная нога упала → words_failed; такие ответы не кэшируются."""
    head = [{"url": "s1", "via": "смысл", "date": "2026-10-01", "text": "x" * 50}]
    _setup(monkeypatch, head, [])
    monkeypatch.setattr(RD, "_keep_urls", lambda urls, **k: None)
    got = RD.search_set("навязали страховку", "Сбербанк")
    assert got["rows"] == [] and got["meta"]["failed"] == "filters" and not RD._SET_CACHE

    _setup(monkeypatch, head, [])

    def broken(groups, **k):
        k["_meta"]["failed"] = True
        return [], False
    monkeypatch.setattr(bankiru_fts, "search_near", broken)
    got = RD.search_set("навязали страховку", "Сбербанк")
    assert got["meta"]["words_failed"] is True and [r["url"] for r in got["rows"]] == ["s1"]
    assert not RD._SET_CACHE
    # смысл не проверен моделью — тоже не кэшируем
    _setup(monkeypatch, head, [{"url": "w1", "date": "2026-10-01"}])
    monkeypatch.setattr(RD, "_verify_sense", lambda q, rows: (rows, False))
    got = RD.search_set("навязали страховку", "Сбербанк")
    assert got["meta"]["sense_checked"] is False and not RD._SET_CACHE


def test_phrases_go_to_sql_phrase_search(monkeypatch):
    _setup(monkeypatch, [], [])
    seen = {}

    def fake_phrases(phrases, **k):
        seen["p"] = phrases
        k["_meta"]["stop_phrases"] = ["не без"]
        return [{"url": "p1", "date": "2026-10-01"}], False
    monkeypatch.setattr(bankiru_fts, "search_phrases", fake_phrases)
    monkeypatch.setattr(bankiru_fts, "search_near", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    got = RD.search_set('"переводы самому себе" "не без"', "Сбербанк")
    assert seen["p"] == ["переводы самому себе", "не без"]
    assert [r["url"] for r in got["rows"]] == ["p1"] and got["meta"]["stop_phrases"] == ["не без"]


def test_signal_week_filter():
    """Неделя всплеска (month=sig:<день>) — те же жалобы, что в числе сигнала:
    7 дней по этот день и события не старше 60 дней."""
    p: dict = {}
    c = RD._month_clause("i", "sig:2026-10-31", p)
    assert p["sig_d"] == "2026-10-31"
    assert "i.dt >= CAST(:sig_d AS date) - 6" in c and "i.dt < CAST(:sig_d AS date) + 1" in c
    assert "i.ev_date >= i.dt::date - 60" in c
    assert RD._month_clause("i", "sig:2026-10", {}) == " AND false"
    assert RD._month_clause("i", "2026-10", {"x": 1}).startswith(" AND date_trunc")
    from bank_audit.ai import agent_tools as T
    link = T.link_reviews("Сбербанк", None, "chargeback", 7, week_end="2026-10-31")
    assert "month=sig%3A2026-10-31" in link and "p=" not in link and "theme=chargeback" in link


def _no_summaries(monkeypatch):
    class R:
        def all(self): return []

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): return R()
    monkeypatch.setattr(RD.db, "session", lambda: S())


def test_sense_check_reranker_orders_model_decides(monkeypatch):
    """Реранкер упорядочивает и отсекает явно чужие, модель решает «та ли ситуация»
    пакетами по 10; модель молчит — порог реранкера; молчат оба — «не проверено»."""
    _no_summaries(monkeypatch)
    RD._SENSE_OK.clear()
    rows = [{"url": f"u{i}", "text": f"жалоба {i}"} for i in range(25)]
    scores = [i / 25 for i in range(25)]                    # u24 — ближе всех, u0 — 0
    monkeypatch.setattr(RD, "_rerank", lambda q, texts: list(scores))
    asked = []

    def model(q, texts):
        asked.append(len(texts))
        return [int(t.split()[-1]) % 2 == 0 for t in texts]      # чётные — та ситуация
    monkeypatch.setattr(RD, "_ask_relevance", model)
    got, ok = RD._verify_sense("q", rows)
    assert ok and sorted(asked) == [3, 10, 10]                 # u0 (0 < 0,05) отсечён реранкером
    assert [r["url"] for r in got][:3] == ["u24", "u22", "u20"] and all(int(r["url"][1:]) % 2 == 0 for r in got)
    assert "u0" not in {r["url"] for r in got}
    # модель не ответила — порог реранкера, порядок тот же
    RD._SENSE_OK.clear()
    monkeypatch.setattr(RD, "_ask_relevance", lambda q, texts: None)
    got, ok = RD._verify_sense("q", rows)
    assert ok and [r["url"] for r in got] == [f"u{i}" for i in range(24, 12, -1)]   # ≥ 0,5
    # не ответили оба — кандидаты как есть, с пометкой
    monkeypatch.setattr(RD, "_rerank", lambda q, texts: None)
    got, ok = RD._verify_sense("q", rows)
    assert not ok and len(got) == 25
