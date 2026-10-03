"""Аудит-дело, этап 4: «В дело» из ИИ-помощника, новостей и «Рынка».

Главное правило владельца: огромный отчёт ИИ не попадает в выгрузку дела —
только название, вопрос, короткий вывод и где открыть полный текст.
SQL снимков (отчёт, продукт) проверяется на живой базе.
"""
from __future__ import annotations

import json

from bank_audit.web import case_export as X
from bank_audit.web import userdata as U

LONG_REPORT = ("# Ставки по вкладам\n\n## Резюме\nСтавки по вкладам на 3 месяца у топ-10 банков снизились "
               "в среднем на 0,4 п.п. [1][2]. **Сбер** держит ставку ниже рынка [3, 4].\n\n"
               "## Детали\n| банк | ставка |\n|---|---|\n" + ("СЕКРЕТНЫЙ ХВОСТ ОТЧЁТА. " * 2000))


def test_report_lead_takes_summary_section_without_citations():
    lead = U.report_lead(LONG_REPORT)
    assert lead.startswith("Ставки по вкладам на 3 месяца")
    assert "[1]" not in lead and "**" not in lead and "п.п.." not in lead
    assert "ХВОСТ" not in lead and len(lead) <= 420


def test_report_lead_falls_back_to_first_paragraph_and_cuts_on_sentence():
    assert U.report_lead("Коротко: рынок стабилен.\n\n### Раздел\nещё") == "Коротко: рынок стабилен."
    lead = U.report_lead("## Выводы\n" + "Предложение про рынок вкладов. " * 40)
    assert len(lead) <= 420 and lead.endswith(".")
    assert U.report_lead("") == "" and U.report_lead(None) == ""


def test_news_item_is_sanitized_and_needs_a_link():
    assert U._prepare_item({"kind": "news", "url": "javascript:alert(1)", "title": "x"}, "u") is None
    r = U._prepare_item({"kind": "news", "url": "https://ex.org/n", "title": "Банк поднял ставки",
                         "meta": {"domain": "ex.org", "summary": "  почему   важно  " + "я" * 900,
                                  "evil": "<script>"}}, "u")
    m = json.loads(r["m"])
    assert r["k"] == "news" and r["u"] == "https://ex.org/n" and r["r"] is None
    assert m["summary"].startswith("почему важно") and len(m["summary"]) <= 500 and "evil" not in m


def test_answer_gets_fingerprint_address_and_keeps_text():
    a = {"kind": "answer", "meta": {"question": "Что с комиссиями?", "text": "Ответ\n\nс абзацами",
                                    "sources": [{"n": 1, "url": "https://ex.org/a", "title": "Ex"},
                                                {"n": 2, "url": "ftp://bad"}]}}
    r1, r2 = U._prepare_item(a, "u"), U._prepare_item(a, "v")
    assert r1["u"].startswith("answer:") and r1["u"] == r2["u"]        # один ответ — один адрес
    m = json.loads(r1["m"])
    assert m["text"] == "Ответ\n\nс абзацами" and [s["n"] for s in m["sources"]] == [1]
    assert r1["t"] == "Что с комиссиями?"
    assert U._prepare_item({"kind": "answer", "meta": {"question": "q", "text": " "}}, "u") is None


def test_report_and_offer_need_server_side_data(monkeypatch):
    monkeypatch.setattr(U, "_report_meta", lambda rid, u: None)       # нет доступа к отчёту
    assert U._prepare_item({"kind": "report", "ref_id": 5}, "u") is None
    monkeypatch.setattr(U, "_report_meta", lambda rid, u: {"report_id": 5, "title": "Т", "lead": "Л"})
    r = U._prepare_item({"kind": "report", "ref_id": 5, "title": "подмена", "url": "https://x"}, "u")
    assert r["r"] == 5 and r["t"] == "Т" and r["u"] is None          # название — с сервера
    monkeypatch.setattr(U, "_offer_meta", lambda oid: {"offer_id": 7, "bank": "Банк", "product": "Вклад",
                                                       "url": "https://bank/v"})
    r = U._prepare_item({"kind": "offer", "ref_id": 7}, "u")
    assert r["t"] == "Банк · Вклад" and r["u"] == "https://bank/v" and r["r"] == 7
    assert U._prepare_item({"kind": "bogus", "url": "https://x"}, "u") is None


