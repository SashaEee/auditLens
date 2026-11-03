"""Срезы «Аудита отзывов» вне справочника продуктов (rag/review_slices.py): БД и сеть не нужны."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import review_slices as RS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _latest_product_fn() -> str:
    """Последняя миграция, где определена al_product_is, — она и действует на проде."""
    files = sorted(p for p in (ROOT / "migrations").glob("[0-9]*.sql")
                   if "FUNCTION al_product_is" in p.read_text(encoding="utf-8"))
    return files[-1].read_text(encoding="utf-8")


def test_flags_parsing_is_strict():
    got = RS.flags({"slices": [{"key": "field", "clause": "1", "quote": "курьер не приехал"},
                               {"key": "nonsense", "clause": "1"}],
                    "excluded": {"us": "а", "field": "б"}})
    assert got["field"] is True and not any(got[k] for k in RS.KEYS if k != "field")
    assert got["evidence"] == {"field": {"clause": "1", "quote": "курьер не приехал"}}
    assert got["excluded"] == {"us": "а"}                 # «НЕ» у включённого среза не держим
    assert RS.flags({"slices": []})["field"] is False     # пустой список — ни к одному срезу
    assert RS.flags({"payroll": True}) is None            # старый формат / без slices — нет ответа
    assert RS.flags(None) is None and RS.flags("text") is None
    assert RS.tags_of({"payroll": True, "us": False, "field": True}) == ["payroll", "field"]


def _f(**on):
    return {**{k: False for k in RS.KEYS}, **on,
            "evidence": {k: {"clause": "1", "quote": k} for k, v in on.items() if v}, "excluded": {}}


def test_decide_cascade():
    # B не спрашивали (отрицательный A вне контрольной доли) — решает A
    final, ev, st = RS.decide(_f(), None, None)
    assert st == "single" and not any(final.values()) and ev == {}
    # A и B согласны
    final, ev, st = RS.decide(_f(field=True), _f(field=True), None)
    assert st == "agree" and final["field"] and ev["field"]["clause"] == "1"
    # разошлись — большинство из трёх
    final, ev, st = RS.decide(_f(field=True, us=True), _f(field=True), _f(us=False))
    assert st == "arbitrated" and final["field"] and not final["us"]
    final, ev, st = RS.decide(_f(us=True), _f(), _f(us=True))
    assert final["us"] and "us" in ev
    # третья молчит — осторожное «нет» и повтор следующим прогоном
    final, ev, st = RS.decide(_f(us=True), _f(), None)
    assert st == "failed_tie" and not final["us"]
    assert RS.decide(None, None, None)[2] == "failed"


def test_confirm_negative_share_is_stable():
    urls = [f"https://x/{i}" for i in range(4000)]
    picked = [u for u in urls if RS.confirm_negative(u)]
    assert 0.07 < len(picked) / len(urls) < 0.13
    assert picked == [u for u in urls if RS.confirm_negative(u)]       # детерминизм


def test_clip_keeps_head_and_tail():
    t = "начало " + "x" * 9000 + " курьер так и не приехал"
    c = RS.clip(t)
    assert c.startswith("начало") and c.endswith("курьер так и не приехал") and "[…]" in c
    assert RS.clip("коротко") == "коротко"


def test_candidates_cover_every_slice():
    sql = RS.candidate_sql()
    for k, q in RS._TSQ.items():
        assert q in sql, k
        assert RS._key_candidates(k).startswith("(")
    assert "'atm'" in sql and "cash_atm" in sql and "'courier'" in sql


def test_slice_labels_match_sql_function():
    """Название среза в списке продуктов и в al_product_is — одно и то же:
    иначе пункт в списке есть, а фильтр по нему пуст."""
    sql = _latest_product_fn()
    for key, (label, _definition) in RS.SLICES.items():
        assert f"want = '{label}' AND '{key}'" in sql, key


def test_system_prompt_has_definitions_and_negatives():
    assert "КАЖДОМУ срезу" in RS.SYSTEM and "три флага" not in RS.SYSTEM
    for key, (_label, definition) in RS.SLICES.items():
        assert definition in RS.SYSTEM
        assert f"НЕ {key}" in definition                              # у каждого среза — чего НЕ относить
        assert re.search(r"ДА, если", definition), key


def test_migration_keeps_versions_and_registry():
    sql = (ROOT / "migrations" / "102_review_slices_registry.sql").read_text(encoding="utf-8")
    assert "lock_timeout" in sql and "PRIMARY KEY (url, schema)" in sql
    assert "review_slice_active" in sql and "review_slice_feedback" in sql
    for key in RS.KEYS:
        assert f"('{key}', 's" in sql, key                           # у каждого среза — активная версия


class _Caller:
    """Подставная модель: отвечает заданным по модели и пишет, кого спрашивали."""

    def __init__(self, answers):
        self.answers, self.seen, self.sys = answers, [], None

    async def ask(self, model, user):
        n = len(re.findall(r"^\[\d+\] ", user, flags=re.M))
        self.seen.append((model, n))
        return {"items": self.answers[model][:n] if isinstance(self.answers[model], list)
                else self.answers[model](n)}, 1000, 100


def test_process_cascade_asks_only_what_changes(monkeypatch):
    items = [{"url": f"https://banki/{i}", "bank": "Банк", "text": f"Курьер не привёз карту {i}. " * 3}
             for i in range(3)]
    # A: первый — выездной, второй — УС, третий — ничего; B: подтверждает выездной, УС отвергает
    a = [{"n": 1, "slices": [{"key": "field", "clause": "1", "quote": "Курьер не привёз карту 0"}]},
         {"n": 2, "slices": [{"key": "us", "clause": "1", "quote": "банкомат"}]},
         {"n": 3, "slices": []}]
    b = lambda n: ([{"n": 1, "slices": [{"key": "field", "clause": "1", "quote": "Курьер не привёз"}]},
                    {"n": 2, "slices": []}] + [{"n": 3, "slices": []}])[:n]        # noqa: E731
    tie = lambda n: [{"n": 1, "slices": []}][:n]                                    # noqa: E731
    c = _Caller({RS.STRONG_A: a, RS.STRONG_B: b, RS.STRONG_TIE: tie})
    monkeypatch.setattr(RS, "confirm_negative", lambda url, share=0.1: False)
    written = []

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, rows=None): written.extend(rows or [])
    monkeypatch.setattr(RS.db, "session", lambda: S())
    rows, rub = asyncio.run(RS._process(c, items))
    tags = {r["url"][-1]: r["tags"] for r in rows}
    assert tags == {"0": ["field"], "1": [], "2": []}
    assert [m for m, _n in c.seen] == [RS.STRONG_A, RS.STRONG_B, RS.STRONG_TIE]
    assert dict(c.seen)[RS.STRONG_B] == 2          # B — только положительные A
    assert dict(c.seen)[RS.STRONG_TIE] == 1        # третья — только спорный отзыв
    ev = json.loads(rows[0]["evidence"])
    assert ev["field"]["clause"] == "1" and ev["field"]["quote_ok"] is True
    assert rows[1]["status"] == "arbitrated" and rows[2]["status"] == "single"
    assert rub > 0 and written


def test_coverage_shape(monkeypatch):
    class R:
        def __init__(self, v): self.v = v
        def one(self): return self.v
        def all(self): return [("payroll", "s6")]

    class S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, p=None):
            return R((100, 99)) if "count(*)" in str(sql) else R(None)
    monkeypatch.setattr(RS.db, "session", lambda: S())
    cov = RS.coverage()
    assert set(cov["keys"]) == set(RS.KEYS) and cov["keys"]["payroll"]["schema"] == "s6"
    assert cov["keys"]["payroll"]["ready"] is True and cov["ready"] is True
