"""Тесты аналитики вкладки «Отзывы» (волна 3): статистика и фильтры.

Индекс «банк против рынка», география и значимость изменений тем держатся на
трёх чистых функциях — отношение долей с интервалом, изменение счётчика и
наполненность месяца события. Фильтр признаков риска подставляет код в SQL,
поэтому белый список обязан отвергать всё незнакомое. БД не нужна.
"""
import datetime as dt

import pytest

from bank_audit.rag.reviews_dash import (
    _flag_sql, _month_clause, _month_completeness, _rate_change, _ratio_ci, flag_label,
)


def test_ratio_ci_equal_shares_not_significant():
    r = _ratio_ci(50, 1000, 500, 10000)
    assert r["rr"] == pytest.approx(1.0)
    assert r["lo"] < 1 < r["hi"]
    assert r["p"] > 0.5


def test_ratio_ci_doubled_share_significant():
    # 9,5% жалоб банка против 4,2% у рынка — как «одностороннее изменение условий»
    r = _ratio_ci(417, 4412, 1795, 42737)
    assert r["rr"] == pytest.approx(2.25, rel=0.01)
    assert r["lo"] > 1.9 and r["p"] < 1e-6


def test_ratio_ci_zero_is_finite():
    r = _ratio_ci(0, 100, 30, 1000)
    assert r is not None and r["hi"] < float("inf")
    assert _ratio_ci(5, 0, 5, 10) is None


def test_rate_change_small_counts_wide_interval():
    # «Кредитные каникулы: 99 против 56» — рост есть, но интервал широкий
    ch = _rate_change(99, 56)
    assert ch["lo"] > 0 and ch["hi"] > 100
    assert _rate_change(0, 10) is None


def test_month_clause_publication_and_event():
    p: dict = {}
    assert "i.dt" in _month_clause("i", "2026-05", p) and p["month"] == "2026-05"
    p = {}
    assert "i.ev_date" in _month_clause("i", "ev:2026-05", p) and p["month"] == "2026-05"
    assert _month_clause("i", None, {}) == ""
    # мусор в параметре — пустой срез, а не весь корпус и не ошибка SQL
    assert _month_clause("i", "2026-05'; drop", {}) == " AND false"


@pytest.mark.parametrize("flag", ["to:cbr", "to:fas", "vuln:any", "vuln:svo", "esc:filed",
                                  "no_consent", "misled", "amount", "amount:1m"])
def test_flag_whitelist_known(flag):
    assert _flag_sql(flag)
    assert flag_label(flag)


@pytest.mark.parametrize("flag", ["to:x", "vuln:'; drop table review", "amount:5", "esc:none", "bogus"])
def test_flag_whitelist_rejects_unknown(flag):
    assert _flag_sql(flag) is None
    assert flag_label(flag) is None


def test_no_flag_means_no_filter():
    assert _flag_sql(None) == "" and _flag_sql("") == ""


def test_month_completeness_grows_with_time():
    # задержка публикации: половина в тот же день, остальное равномерно за 100 дней
    cdf = [min(1.0, 0.5 + 0.005 * k) for k in range(731)]
    fresh = _month_completeness("2026-09", dt.date(2026, 9, 25), cdf)
    older = _month_completeness("2026-06", dt.date(2026, 9, 25), cdf)
    old = _month_completeness("2025-06", dt.date(2026, 9, 25), cdf)
    assert fresh < older < old == pytest.approx(1.0)


# ── Волна 4: рабочее место аудитора ─────────────────────────────────────────

def test_filed_cbr_court_flag():
    from bank_audit.rag.reviews_dash import _flag_sql, flag_label
    assert "ARRAY['cbr', 'court']" in _flag_sql("filed:cbr_court")
    assert flag_label("filed:cbr_court") == "обратились в ЦБ или суд"


def test_source_clause_whitelist():
    from bank_audit.rag.reviews_dash import _source_clause
    p: dict = {}
    assert _source_clause("i", "banki", p) and p["srcs"] == ["bankiru", "banki_reviews"]
    assert _source_clause("i", None, {}) == ""
    assert _source_clause("i", "evil'; drop", {}) is None


def test_severity_uses_codebook_compliance():
    from bank_audit.rag import review_codebook as cb
    from bank_audit.rag.reviews_dash import _severity_sql
    sql = _severity_sql()
    comp = [k for k, v in cb.ISSUES.items() if v[3] == "compliance"]
    assert comp and all(f"'{k}'" in sql for k in comp)
    assert "WHEN 'filed' THEN 3" in sql


_CASE = {"title": "Ставки по льготной ипотеке", "owner": "auditor", "shared": True,
         "note": "проверка", "analysis": "### Итог\n- **важно** [1]",
         "items": [
             {"kind": "review", "url": "https://example.org/r/1", "title": "снимок", "note": "ключевой",
              "added_by": "auditor",
              "review": {"bank": "Банк", "date": "2026-09-12", "city": "Казань", "source": "banki.ru",
                         "product": "Ипотека", "issue_label": "Одностороннее изменение условий",
                         "risk": "conduct", "summary": "=HYPERLINK(\"x\")", "quote": "подняли ставку",
                         "esc": "filed", "esc_to": ["cbr"], "vulnerable": ["pensioner"],
                         "no_consent": False, "misled": False, "amount": 150000.0}},
             {"kind": "document", "url": "https://example.org/d", "title": "Тарифы", "note": None,
              "bank_name": "Банк", "fetched_at": "2026-09-01T10:00:00"}]}


def test_case_digest_counts_by_code():
    from bank_audit.rag.reviews_llm import case_digest
    summary, listing = case_digest(_CASE)
    assert "жалоб: 1" in summary and "документов: 1" in summary
    assert "Уже обратились в ЦБ, суд и т. п.: 1" in summary
    assert listing.startswith("[1] жалоба 2026-09-12") and "[2] документ: Тарифы" in listing


def test_case_xlsx_does_not_execute_formulas():
    import io
    import openpyxl
    from bank_audit.web.case_export import to_xlsx
    ws = openpyxl.load_workbook(io.BytesIO(to_xlsx(_CASE)))["Материалы"]
    head = [c.value for c in ws[1]]
    cell = ws.cell(row=2, column=head.index("Суть") + 1)
    assert cell.data_type == "s" and cell.value.startswith("=HYPERLINK")
    assert ws.cell(row=2, column=head.index("Куда") + 1).value == "ЦБ"


def test_case_docx_has_analysis_and_items():
    import io
    import docx
    from bank_audit.web.case_export import to_docx
    text_ = "\n".join(p.text for p in docx.Document(io.BytesIO(to_docx(_CASE))).paragraphs)
    assert "Разбор материалов (ИИ)" in text_ and "[1] жалоба · 12.09.2026" in text_
    assert "Признаки: обратился: ЦБ; уязвимый клиент: пенсионер" in text_


def test_pct_int_rounds_half_up_like_frontend():
    # «Главное» считает Math.round, шапка — Python; банковское round(22.5)=22
    # давало «больше на 23%» и «больше на 22%» на одном экране
    from bank_audit.rag.reviews_dash import _pct_int, _int_sp
    assert _pct_int(22.5) == 23
    assert _pct_int(-22.5) == -23
    assert _pct_int(22.49) == 22
    assert _pct_int(None) == 0
    assert _int_sp(2428) == "2 428"
