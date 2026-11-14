"""«Пульс»: сводка по отчётам — задачи, время шагов, обрывы."""
from bank_audit.ai import report_stats as R


def test_summary_counts_tasks_steps_and_broken_reports():
    rows = [
        {"task": "product", "elapsed_s": 250, "timings": {"plan": 6, "collect": 50, "facts": 34, "write": 119}, "broken": False},
        {"task": "product", "elapsed_s": 300, "timings": {"plan": 8, "collect": 70, "facts": 40, "write": 150}, "broken": True},
        {"task": "market_data", "elapsed_s": 187, "timings": {"plan": 6, "collect": 21, "facts": 77, "write": 73}, "broken": False},
        {"task": None, "elapsed_s": 294, "timings": None, "broken": False},      # отчёт до 11.11
    ]
    out = R.summarize(rows, 30)
    assert out["reports"] == 4 and out["timed"] == 3 and out["broken"] == 1
    assert out["median_s"] == 272.0
    tasks = {t["key"]: t for t in out["tasks"]}
    assert tasks["product"]["n"] == 2 and tasks["product"]["median_s"] == 275.0
    assert tasks["product"]["label"] == "Всё о продукте" and tasks[""]["label"] == "Не определена"
    steps = {s["key"]: s["median_s"] for s in out["steps"]}
    assert steps["plan"] == 6 and steps["facts"] == 40 and steps["critic"] == 0


def test_no_timed_reports_gives_empty_steps():
    out = R.summarize([{"task": None, "elapsed_s": 294, "timings": None, "broken": False}], 30)
    assert out["steps"] == [] and out["timed"] == 0
    assert R.summarize([], 30)["median_s"] is None


def test_typical_answer_uses_own_durations_when_enough():
    """Строка хода работы говорит «обычно около N с» по недавним ответам, а не по догадке."""
    rows = [("hermes", s) for s in (10, 12, 14, 20, 22, 26, 30, 44, 60)] + [("deck", 130)]
    out = R.typical_answer_from(rows)
    assert out["answer"] == {"p50": 22, "p75": 30, "n": 9}
    # презентаций мало — запасные значения, и видно, что они запасные (n = 0)
    assert out["deck"]["n"] == 0 and out["deck"]["p50"] == 140


def test_typical_answer_ignores_garbage_and_falls_back():
    out = R.typical_answer_from([("hermes", None), ("hermes", "x"), ("hermes", 0), ("hermes", 99999)])
    assert out["answer"]["n"] == 0 and out["answer"]["p50"] == 30
    assert R.typical_answer_from(None)["deck"]["p75"] == 190
    deck = R.typical_answer_from([("deck", s) for s in (100, 110, 120, 130, 140, 150, 160, 170)])
    assert deck["deck"] == {"p50": 135, "p75": 152, "n": 8}


def test_typical_answer_never_raises_without_database(monkeypatch):
    R._answer_cache.update(at=0.0, val=None)

    def boom():
        raise RuntimeError("нет базы")
    monkeypatch.setattr(R.db, "session", boom)
    out = R.typical_answer()
    assert out["answer"]["n"] == 0 and out["deck"]["n"] == 0
    R._answer_cache.update(at=0.0, val=None)
