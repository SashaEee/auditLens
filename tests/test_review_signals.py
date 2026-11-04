"""Сигналы жалоб недели: поправка по группам, две недели подряд, удержание.

30.09 всплеск жалоб на чарджбэк (12–14 при норме 3,4, вторую неделю подряд)
пропал из «Обзора»: поправку делили на все 39 проблем. БД не нужна — всё ниже
чистые функции над числами, weekly_signals — с подменёнными счётчиками.
"""
import pytest

from bank_audit.rag import reviews_dash as rd

# нормы проблем Сбера в неделю на 30.09 (39 проблем кодификатора)
NORMS = [0.1, 0.3, 0.3, 0.4, 0.4, 0.4, 0.6, 1.0, 1.1, 1.1, 1.1, 1.3, 1.3, 1.3, 1.6, 1.7,
         2.1, 2.3, 2.9, 2.9, 2.9, 3.9, 4.0, 4.4, 4.6, 4.9, 5.3, 5.4, 5.6, 5.9, 5.9, 6.0,
         7.9, 9.1, 10.9, 11.6, 12.6, 12.7]
# чарджбэк на 30.09: недели 0..8 (0 — последняя)
CHARGEBACK = [12, 10, 4, 6, 2, 0, 5, 5, 2]


def _calm_weeks() -> dict[str, list[int]]:
    """Остальные проблемы — обычная неделя, ровно на норме."""
    out = {}
    for n, bw in enumerate(NORMS):
        base = [round(bw * (j + 1)) - round(bw * j) for j in range(7)]   # сумма ≈ 7·bw
        out[f"t{n}"] = [round(bw), round(bw)] + base
    return out


