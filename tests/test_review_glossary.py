"""Словарь «Аудита отзывов» и числа ИИ-помощника, выровненные с вкладкой (04.11.2026).

Что лечим: выгрузка и инструмент ИИ называли жалобой «отзыв 1–2★», а считают её
по LLM-разметке; ИИ видел старые темы на регулярках (review_topic); разбор темы
отдавал total = длину списка с потолком 40; ссылка на вкладку теряла признак,
площадку и период «с — по». Без сети и без БД: данные вкладки подменяются.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from urllib.parse import parse_qs

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from bank_audit.ai import agent_tools as T  # noqa: E402
from bank_audit.ai import analyst as A  # noqa: E402
from bank_audit.rag import review_codebook as cb  # noqa: E402
from bank_audit.rag import review_glossary as G  # noqa: E402
from bank_audit.rag import review_period as rp  # noqa: E402
from bank_audit.rag import review_slices as rs  # noqa: E402
from bank_audit.rag import reviews_dash as RD  # noqa: E402
from bank_audit.rag import reviews_work as RW  # noqa: E402
from bank_audit.web import reviews_export  # noqa: E402

STARS = ("1–2★", "1-2★", "1–2 ★", "звёзд 1")


def _qs(link: str) -> dict[str, str]:
    assert link.startswith("#reviews")
    q = link.split("?", 1)[1] if "?" in link else ""
    return {k: v[0] for k, v in parse_qs(q).items()}


# ── словарь ──────────────────────────────────────────────────────────────────

def test_complaint_is_annotation_not_stars():
    assert G.COMPLAINT_KINDS == cb.COMPLAINT_KINDS == ("complaint", "mixed")
    # счётчики вкладки берут ровно эти виды разметки
    for kind in G.COMPLAINT_KINDS:
        assert f"'{kind}'" in RD._CMP
    for text in (G.COMPLAINT, G.COMPLAINT_SHORT):
        assert not any(s in text for s in STARS)
        assert "смешан" in text and "ИИ" in text
    assert "не по оценке" in G.COMPLAINT or "а не по оценке" in G.COMPLAINT


def test_search_terms_follow_search_caps(monkeypatch):
    from bank_audit.rag import bankiru_fts
    monkeypatch.setattr(RD, "SENSE_CAP", 7)
    monkeypatch.setattr(bankiru_fts, "NEAR_CAP", 1500)
    assert "до 7 похожих по смыслу" in G.search_scope()
    assert "до 7 жалоб" in G.by_sense() and "не счёт" in G.by_sense()
    assert "1 500" in G.by_words() and "полный счёт" in G.by_words()


def test_slice_names_come_from_slices_module():
    text = G.slice_text()
    for label in rs.LABELS.values():
        assert f"«{label}»" in text
    assert "период «с — по»" in text


def test_terms_dictionary():
    assert set(G.terms()) == {"жалоба", "тема", "со словами запроса", "по смыслу", "срез",
                              "направление"}
    assert G.terms("тема") == {"тема": G.THEME}
    with pytest.raises(KeyError):
        G.term("звёзды")


# ── выгрузка в Excel ─────────────────────────────────────────────────────────

def _rows(n: int = 6) -> list[dict]:
    return [{"дата": f"2026-10-{1 + i:02d}", "банк": "Сбербанк", "площадка": "bankiru",
             "город": "Москва", "оценка": 1, "продукт": "Вклад",
             "главная проблема": "Проценты по вкладам и накопительным счетам", "группа": "",
             "доп. проблемы": "", "эскалация": "", "куда": "", "без согласия": "",
             "ввели в заблуждение": "", "уязвимый клиент": "", "сумма": "",
             "дата события": "", "вне кодификатора": "", "суть": "Суть", "цитата": "Цитата",
             "ссылка": f"https://www.banki.ru/r/{i}", "текст": "Текст отзыва"} for i in range(n)]


def _cells(body: bytes) -> dict[str, list[str]]:
    wb = load_workbook(io.BytesIO(body))
    return {ws.title: [str(c.value) for row in ws.iter_rows() for c in row if c.value]
            for ws in wb.worksheets}


def test_export_defines_complaint_like_the_tab():
    cells = _cells(reviews_export.to_xlsx(_rows(), {"bank": "Сбербанк", "days": 90}))
    everything = " ".join(v for vs in cells.values() for v in vs)
    assert not any(s in everything for s in STARS)
    assert any(G.COMPLAINT in v for v in cells["О выгрузке"])
    assert any(G.COMPLAINT in v for v in cells["Обзор"])
    assert G.THEME in cells["О выгрузке"]
    assert any(v.startswith("Поиска нет") for v in cells["О выгрузке"])


def test_export_with_search_says_what_was_found():
    f = {"bank": "Сбербанк", "days": 90, "q": "навязали страховку"}
    lines = reviews_export.method_lines(f)
    assert G.by_words() in lines and G.by_sense() in lines
    assert not any("в выгрузку не входит" in x for x in lines)    # так писали до 04.11
    flt = dict(reviews_export.describe_filters(f))
    assert flt["Поиск"] == f"«навязали страховку» — {G.search_scope()}"
    cells = _cells(reviews_export.to_xlsx(_rows(), f))
    assert G.by_words() in cells["О выгрузке"]


# ── ссылка на вкладку ────────────────────────────────────────────────────────

def test_link_keeps_flag_source_and_period():
    link = T.link_reviews("ВТБ", "Вклад", "deposit_interest", flag="esc:filed",
                          source="sravni_reviews", date_from="2026-05-01",
                          date_to="2026-10-31", city="Казань")
    p = _qs(link)
    assert p == {"tab": "complaints", "bank": "ВТБ", "product": "Вклад",
                 "from": "2026-05-01", "to": "2026-10-31", "theme": "deposit_interest",
                 "flag": "esc:filed", "city": "Казань", "source": "sravni"}


def test_link_period_is_always_written():
    # без периода в адресе вкладка взяла бы период из прошлого визита в этом браузере
    assert _qs(T.link_reviews(days=90)) == {"p": "90d"}
    assert _qs(T.link_reviews(theme="chargeback", days=7)) == {
        "tab": "complaints", "p": "7d", "theme": "chargeback"}
    assert T.link_reviews("ВТБ").startswith("#reviews?bank=")


def test_link_from_period_object():
    roll = rp.parse("30d")
    assert _qs(T.link_reviews(period=roll))["p"] == "30d"
    assert _qs(T.link_reviews(period=rp.parse("all")))["p"] == "all"
    rng = rp.parse(None, "2026-05-01", "2026-06-15")
    p = _qs(T.link_reviews(period=rng, days=90))
    assert (p["from"], p["to"]) == ("2026-05-01", "2026-06-15") and "p" not in p


def test_link_drops_what_the_tab_would_reject():
    p = _qs(T.link_reviews(theme="нет_такой", flag="drop table", source="vk.com", days=30))
    assert p == {"p": "30d"}
    assert _qs(T.link_reviews(esc=True, days=30))["esc"] == "1"
    assert _qs(T.link_reviews(source="banki.ru", days=30))["tab"] == "complaints"


def test_source_key():
    assert T.source_key("bankiru") == T.source_key("banki_reviews") == "banki"
    assert T.source_key("banki.ru") == T.source_key("www.banki.ru") == "banki"
    assert T.source_key("finuslugi_reviews") == "finuslugi"
    assert T.source_key("sravni") == "sravni"
    assert T.source_key("отзовик") is None and T.source_key(None) is None


def test_tool_period_matches_the_tab():
    per = T._period(None, 90)
    tab = rp.from_request("90d", None, None, None, None, data_start=RD.data_start_for(None))
    assert (per.t0, per.t1, per.kind) == (tab.t0, tab.t1, tab.kind)
    rng = T._period("Зарплатные проекты", 90, "2026-05-01", "2026-06-15")
    assert rng.info()["from"] == "2026-05-01" and rng.info()["to"] == "2026-06-15"
    with pytest.raises(ValueError):
        T._period(None, 90, "01.05.2026")
    with pytest.raises(ValueError):
        T._period(None, 90, None, "2026-06-15")


# ── инструменты ИИ ───────────────────────────────────────────────────────────

def _theme_fakes(monkeypatch, *, n_items=40, total=57, calls=None):
    calls = calls if calls is not None else {}
    monkeypatch.setattr(RD, "weekly_signals", lambda b, p=None: {
        "week_end": "2026-11-02", "signals": [{"key": "chargeback", "label": "Чарджбэк",
                                               "week": total}]})
    monkeypatch.setattr(RD, "week_pulse", lambda b, p=None: {})
    monkeypatch.setattr(RD, "signal_evidence", lambda b, k, p, d, lim: [
        {"url": f"u{i}", "date": "2026-11-01", "city": "Москва", "summary": "с", "quote": "ц",
         "esc": None} for i in range(min(n_items, lim))])
    monkeypatch.setattr(RD, "signal_evidence_total", lambda b, k, p, d: total)

    def feed(*a, **kw):
        calls["feed"] = kw
        return {"items": [{"url": "f1", "text": "текст жалобы", "date": "2026-10-20"}],
                "total": 321}
    monkeypatch.setattr(RD, "list_reviews_ex", feed)
    monkeypatch.setattr(RW, "reviews_by_urls", lambda urls: [])

    def clusters(*a, **kw):
        calls["clusters"] = (a, kw)
        return {"clusters": []}
    monkeypatch.setattr(RW, "clusters", clusters)
    return calls


def test_theme_total_is_real_count_not_list_length(monkeypatch):
    _theme_fakes(monkeypatch, n_items=40, total=57)
    res = json.loads(T.tool_complaint_theme("chargeback"))
    assert res["total"] == 57 and res["shown"] == 40
    assert res["sample"].startswith("показано 40 из 57")
    # ссылка — на жалобы самой недели сигнала (неделя по 02.11 включительно): month=sig:…
    # — те же жалобы, что в числе, с отсевом старых событий; периода «с — по» нет
    p = _qs(res["link"])
    assert (p["month"], p["theme"]) == ("sig:2026-11-02", "chargeback") and "from" not in p
    assert _qs(res["weekly_signal"]["link"])["month"] == "sig:2026-11-02"
    assert not any(s in res["counting"] for s in STARS)


def test_theme_over_period_counts_like_the_tab(monkeypatch):
    calls = _theme_fakes(monkeypatch)
    res = json.loads(T.tool_complaint_theme("chargeback", days=30))
    assert res["total"] == 321 and res["shown"] == 1
    assert res["sample"].startswith("показано 1 из 321")
    per = calls["feed"]["period"]
    assert per.kind == "rolling" and per.n == 30 and "days" not in calls["feed"]
    assert calls["clusters"][1]["period"] is per
    assert _qs(res["link"])["p"] == "30d"
    # период «с — по» вместо недели сигнала
    res = json.loads(T.tool_complaint_theme("chargeback", date_from="2026-05-01",
                                            date_to="2026-05-31"))
    p = _qs(res["link"])
    assert (p["from"], p["to"]) == ("2026-05-01", "2026-05-31") and res["days"] == 31
    assert json.loads(T.tool_complaint_theme("chargeback", date_from="май"))["error"]


def test_overview_counting_flags_and_sources(monkeypatch):
    seen = {}

    def ov(bank, product=None, days=90, period=None):
        seen["period"] = period
        return {"bank": "Сбербанк", "total": 1000, "by_source": [
            {"source": "bankiru", "n": 700}, {"source": "banki_reviews", "n": 50},
            {"source": "sravni_reviews", "n": 200}, {"source": "что-то", "n": 3}]}
    monkeypatch.setattr(RD, "overview", ov)
    monkeypatch.setattr(RD, "themes", lambda *a, **k: {"themes": [
        {"key": "chargeback", "label": "Чарджбэк", "n": 40, "pct": 4.0}]})
    monkeypatch.setattr(RD, "risk_flags", lambda *a, **k: {"groups": [
        {"label": "Эскалация", "items": [{"flag": "esc:filed", "label": "уже обратились",
                                          "n": 12, "pct": 1.2}]}]})
    monkeypatch.setattr(RD, "geo", lambda *a, **k: {"cities": []})
    monkeypatch.setattr(RD, "changes", lambda *a, **k: {"items": []})
    monkeypatch.setattr(RD, "vs_market", lambda *a, **k: {"rows": []})
    res = json.loads(T.tool_complaints_overview(days=90))
    assert seen["period"].n == 90
    assert not any(s in res["counting"] for s in STARS)
    assert set(res["glossary"]) == {"тема", "срез"}
    assert _qs(res["link"]) == {"p": "90d"}
    flag = res["risk_flags"][0]
    assert _qs(flag["link"])["flag"] == "esc:filed" and flag["flag"] == "уже обратились"
    src = {x["source"]: x for x in res["by_source"]}
    assert src["banki.ru"]["n"] == 750                      # одна площадка, как фильтр вкладки
    assert _qs(src["sravni.ru"]["link"])["source"] == "sravni"
    assert "link" not in src["что-то"]
    assert _qs(res["themes"][0]["link"])["theme"] == "chargeback"


def test_search_uses_tab_period_and_explains_counts(monkeypatch):
    seen = {}

    def feed(*a, **kw):
        seen.update(kw)
        return {"items": [], "total": 25, "search": {"n_words": 5, "n_sense": 20}}
    monkeypatch.setattr(RD, "list_reviews_ex", feed)
    res = json.loads(T.tool_complaint_search("страховка", days=180))
    assert seen["period"].n == 180 and "days" not in seen
    assert (res["by_words"], res["by_sense"], res["total"]) == (5, 20, 25)
    assert set(res["glossary"]) == {"со словами запроса", "по смыслу"}
    p = _qs(res["link"])
    assert (p["q"], p["p"], p["tab"]) == ("страховка", "180d", "complaints")


# ── старые таблицы закрыты ───────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "select topic, count(*) from review_topic group by 1",
    "SELECT * FROM v_review_topics",
    "select * from public.review_sentiment",
    'select * from "review_topic_label" l join review r using(url)',
    "select * from review_summary",
])
def test_agent_sql_refuses_stale_review_markup(sql):
    res = json.loads(T.tool_sql(sql))
    assert "старая разметка жалоб" in res["error"] and "complaints_overview" in res["error"]


def test_stale_pattern_keeps_current_tables():
    for ok in ("select * from review_summary_vec", "select * from review_index",
               "select * from review_annotation", "select * from review"):
        assert T.STALE_REVIEW_RELATIONS.search(ok) is None


def test_analyst_sql_whitelist_has_no_stale_markup():
    for rel in ("review_topic", "v_review_topics", "review_sentiment", "v_review_sentiment_share"):
        assert rel not in A._ALLOWED_RELATIONS
        err = A._validate_sql(f"select * from {rel} limit 5")
        assert err and "старая разметка жалоб" in err
    assert A._validate_sql("select * from bank limit 5") is None
    spec = next(t for t in A.TOOLS if t["function"]["name"] == "run_sql")
    assert "review_topic" not in spec["function"]["description"]


def test_analyst_review_tools_use_tab_numbers(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "tool_complaints_overview",
                        lambda *a, **k: calls.append((a, k)) or json.dumps({"ok": 1}))
    assert json.loads(A._run_tool("get_reviews_analysis", {"bank_slug": "sberbank"})) == {"ok": 1}
    assert calls[-1] == (("sberbank", None, 90), {"date_from": None})
    A._run_tool("get_review_themes", {"bank_slug": "vtb", "period": "all"})
    assert calls[-1][1]["date_from"] == RD.data_start_for(None).isoformat()
    monkeypatch.setattr(RD, "vs_market", lambda *a, **k: {"rows": [
        {"bank": "Сбербанк", "n": 10, "pct": 50.0, "is_target": True}]})
    res = json.loads(A._run_tool("get_reviews_analysis", {"bank_slug": "all"}))
    assert res["banks_by_complaints"][0]["n"] == 10 and _qs(res["link"])["p"] == "90d"


# ── подсчёт выборки сигнала ──────────────────────────────────────────────────

def test_signal_evidence_total_counts_the_signal_sample(monkeypatch):
    log = []

    class _S:
        def execute(self, stmt, params=None):
            log.append((str(stmt), params))
            return type("R", (), {"scalar": lambda self: 57})()

    @contextlib.contextmanager
    def session():
        yield _S()
    monkeypatch.setattr(RD, "resolve_bank", lambda b: "Сбербанк")
    monkeypatch.setattr(RD.db, "session", session)
    assert RD.signal_evidence_total("Сбербанк", "chargeback", None, 7) == 57
    sql, params = log[-1]
    assert "count(*)" in sql and RD._EVIDENCE_WHERE in sql
    assert params == {"bank": "Сбербанк", "key": "chargeback", "product": None, "d": 7}
    assert RD.signal_evidence_total("Сбербанк", "нет_такой") is None


# ── вкладка читает площадку из адреса ────────────────────────────────────────

def test_reviews_page_reads_source_from_address():
    from pathlib import Path
    jsx = (Path(__file__).resolve().parents[1] / "src/bank_audit/web/static/app.jsx"
           ).read_text(encoding="utf8")
    assert "useState(()=>rvSrcOk(P.source))" in jsx
    assert 'if(fSrc)sp.set("source",fSrc)' in jsx
    assert "поиск в выгрузку не входит" not in jsx
