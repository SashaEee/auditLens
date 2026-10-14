"""Срезы «Аудита отзывов» вне справочника продуктов (rag/review_slices.py): БД и сеть не нужны."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import review_slices as RS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_flags_parsing_is_strict():
    assert RS.flags({"payroll": True, "us": "false", "field": "true", "quote": "x"}) == \
        {"payroll": True, "us": False, "field": True, "quote": "x"}
    assert RS.flags({"payroll": True, "us": False}) is None           # нет поля — ответ негоден
    assert RS.flags(None) is None and RS.flags("text") is None


def test_merge_agreement_and_disputes():
    a = {"payroll": True, "us": False, "field": False}
    b = {"payroll": True, "us": True, "field": False}
    final, disputed = RS.merge(a, b)
    assert final == {"payroll": True, "field": False} and disputed == ["us"]
    final, disputed = RS.merge(a, None)                               # один голос — спорно всё
    assert disputed == list(RS.KEYS) and final["payroll"] is True
    assert RS.tags_of({"payroll": True, "us": False, "field": True}) == ["payroll", "field"]


def test_candidates_cover_every_slice():
    sql = RS.candidate_sql()
    for k, q in RS._TSQ.items():
        assert q in sql, k
    assert "'atm'" in sql and "cash_atm" in sql and "'courier'" in sql


def test_slice_labels_match_sql_function():
    """Название среза в списке продуктов и в al_product_is — одно и то же:
    иначе пункт в списке есть, а фильтр по нему пуст."""
    sql = (ROOT / "migrations" / "099_review_slices.sql").read_text(encoding="utf-8")
    for key, (label, _definition) in RS.SLICES.items():
        assert f"'{label}'" in sql and f"'{key}'" in sql, key


def test_system_prompt_has_definitions_and_negatives():
    for key, (_label, definition) in RS.SLICES.items():
        assert definition in RS.SYSTEM
        assert f"НЕ {key}" in definition                              # у каждого среза — чего НЕ относить


def test_strong_subset_selection():
    """Сильным моделям — только отзывы с признаком: дешёвый проход сказал «да» или в
    тексте «зарплатн…», «з/п», «аванс», «отпускные», «реестр» (замер: 34 из 34)."""
    assert RS.needs_strong("payroll", "Мне перестали платить зарплатную льготу", None, None, None)
    assert RS.needs_strong("payroll", "не пришёл аванс", None, None, None)
    assert RS.needs_strong("payroll", "Банк вернул деньги", {"payroll": True}, None, None)
    assert not RS.needs_strong("payroll", "Отказали в кредите, доход 80 тыс.", {"payroll": False},
                               {"payroll": False}, None)
    for clause in ("(1)", "(2)", "(3)", "(а)", "(б)", "(в)", "(г)"):
        assert clause in RS._STRONG_SYS["payroll"], clause


def test_strong_ensemble_majority_and_own_prompts():
    """Две сильные модели, при расхождении — третья; у разметки и у сильных — свои
    клиенты (14.10 подмена общей подсказки ломала параллельные пакеты)."""
    import asyncio

    class Fake:
        def __init__(self, sys_, answers):
            self.sys, self.seen, self.answers = sys_, [], answers

        async def ask(self, model, user):
            self.seen.append((self.sys, model))
            await asyncio.sleep(0)
            n = user.count("\n[") + 1
            if self.sys == RS.SYSTEM:
                return {"items": [{"n": k + 1, "payroll": False, "us": False, "field": False}
                                  for k in range(n)]}, 10, 10
            vals = self.answers[model]
            return {"items": [{"n": k + 1, "payroll": vals[k]} for k in range(len(vals))]}, 10, 10

    items = [{"url": f"u{k}", "bank": "Банк", "text": t} for k, t in enumerate(
        ["Не пришёл аванс", "Зарплатная карта: перевод по СБП", "Отказали в ипотеке"])]
    c = Fake(RS.SYSTEM, {})
    # первые два — с признаком; A и B согласны по первому, расходятся по второму → решает TIE
    cs = {"payroll": Fake(RS._STRONG_SYS["payroll"], {RS.STRONG_A: [True, True], RS.STRONG_B: [True, False],
                                                      RS.STRONG_TIE: [False]})}

    async def run():
        import bank_audit.db as db
        orig = db.session

        class S:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): return None
        db.session = lambda: S()
        try:
            return await RS._process(c, items, cs)
        finally:
            db.session = orig
    rows, _ = asyncio.run(run())
    tags = {r["url"]: r["tags"] for r in rows}
    assert tags == {"u0": ["payroll"], "u1": [], "u2": []}
    assert {sys_ for sys_, _m in c.seen} == {RS.SYSTEM}
    assert {sys_ for sys_, _m in cs["payroll"].seen} == {RS._STRONG_SYS["payroll"]}
    assert [m for _s, m in cs["payroll"].seen].count(RS.STRONG_TIE) == 1   # третья — только по спорному
