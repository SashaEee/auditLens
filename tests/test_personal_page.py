"""«Для вас»: обрезка без обрыва слов, сигналы из снимка выпуска, строка
сигнала для редактора, поля перехода в журнал тарифов. Без сети и БД."""
from __future__ import annotations

from bank_audit.digest import personal as p


def test_clip_keeps_whole_words_and_marks_cut():
    s = ("Разобрать 15 кейсов чарджбэка за неделю, выявить общий сценарий и "
         "источник концентрации в Санкт-Петербурге, проверить сроки уведомлений")
    out = p._clip(s, 100)
    assert out.endswith("…")
    assert len(out) <= 101
    # последнее слово целое — его начало есть в исходнике как отдельное слово
    last = out[:-1].split()[-1]
    assert f" {last} " in f" {s} "


def test_clip_prefers_sentence_end_and_leaves_short_text():
    assert p._clip("Короткий текст.", 100) == "Короткий текст."
    s = "Первое предложение довольно длинное и законченное. Второе продолжается дальше и дальше"
    assert p._clip(s, 70) == "Первое предложение довольно длинное и законченное."
    assert p._clip(None, 10) == ""


def test_signal_line_carries_market_and_lead_mark():
    line = p._signal_line({"label": "Чарджбэк", "key": "chargeback", "week": 15,
                           "baseline_week": 3.4, "ratio": 4.4, "market_ratio": 1.93},
                          lead_key="chargeback")
    assert "[chargeback]" in line
    assert "норма 3,4" in line and "×4,4" in line
    assert "сильнее рынка" in line
    assert line.endswith("ГЛАВНОЕ ОБЩЕГО ВЫПУСКА")
    same = p._signal_line({"label": "Каникулы", "key": "k", "week": 10,
                           "baseline_week": 5.5, "ratio": 1.8, "market_ratio": 1.7})
    assert "рынок растёт так же" in same
    assert "ГЛАВНОЕ" not in same


def test_my_signals_use_digest_snapshot_numbers(monkeypatch):
    # живой пересчёт трогать нельзя: числа должны совпасть с «Общим»
    from bank_audit.rag import reviews_dash as rd

    def boom(*a, **k):
        raise AssertionError("живой пересчёт вместо снимка")
    monkeypatch.setattr(rd, "weekly_signals", boom)
    monkeypatch.setattr(rd, "week_pulse", boom)
    sections = {"reviews_pulse": {"payload": {
        "signals": [{"key": "chargeback", "label": "Чарджбэк", "week": 15,
                     "baseline_week": 3.4, "ratio": 4.4, "level": "high"}],
        "diverge": [{"key": "chargeback", "label": "Чарджбэк", "gap": 2.3},
                    {"key": "close", "label": "Закрытие счетов", "gap": 1.9, "week": 8}],
    }}}
    out = p._my_signals(None, {}, None, sections=sections)
    by = {s["key"]: s for s in out}
    assert by["chargeback"]["ratio"] == 4.4          # из снимка, а не живое ×4,2
    assert "close" in by


def test_tariff_block_keeps_journal_link_fields():
    sections = {"tariff_moves": {"payload": {"top_changes": [
        {"bank": "Сбербанк", "is_sber": True, "category": "deposit", "title": "СберKids+",
         "from": 11, "to": 11.89, "delta": 0.89, "changed_at": "2026-09-22T10:00:00+03:00",
         "bank_slug": "sberbank", "change_id": 7, "offer_id": 42}],
        "sber_gap": [{"category": "deposit", "sber_max": 19, "sber_min": 11.5,
                      "market_max": 30, "market_min": 6, "market_median": 12.7}]}}}
    tb = p._tariff_block(sections, ["deposit"])
    m = tb["moves"][0]
    assert (m["bank_slug"], m["change_id"], m["offer_id"]) == ("sberbank", 7, 42)
    g = tb["gap"][0]
    assert g["sber_min"] == 11.5 and g["market_min"] == 6