def _daily(weeks_newest_first: list[int]) -> list[int]:
    """Недельные счётчики → дни (равномерно, остаток — в первые дни недели)."""
    days = []
    for c in weeks_newest_first:
        days += [c // 7 + (1 if j < c % 7 else 0) for j in range(7)]
    return days


def test_nb_tail_sum_behaves():
    flat = [3, 4, 3, 3, 4, 3, 4]
    assert rd._nb_tail_sum(7, flat) > 0.3              # две обычные недели
    assert rd._nb_tail_sum(22, flat) < 0.001           # 11 и 11 при норме 3,4
    wavy = [0, 8, 1, 7, 0, 9, 0]                       # волны длиннее недели
    assert rd._nb_tail_sum(22, wavy) > rd._nb_tail_sum(22, flat)
    # недельный хвост не изменился
    assert rd._nb_tail(25, [10, 11, 9, 10, 12, 8, 10]) < 0.001


def test_weighted_simes():
    # одна неделя: цена объединения — ×4/3, не ×2
    assert rd._simes2(0.001, 0.5) == pytest.approx(0.001 / 0.75)
    # затяжной рост без недельного всплеска: ×4 на двухнедельную проверку
    assert rd._simes2(0.3, 0.001) == pytest.approx(0.004)
    # обе малы — не больше большей
    assert rd._simes2(0.001, 0.0002) <= 0.001
    assert rd._simes2(1.0, 1.0) == 1.0


def test_weeks_at_matches_weekly_grouping():
    daily = list(range(90))
    w = rd._weeks_at(daily, 3)
    assert w[0] == sum(daily[3:10]) and w[2] == sum(daily[17:24]) and len(w) == 9


def test_chargeback_passes_on_real_weeks_with_v6():
    """Чарджбэк 23.09 и 30.09 на настоящих недельных счётчиках всех проблем Сбера
    (tests/data, только числа): правило V6 (предсказательные хвосты с приором среза)
    его ловит и не поднимает спокойные проблемы. На синтетике с ровным фоном
    (CHARGEBACK ниже) V6 строже — это осознанный размен: ложных эпизодов в 3–4 раза
    меньше (замер 04.11.2026)."""
    import json
    from pathlib import Path
    real = json.loads((Path(__file__).parent / "data" / "signal_weeks_sber_0923_0930.json").read_text())
    for day in ("2026-09-23", "2026-09-30"):
        ent = rd._signal_entries(real[day])
        assert set(ent) == {"chargeback"}, day
        assert ent["chargeback"]["fam"] == "main" and ent["chargeback"]["q"] < rd._SIG_ALPHA["main"]
    assert rd._signal_entries(real["2026-09-30"])["chargeback"]["sustained"]      # две недели подряд
    # прежнее правило без поправки на группы отсекало синтетический случай (q ≈ 0,08)
    weeks = {**_calm_weeks(), "chargeback": CHARGEBACK}
    old_q = rd._bh({k: rd._nb_tail_v1(w[0], w[2:9]) for k, w in weeks.items()})
    assert old_q["chargeback"] > 0.05


def test_rare_theme_jump_and_small_noise():
    weeks = _calm_weeks()
    weeks["t0"] = [9, 0, 0, 0, 1, 0, 0, 0, 0]         # почти не было — и сразу 9
    weeks["t1"] = [4, 1, 0, 1, 0, 0, 1, 0, 0]         # 4 при норме 0,3 — мало для сигнала
    ent = rd._signal_entries(weeks)
    assert "t0" in ent and ent["t0"]["fam"] == "rare"
    assert "t1" not in ent


def test_one_week_spike_is_not_harder_than_before():
    """Недельный порог не строже прежнего: вес недели 3/4 при бюджете 4%
    и 23 частых проблемах ≈ 0,05 / 39."""
    n_main = sum(1 for bw in NORMS if bw >= rd._SIG_MIN_BASE)
    new_thr = rd._SIG_ALPHA["main"] * rd._SIG_W_WEEK / (n_main + 1)
    assert new_thr >= 0.05 / (len(NORMS) + 1) * 0.95


def test_hold_keeps_signal_when_norm_absorbs_spike():
    """Четыре недели по 10 при прежней норме 3: сегодняшняя норма уже
    впитала всплеск, а сигнал держится против нормы до всплеска."""
    daily = {"x": _daily([10, 10, 10, 10] + [3] * 9)}
    today = {"x": rd._weeks_at(daily["x"], 0)}
    assert "x" not in rd._signal_entries(today)       # без удержания — пропал бы
    ch = rd._signal_chain(daily, ["x"], lookback=21)
    assert "x" in ch and ch["x"]["since"] == 21
    assert sum(ch["x"]["base"]) / 7 == pytest.approx(3.0)


def test_signal_ends_when_complaints_return_to_norm():
    daily = {"x": _daily([3, 3, 14, 14] + [3] * 11)}
    assert "x" not in rd._signal_chain(daily, ["x"], lookback=21)
    # а две недели назад, на всплеске, сигнал был
    assert "x" in rd._signal_chain(daily, ["x"], lookback=21, a0=14)


def _patch_counts(monkeypatch, daily: dict[str, list[int]], topics: list[dict]):
    tw = {}
    for t in topics:
        arr = rd._weeks_at(daily.get(t["key"]), 0)
        tw.update({f'{t["key"]}_w0': arr[0], f'{t["key"]}_w1': arr[1],
                   f'{t["key"]}_b': sum(arr[2:9]), f'{t["key"]}_wk': arr})
    tw.update({"_tw0": 150, "_tb": 7 * 150})

    def week_counts(bank, product, exclude_bank=None, source=None):
        if exclude_bank:                               # рынок без банка: ровный
            return topics, {**{f'{t["key"]}_w0': 70 for t in topics},
                            **{f'{t["key"]}_b': 490 for t in topics}, "_tw0": 1500, "_tb": 10500}
        return topics, tw

    def boom():
        raise RuntimeError("нет БД")

    monkeypatch.setattr(rd, "resolve_bank", lambda b: "Сбербанк")
    monkeypatch.setattr(rd, "_topic_week_counts", week_counts)
    monkeypatch.setattr(rd, "_topic_day_counts", lambda bank, product, days: daily)
    monkeypatch.setattr(rd, "week_end", lambda: "2026-09-29")
    monkeypatch.setattr(rd, "_cached", lambda key, fn, ttl=0: fn())
    monkeypatch.setattr(rd.db, "session", boom)


def test_weekly_signals_marks_continuing(monkeypatch):
    topics = [{"key": "chargeback", "label": "Чарджбэк", "short": "Чарджбэк",
               "risk": "conduct", "group": "cards"},
              {"key": "calm", "label": "Спокойная", "short": "Спокойная",
               "risk": "ops", "group": "other"}]
    daily = {"chargeback": _daily([13, 11, 4, 4, 3, 3, 4, 3, 3, 3, 3, 3]),
             "calm": _daily([5] * 12)}
    _patch_counts(monkeypatch, daily, topics)
    res = rd.weekly_signals("Сбербанк")
    assert res and [s["key"] for s in res["signals"]] == ["chargeback"]
    s = res["signals"][0]
    assert s["status"] == "continuing" and s["days_on"] > 1
    assert s["since"] < "2026-09-30" and s["sustained"]
    assert s["baseline_before"] is not None and s["test"] in ("week", "two_weeks", "hold")
    assert s["bank_specific"] and s["market_flat"]
    # подпись для текстов
    note = rd.signal_note(s)
    assert note.startswith("держится с ") and "две недели подряд" in note


def test_texts_call_continuing_signal_a_continuation():
    from bank_audit.digest import writer
    from bank_audit.rag import reviews_llm
    s = {"key": "chargeback", "label": "Чарджбэк", "short": "Чарджбэк", "week": 12,
         "prev_week": 10, "baseline_week": 3.4, "ratio": 3.5, "level": "medium",
         "status": "continuing", "since": "2026-09-24", "days_on": 7,
         "baseline_before": 3.6, "sustained": True, "test": "week",
         "market_ratio": 1.55, "bank_specific": True}
    lines, _ = reviews_llm.signal_lines({"signals": [s], "overall": {}})
    assert "держится с 24.09" in lines[0] and "продолжение" in lines[0]
    assert "держится с 24.09" in writer._provenance("review_spike", s)
    assert writer._ai_prompt("review_spike", s).startswith("Разбери затяжной рост")
    fresh = {**s, "status": "new", "sustained": False}
    assert rd.signal_note(fresh) is None
    assert writer._ai_prompt("review_spike", fresh).startswith("Разбери всплеск")


def test_week_pulse_faster_than_market_needs_a_real_test(monkeypatch):
    """«Растут быстрее рынка» — только значимый обгон по условному тесту доли с
    поправкой; «по рынку мало данных» — не «рынок ровный» (п. 1.11 плана 04.11)."""
    topics = [{"key": k, "label": k, "short": k, "risk": "ops", "group": "g"} for k in ("big", "noise", "thin")]
    bank = {"big_w0": 40, "big_b": 70, "noise_w0": 13, "noise_b": 70, "thin_w0": 12, "thin_b": 35,
            "_tw0": 65, "_tb": 175}
    market = {"big_w0": 300, "big_b": 2100, "noise_w0": 300, "noise_b": 2100, "thin_w0": 2, "thin_b": 7,
              "_tw0": 600, "_tb": 4200}

    def week_counts(bank_, product, exclude_bank=None, source=None):
        return topics, (market if exclude_bank else bank)
    monkeypatch.setattr(rd, "resolve_bank", lambda b: "Сбербанк")
    monkeypatch.setattr(rd, "_topic_week_counts", week_counts)
    d = {x["key"]: x for x in rd.week_pulse("Сбербанк")["diverge"]}
    assert d["big"]["faster_sig"] and d["big"]["market_claim"] == "only_bank" and d["big"]["market_flat"]
    assert not d["noise"]["faster_sig"]                   # ×1,3 при ровном рынке — шум, не обгон
    assert d["thin"]["market_claim"] == "market_unknown" and not d["thin"]["market_flat"]
    assert not d["thin"]["faster_sig"] and "мало" in d["thin"]["market_note"]


def test_week_pulse_uses_real_spread_of_weeks(monkeypatch):
    """С перерассеянием недель (волны жалоб) умеренный обгон рынка — не «быстрее
    рынка»; без волн тот же обгон значим (ревью 04.11.2026: при φ = 1 флаг горел
    почти каждую неделю)."""
    topics = [{"key": f"t{i}", "label": f"t{i}", "short": f"t{i}", "risk": "ops", "group": "g"} for i in range(8)]

    def rows(wavy: bool):
        b, m = {"_tw0": 200, "_tb": 1400}, {"_tw0": 3000, "_tb": 21000}
        for i, t in enumerate(topics):
            base = [4, 30, 6, 28, 5, 32, 5] if wavy else [15, 15, 15, 15, 15, 15, 15]
            b.update({f"t{i}_w0": 40 if i == 0 else 15, f"t{i}_b": sum(base), f"t{i}_wk": [40 if i == 0 else 15, 15] + base})
            m.update({f"t{i}_w0": 300, f"t{i}_b": 2100, f"t{i}_wk": [300, 300] + [300] * 7})
        return b, m

    monkeypatch.setattr(rd, "resolve_bank", lambda b: "Сбербанк")
    for wavy, expect in ((False, True), (True, False)):
        b, m = rows(wavy)
        monkeypatch.setattr(rd, "_topic_week_counts", lambda bank, product, exclude_bank=None, source=None, b=b, m=m:
                            (topics, m if exclude_bank else b))
        d = {x["key"]: x for x in rd.week_pulse("Сбербанк")["diverge"]}
        assert d["t0"]["faster_sig"] is expect, wavy


def test_market_comparison_uses_comparable_source(monkeypatch):
    """Новый свой источник жалоб на банк (рост только «по всем источникам») — не «быстрее
    рынка»: банк и рынок сравниваются по одному корпусу (CMP_SOURCE)."""
    topics = [{"key": f"t{i}", "label": f"t{i}", "short": f"t{i}", "risk": "ops", "group": "g"} for i in range(6)]
    base = [15] * 7

    def rows(w0):
        r = {"_tw0": 100, "_tb": 700}
        for i in range(6):
            r.update({f"t{i}_w0": w0 if i == 0 else 15, f"t{i}_b": 105, f"t{i}_wk": [w0 if i == 0 else 15, 15] + base})
        return r
    market = {"_tw0": 3000, "_tb": 21000}
    for i in range(6):
        market.update({f"t{i}_w0": 300, f"t{i}_b": 2100, f"t{i}_wk": [300] * 9})
    seen = []

    def week_counts(bank, product, exclude_bank=None, source=None):
        seen.append((bool(exclude_bank), source))
        if exclude_bank:
            return topics, market
        return topics, rows(15 if source == rd.CMP_SOURCE else 45)   # всплеск — только из нового источника
    monkeypatch.setattr(rd, "resolve_bank", lambda b: "Сбербанк")
    monkeypatch.setattr(rd, "_topic_week_counts", week_counts)
    d = {x["key"]: x for x in rd.week_pulse("Сбербанк")["diverge"]}
    assert d["t0"]["ratio"] == 3.0                       # рост к своей норме — по всем источникам
    assert d["t0"]["faster_sig"] is False                # а «быстрее рынка» — нет
    assert (True, rd.CMP_SOURCE) in seen and (False, rd.CMP_SOURCE) in seen
