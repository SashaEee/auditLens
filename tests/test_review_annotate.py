"""LLM-разметка отзывов: проверки кодом и статистика сигналов.

БД и модель не нужны: всё ниже — чистые функции над строками и числами.
"""
from bank_audit.rag import review_annotate as ra
from bank_audit.rag import review_codebook as cb
from bank_audit.rag.bankiru_fts import _city
from bank_audit.rag.reviews_dash import _bh, _nb_tail

BASE = {"kind": "complaint", "segment": "person", "product": "debit_card", "channel": ["app"],
        "issue": "block_161", "issues2": ["support_access"], "esc": "none", "esc_to": [],
        "no_consent": False, "misled": False, "vulnerable": [], "amount": None,
        "event_date": "", "city": "", "code_fit": "exact", "summary": "s", "quote": "q",
        "new_topic": ""}


def test_codebook_and_prompt_agree():
    """Каждый код кодификатора описан в тексте для модели — иначе модель его
    не поставит, а код отвергнет всё, чего нет в списке."""
    text = ra._system()
    for code in list(cb.ISSUES) + [p for p in cb.PRODUCTS]:
        assert code in text, code


def test_normalize_rejects_unknown_codes():
    assert ra.normalize(BASE)["issue"] == "block_161"
    assert ra.normalize({**BASE, "issue": "misinformation"}) is None   # признак, а не код
    assert ra.normalize({**BASE, "product": "mobile_app"}) is None
    assert ra.normalize({**BASE, "kind": "angry"}) is None


def test_normalize_consistency():
    v = ra.normalize({**BASE, "kind": "praise", "issue": "fees", "issues2": ["staff"]})
    assert v["issue"] == "no_issue" and v["issues2"] == []
    v = ra.normalize({**BASE, "esc": "none", "esc_to": ["cbr"]})
    assert v["esc_to"] == []
    v = ra.normalize({**BASE, "issues2": ["block_161", "fees", "staff", "cash_atm"]})
    assert v["issues2"] == ["fees", "staff"]                 # без главной, не больше двух
    v = ra.normalize({**BASE, "new_topic": "что-то", "code_fit": "exact"})
    assert v["new_topic"] is None                            # точный код — новой темы нет
    v = ra.normalize({**BASE, "no_consent": "false"})
    assert v["no_consent"] is False


def test_event_date_only_real_dates():
    assert ra._event_date("2026-03-15") == "2026-03-15"
    assert ra._event_date("примерно 2025-11") == "2025-11"
    assert ra._event_date("2026-02-30") is None
    assert ra._event_date("2026-13") is None
    assert ra._event_date("") is None


def test_agree_on_what_counters_use():
    a = ra.normalize(BASE)
    assert ra.agree(a, dict(a))
    assert not ra.agree(a, {**a, "issue": "block_115"})
    assert not ra.agree(a, {**a, "esc": "threat"})
    assert ra.agree(a, {**a, "summary": "другое изложение", "channels": []})


def test_fix_quote_verbatim_and_repair():
    text = 'Банк заблокировал карту «как подозрительную».  Поддержка молчит третий день, деньги не вернули.'
    q, ok = ra.fix_quote("Поддержка молчит третий день", text)
    assert ok and q == "Поддержка молчит третий день"
    # кавычки, пробелы и регистр не мешают, возвращается исходный фрагмент текста
    q, ok = ra.fix_quote('банк заблокировал карту "как подозрительную"', text)
    assert ok and q in text
    # пересказ близко к тексту — берём настоящее предложение
    q, ok = ra.fix_quote("Поддержка молчит уже третий день, деньги не вернули.", text)
    assert ok and q in text
    # выдумка — очищаем
    q, ok = ra.fix_quote("Сотрудник в отделении нахамил и порвал договор", text)
    assert not ok and q == ""


def test_clean_text_strips_markup():
    t, why = ra.clean_text(".rv{background:#fff}.a b{font-size:12px} Банк списал комиссию без предупреждения, "
                           "прошу вернуть деньги на карту.")
    assert why is None and "{" not in t and t.startswith("Банк")
    assert ra.clean_text("отвечают ......................")[1] == "no_text"
    assert ra.clean_text("x" * 60000)[1] == "too_long"


def test_scrub_personal_data():
    s = ra.scrub("звоните +7 (912) 345-67-89, карта 4276 1234 5678 9012, почта a.b@mail.ru")
    assert "912" not in s and "4276" not in s and "mail.ru" not in s


def test_nb_tail_behaves():
    flat = [10, 11, 9, 10, 12, 8, 10]
    assert _nb_tail(10, flat) > 0.3             # обычная неделя — не всплеск
    assert _nb_tail(25, flat) < 0.001           # явный всплеск
    wavy = [2, 18, 3, 20, 1, 15, 11]            # жалобы идут волнами
    assert _nb_tail(25, wavy) > _nb_tail(25, flat)  # разброс учтён — порог строже
    assert _nb_tail(6, [0, 0, 0, 0, 0, 0, 0]) < 0.01  # новая проблема с нуля


def test_bh_is_monotone_and_conservative():
    q = _bh({"a": 0.001, "b": 0.02, "c": 0.04, "d": 0.5})
    assert q["a"] <= q["b"] <= q["c"] <= q["d"]
    assert q["a"] >= 0.001 and q["c"] >= 0.04


def test_city_merges_region_suffix():
    assert _city("Москва и область") == "Москва"
    assert _city("Санкт-Петербург и Ленинградская область") == "Санкт-Петербург"
    assert _city("Казань (Республика Татарстан)") == "Казань"
    assert _city("") is None
