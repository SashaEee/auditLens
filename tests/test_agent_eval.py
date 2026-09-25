"""Регрессионный набор ИИ-аналитика: разбор чисел, проверки ответа, итог прогона."""
from __future__ import annotations

import asyncio

from bank_audit.ai import agent_eval as E


def test_numbers_in_russian_text():
    xs = E.numbers_in("2 419 жалоб, эскалация 20,7% против 19,8%, ×4,2, 33.0 балла, 17-е")
    for v in (2419, 20.7, 19.8, 4.2, 33.0, 17):
        assert v in xs
    assert E.has_number("ставка 19% годовых", 19.0, 0.05)
    assert not E.has_number("ставка 18,9%", 19.0, 0.05)


def _spec(**kw):
    return {"numbers": [], "words": [], **kw}


def test_forbidden_is_hard_fail():
    a = "Смотрите [тут](http://127.0.0.1:8000/#reviews). Тема chargeback растёт."
    checks = E.check_answer(a, _spec(), 20)
    bad = {c["check"] for c in checks if not c["ok"]}
    assert "нет: внутренний адрес" in bad and "нет: служебный ключ темы" in bad
    assert E.verdict(checks, None) == "fail"


def test_bypass_advice_forbidden():
    checks = E.check_answer("Можно обойти защиту Cloudflare через r.jina.ai", _spec(), 5)
    assert E.verdict(checks, None) == "fail"


def test_stub_is_fail():
    assert E.verdict(E.check_answer("Давай разберусь детальнее.", _spec(), 5), None) == "fail"
    assert E.verdict(E.check_answer("Нет данных.", _spec(), 5), None) == "fail"


def test_numbers_and_words():
    spec = _spec(numbers=[("жалоб", 15, 1), ("норма", 3.6, 0.1)],
                 words=["Оспаривание операций и возвраты (чарджбэк)"], min_words=1)
    good = ("**Всплеск оспаривания операций:** 15 жалоб за неделю при норме ~3,6. "
            "Сюжет — билеты на отменённый концерт.")
    assert E.verdict(E.check_answer(good, spec, 30), {"score": 5}) == "pass"
    vague = "Клиенты жалуются на возвраты, их стало больше."
    assert E.verdict(E.check_answer(vague, spec, 30), {"score": 3}) == "fail"


def test_judge_downgrades():
    spec = _spec(numbers=[("место", 1, 0)])
    checks = E.check_answer("Сбер на 1-м месте.", spec, 10)
    assert E.verdict(checks, {"score": 5}) == "pass"
    assert E.verdict(checks, {"score": 3}) == "partial"
    assert E.verdict(checks, {"score": 5, "hallucination": True}) == "fail"


def test_slow_is_partial():
    checks = E.check_answer("Сбер на 1-м месте.", _spec(numbers=[("место", 1, 0)]), 999)
    assert E.verdict(checks, None) == "partial"


def test_summarize():
    s = E.summarize([{"verdict": "pass", "seconds": 10}, {"verdict": "partial", "seconds": 30},
                     {"verdict": "fail", "seconds": 50}, {"verdict": "skip"}])
    assert s == {"score": 50.0, "n_pass": 1, "n_partial": 1, "n_fail": 1, "median_s": 30}


def test_run_case_with_fake_agent(monkeypatch):
    async def fake_ask(question, model, hint):
        return {"answer": "Сбер — **1-е место из 134**, «Выгодный старт +» 19%.",
                "tools": ["Позиция на рынке"], "meta": {}, "error": None, "seconds": 12.0}
    monkeypatch.setattr(E, "_ask", fake_ask)
    case = E.Case("X", "Рынок", "место", lambda: {
        "question": "место?", "numbers": [("место", 1, 0), ("банков", 134, 0), ("ставка", 19, 0.05)],
        "words": ["Выгодный старт +"], "min_words": 1, "facts": {}})
    r = asyncio.run(E.run_case(case, None, False, "t"))
    assert r["verdict"] == "pass" and r["tools"] == ["Позиция на рынке"]


def test_case_ids_unique():
    ids = [c.id for c in E.CASES]
    assert len(ids) == len(set(ids)) >= 14
