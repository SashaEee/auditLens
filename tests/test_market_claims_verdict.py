"""О рынке говорит только вердикт сопоставимого корпуса (ревью 04.11.2026).

Всплеск, посчитанный по всем площадкам банка, нельзя сравнивать с рынком banki.ru
через отношения: новый свой источник жалоб выглядел как «только у Сбера»."""
from __future__ import annotations

from bank_audit.rag import review_stats as RS
from bank_audit.rag import reviews_dash as rd
from bank_audit.rag import reviews_llm as rl


def test_bank_without_comparable_corpus_gets_no_market_claim():
    # у банка на banki.ru ноль: тест доли на нулях давал «рынок растёт так же»
    v = RS.market_verdict(0, 400, 0, 2100, k=1 / 7, min_bank=20)
    assert v["claim"] == "bank_unknown" and not v["bank_specific"] and not v["market_flat"]
    assert "с рынком не сравниваем" in RS.market_note(v)
    # без порога — прежнее поведение (вызовы вне вкладки не меняются)
    assert RS.market_verdict(0, 400, 0, 2100, k=1 / 7)["claim"] == "same"


def test_small_comparable_share_is_bank_unknown():
    # 40 жалоб за неделю при норме 10 — почти все с других площадок; banki.ru — 10 из 110
    brow = {"t_w0": 40, "t_b": 70}
    crow = {"t_w0": 2, "t_b": 28}
    mrow = {"t_w0": 300, "t_b": 2100}
    v = rd._market_v("t", brow, crow, mrow, 7.0, 1.0, 1.0)
    assert v["claim"] == "bank_unknown" and not v["market_flat"]
    crow = {"t_w0": 20, "t_b": 60}                  # banki.ru — заметная часть
    assert rd._market_v("t", brow, crow, mrow, 7.0, 1.0, 1.0)["claim"] != "bank_unknown"


def test_market_text_uses_verdict_and_stays_silent_without_note():
    s = {"ratio": 3.0, "market_ratio": 1.0, "market_claim": "none", "market_note": None}
    assert rd.market_text(s, "Сбера") is None
    old = {"ratio": 3.0, "market_ratio": 1.0}          # снимок до вердикта
    assert "только у Сбера" in rd.market_text(old, "Сбера")


def test_only_bank_wording_removed_without_only_bank_verdict():
    sig = [{"key": "t0", "label": "Тема0", "short": "Тема0", "ratio": 3.0, "market_ratio": 1.0,
            "market_flat": True, "market_claim": "none", "market_note": None}]
    out = rd.fix_market_claims("Всплеск «Тема0» только у Сбера.", sig)
    assert "только у" not in out and "у Сбера" in out
    sig[0].update(market_claim="only_bank", market_note="только у банка: по рынку тема ровная (×1)")
    assert "только у Сбера" in rd.fix_market_claims("Всплеск «Тема0» только у Сбера.", sig)


def test_signal_lines_follow_verdict():
    base = {"key": "t0", "label": "Тема 0", "level": "medium", "week": 30, "baseline_week": 10.0,
            "ratio": 3.0, "market_ratio": 1.0, "bank_specific": False}
    lines, _ov = rl.signal_lines({"signals": [{**base, "market_claim": "none", "market_note": None}]})
    assert "ТОЛЬКО" not in lines[0] and "рынок" not in lines[0]
    lines, _ov = rl.signal_lines({"signals": [{**base, "bank_specific": True, "market_claim": "only_bank",
                                               "market_note": "только у банка: по рынку тема ровная (×1)"}]})
    assert "ТОЛЬКО у банка" in lines[0]
    _l, ov = rl.signal_lines({"signals": [], "overall": {"week": 100, "baseline_week": 80.0,
                                                       "cmp_ratio": 1.02, "market_ratio": 1.0}})
    assert "по banki.ru банк ×1.02, рынок ×1.0" in ov
