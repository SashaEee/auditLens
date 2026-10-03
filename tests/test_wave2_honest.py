"""Волна 2 аудита 03.10 «Честные цифры»: цвет «Проверить сегодня» только по
подтверждённому росту, слова ИИ сверяются с числами сигнала, якоря [N] —
с источником, разбор дела помнит состав, «Новое» сверяется с историей,
уязвимые клиенты — с поправкой на продукты. БД не нужна."""
from __future__ import annotations

import os
import types

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import reviews_dash as RD  # noqa: E402

CHARGEBACK = {"key": "chargeback", "label": "Оспаривание операций и возвраты (чарджбэк)",
              "short": "Чарджбэк", "week": 9, "prev_week": 14, "accel": False,
              "ratio": 2.2, "market_ratio": 1.58}
ENFORCEMENT = {"key": "enforcement", "label": "Аресты и списания по исполнительным документам",
               "short": "Исполнительные документы", "week": 9, "ratio": 2.25,
               "market_ratio": 1.02}


def test_poisson_matches_audit_numbers():
    """ОБЗ-01: 9 при норме 4,0 — P≈0,02; на 40 проблемах это случайность."""
    assert RD.poisson_sf(9, 4.0) == pytest.approx(0.0214, abs=5e-4)
    assert RD.poisson_sf(13, 9.9) == pytest.approx(0.199, abs=2e-3)
    assert RD.poisson_sf(9, 4.0) > RD.PULSE_ALPHA / 40
    assert RD.poisson_sf(30, 10) < RD.PULSE_ALPHA / 40
    assert RD.poisson_sf(0, 3) == 1.0 and RD.poisson_sf(3, 0) == 0.0


def test_brief_cannot_say_accelerating_when_week_fell():
    """ОБЗ-02: «нарастают … *Ускоряется.*» при 14 → 9."""
    md = ("- **[СРЕДНИЙ]** **Чарджбэк: отказы и формальные отписки нарастают** — две недели "
          "подряд (14 → 9), ×2,2 к норме. *Ускоряется.* Аудитору: проверить инструкцию.")
    out = RD.fix_market_claims(md, [CHARGEBACK])
    assert "нараста" not in out and "скоряется" not in out
    assert "отписки сохраняются" in out and "Аудитору: проверить инструкцию." in out
    acc = {**CHARGEBACK, "accel": True, "prev_week": 5}
    assert RD.fix_market_claims(md, [acc]) == md          # ускорение есть — не трогаем


def test_market_denial_is_checked_per_topic():
    """ОБЗ-02: «которые рынок не повторяет» при чарджбэке ×1,58 у рынка."""
    lead = "Локальные всплески по арестам и чарджбэку, которые рынок не повторяет."
    out = RD.fix_market_claims(lead, [ENFORCEMENT, CHARGEBACK])
    assert "рынок не повторяет" not in out
    assert "«Чарджбэк» растёт и у рынка (×1,6)" in out
    # тема ровная по рынку — утверждение верное и остаётся
    ok = "Аресты растут только у Сбера, рынок ровный."
    assert RD.fix_market_claims(ok, [ENFORCEMENT, CHARGEBACK]) == ok
    # «только у Сбера» про чарджбэк — правится, хотя в списке есть ровная тема
    assert "сильнее, чем по рынку" in RD.fix_market_claims(
        "Чарджбэк растёт только у Сбера.", [ENFORCEMENT, CHARGEBACK])


def test_for_you_does_not_pass_unconfirmed_divergence_as_signal(monkeypatch):
    """ОБЗ-01: расхождения подмешивались к сигналам и проходили без связи с профилем."""
    from bank_audit.digest import personal as P
    monkeypatch.setattr(P, "_item_vecs", lambda labels: None)
    monkeypatch.setattr(P, "_taste_of", lambda prof, v: 0)
    sec = {"reviews_pulse": {"payload": {
        "checked": {"themes": 40},
        "signals": [{**CHARGEBACK, "level": "medium", "risk": "ops"}],
        "diverge": [{**ENFORCEMENT, "gap": 2.21, "baseline_week": 4.0, "risk": "compliance"},
                    {"key": "fraud", "label": "Мошенничество", "short": "Мошенники",
                     "week": 30, "baseline_week": 10.0, "gap": 1.6, "ratio": 3.0,
                     "market_ratio": 1.9, "risk": "ops"}]}}}
    out = P._my_signals(None, {"ops": 1.0}, None, sections=sec)
    keys = {s["key"]: s for s in out}
    assert "enforcement" not in keys                     # не подтверждено и не про зону
    assert keys["chargeback"]["confirmed"] is True
    assert keys["fraud"]["confirmed"] is True            # 30 при норме 10 — значимо
    line = P._signal_line({**ENFORCEMENT, "confirmed": False, "baseline_week": 4.0})
    assert "рост не подтверждён статистикой" in line