def test_offer_terms_line():
    t = X.offer_terms({"rate_pct": 18.5, "rate_label": "Ставка", "amount_min": 10000, "amount_max": 3000000,
                       "term_min": 3, "term_max": 36, "cashback_pct": 5})
    assert t.startswith("ставка 18,5%") and "срок 3–36 мес" in t and "кешбэк до 5%" in t
    assert "3 000 000 ₽" in t
    assert X.offer_terms({}) == ""


def _report_item():
    return {"kind": "report", "ref_id": 45, "title": "Ставки по вкладам", "url": None,
            "meta": {"title": "Ставки по вкладам", "question": "Что со ставками?",
                     "lead": U.report_lead(LONG_REPORT), "mode": "deep", "owner_name": "Елена Волкова",
                     "created_at": "2026-10-03T10:00:00+03:00"}}


def test_export_row_has_lead_and_link_but_never_the_report_body():
    row = X._row(1, _report_item(), "https://auditlens.example")
    assert row["Тип"] == "отчёт ИИ" and row["Дата"] == "03.10.2026" and row["Площадка"] == "ИИ-помощник"
    assert row["Суть"].startswith("Ставки по вкладам — Ставки по вкладам на 3 месяца")
    assert "ХВОСТ" not in json.dumps(row, ensure_ascii=False)
    assert row["Ссылка"] == "https://auditlens.example/#ai?report=45"
    assert X._row(1, _report_item(), None)["Ссылка"] == ""            # внутренний адрес не пишем


def test_export_answer_has_no_fingerprint_link_and_offer_shows_snapshot():
    ans = {"kind": "answer", "url": "answer:abc", "title": "Вопрос",
           "meta": {"question": "Вопрос", "text": "Ответ"}, "added_at": "2026-10-03T09:00:00+03:00"}
    row = X._row(2, ans)
    assert row["Ссылка"] == "" and row["Суть"] == "Вопрос — Ответ" and row["Тип"] == "ответ ИИ"
    off = {"kind": "offer", "ref_id": 7, "url": "https://bank/v", "title": "Банк · Вклад",
           "meta": {"bank": "Банк", "category_label": "Вклады", "rate_pct": 17, "as_of": "2026-10-03",
                    "valid_from": "2026-09-20"}}
    row = X._row(3, off)
    assert row["Банк"] == "Банк" and row["Продукт"] == "Вклады" and row["Дата"] == "03.10.2026"
    assert "условия на 03.10.2026" in row["Суть"] and "версия с 20.09.2026" in row["Суть"]


def test_word_and_excel_build_with_new_kinds():
    case = {"case_id": 1, "title": "Тест", "owner_name": "Владелец", "members": [], "status_label": "В работе",
            "items": [_report_item(),
                      {"kind": "news", "url": "https://ex.org/n", "title": "Новость",
                       "meta": {"domain": "ex.org", "summary": "почему важно", "ts": "2026-10-02T08:00:00"}}],
            "app_base": None}
    docx, xlsx = X.to_docx(case), X.to_xlsx(case)
    assert docx[:2] == b"PK" and xlsx[:2] == b"PK"
    import io
    import zipfile
    body = zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()
    assert "Полный отчёт в выгрузку не входит" in body and "ХВОСТ" not in body


def test_memo_listing_uses_lead_not_body():
    from bank_audit.rag.reviews_llm import case_digest
    summary, listing = case_digest({"items": [_report_item()]})
    assert "[1] отчёт ИИ: Ставки по вкладам" in listing and "ХВОСТ" not in listing
    assert "отчётов ИИ: 1" in summary