def test_anchor_check_flags_source_that_says_nothing_of_the_phrase():
    """ИИ-04: «перепутан источник [28]» — критик проверял факт, а не связку с фразой."""
    from bank_audit.research.gptr import citations as C
    cited = [{"n": 1, "facts": [{"verbatim": "Ставка по вкладу «Лучший» — 16% годовых",
                                 "value": "16", "attribute": "ставка"}]},
             {"n": 2, "facts": [{"verbatim": "Комиссия за перевод по СБП не взимается",
                                 "value": "0", "attribute": "комиссия"}]}]
    rep = ("Ставка по вкладу достигает 16% годовых [1]. "
           "Ипотечные заёмщики жалуются на навязанную страховку жизни [2].\n"
           "| таблица [2] | без проверки |")
    bad = C.anchor_mismatches(rep, cited)
    assert len(bad) == 1 and "страховку" in bad[0]["claim"] and "[2]" in bad[0]["issue"]


def test_case_analysis_tracks_composition_not_count():
    """ДЕЛ-04: убрали один материал, добавили другой — разбор считался свежим."""
    from bank_audit.web import userdata as U
    case = {"analysis": "Главное — [1] и [3]; см. также [2].", "analysis_items": 3,
            "analysis_item_ids": [10, 11, 12],
            "items": [{"item_id": 10}, {"item_id": 12}, {"item_id": 13}]}
    U.analysis_state(case)
    assert case["analysis_stale"] is True
    assert case["analysis_refs"] == {"1": 1, "2": None, "3": 2}
    assert U.remap_analysis(case) == "Главное — [1] и [2]; см. также [материал удалён]."
    same = {"analysis": "x [1]", "analysis_item_ids": [10, 12],
            "items": [{"item_id": 10}, {"item_id": 12}]}
    U.analysis_state(same)
    assert same["analysis_stale"] is False
    old = {"analysis": "x", "analysis_items": 2, "items": [{"item_id": 1}, {"item_id": 2}]}
    U.analysis_state(old)                                 # разбор до 03.10 — по числу
    assert old["analysis_stale"] is False and "analysis_refs" not in old
    from bank_audit.web import case_export as E
    note = E._analysis_note({**case, "analysis_at": "2026-10-03T09:00:00+00:00"})
    assert note.startswith("Разбор от 03.10.2026 по 3 материалам") and "менялся" in note


class _Sess:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *a, **k):
        return types.SimpleNamespace(all=lambda: self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_new_story_is_dropped_when_it_was_there_before(monkeypatch):
    """ОТЗ-03: «Новое: занижает суммы по исполнительным листам» жило с июля."""
    from bank_audit.rag import embedder
    from bank_audit.rag import reviews_work as W
    v_old, v_new = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
    monkeypatch.setattr(W, "_vecs_for", lambda urls: {u: (v_old if u.startswith("a") else v_new)
                                                      for u in urls})
    monkeypatch.setattr(W, "_store_vecs", lambda v: None)
    monkeypatch.setattr(embedder, "embed_batch", lambda t: [v_new] * len(t))
    monkeypatch.setattr(RD.db, "session", lambda: _Sess([(v_old,), (v_old,), (v_old,)]))
    clusters = [{"urls": ["a1", "a2"], "items": []}, {"urls": ["b1", "b2"], "items": []}]
    assert RD._seen_before("Сбербанк", clusters, 7, 8) == [True, False]
    monkeypatch.setattr(RD.db, "session", lambda: _Sess([]))
    assert RD._seen_before("Сбербанк", clusters, 7, 8) == [False, False]


def test_vulnerable_share_is_compared_within_products(monkeypatch):
    """ОТЗ-10: «уязвимые выше рынка» складывалось из детских и пенсионных карт."""
    vcodes = [f for g, _l, items in RD._FLAG_GROUPS if g == "vuln" for f, _n, _c in items]
    k = len(vcodes)

    def r(own, p, n, any_):
        return (own, p, n, any_) + (0,) * (k - 1)
    # у Сбера больше детских карт; внутри каждого продукта доля как у рынка
    rows = [r(True, "debit_card", 1000, 100), r(True, "mortgage", 1000, 10),
            r(False, "debit_card", 1000, 100), r(False, "mortgage", 9000, 90)]
    monkeypatch.setattr(RD.db, "session", lambda: _Sess(rows))
    adj = RD._vuln_by_product("Сбербанк", None, 90)["vuln:any"]
    assert adj["observed"] == 110 and adj["expected"] == pytest.approx(110)
    assert adj["p"] > 0.5                                # отличия нет
    # сырое сравнение: 5,5% против 1,9% — «выше рынка» в 2,9 раза
    assert (110 / 2000) / (190 / 10000) > 2.5
