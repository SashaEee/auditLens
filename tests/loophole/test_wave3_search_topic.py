"""«Аудит уязвимостей», волна 3 аудита 03.10: поиск по словам (УЯЗ-03) и
записи «не о банках» (УЯЗ-01). SQLite в памяти; LOWER переопределяется на
юникодный — встроенный SQLite кириллицу в нижний регистр не переводит."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from bank_audit.loophole import relevance
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from tests.loophole import test_record_verdict_authorization as access

session = access.session
client = access.client
ROOT = Path(__file__).resolve().parents[2]


def _unicode_lower(session) -> None:
    raw = session.connection().connection
    raw.create_function("lower", 1, lambda v: v.lower() if isinstance(v, str) else v,
                        deterministic=True)


def _add(session, sha, title, *, snippet=None, headline=None, url="https://hranidengi.com/t/1",
         classification="vulnerability", is_loophole=True, model=None):
    rid = repo.insert_record(LoopholeRecord(
        sha256=sha, title=title, snippet=snippet, url=url,
        is_loophole=is_loophole, classification=classification, verdict_model=model,
    ), session=session)
    if headline:
        session.execute(text("UPDATE loophole_record SET headline = :h WHERE record_id = :i"),
                        {"h": headline, "i": rid})
    return rid


def test_search_terms_split_stem_and_fold():
    # показываем слова пользователя, ищем по основе
    assert repo.search_terms("кэшбэк СБП") == [["кэшбэк", "кешбек"], ["сбп", "сбп"]]
    assert repo.search_terms("счёт") == [["счёт", "счет"]]
    # глагольные окончания не срезаются с существительных
    assert [t for _w, t in repo.search_terms("банкомат кредит правила")] == [
        "банкомат", "кредит", "правил"]
    assert [t for _w, t in repo.search_terms("заблокировали")] == ["заблок"]
    assert [t for _w, t in repo.search_terms("Кешбэк за переводы")] == ["кешбек", "перевод"]
    assert [t for _w, t in repo.search_terms("кэшбэк не начисляют")] == ["кешбек", "начисл"]
    assert repo.search_terms("по на") == []
    # «это» — стоп-слово и после свёртки э→е
    assert repo.search_terms("это мошенничество") == [["мошенничество", "мошенничеств"]]
    assert repo.search_terms("что это") == []
    assert [t for _w, t in repo.search_terms("Т‑Банк")] == ["т-банк"]   # неразрывный дефис
    assert all("%" not in t and "_" not in t for _w, t in repo.search_terms("100% кэш_бэк"))


def test_catalog_finds_all_words_in_any_field_and_form(session):
    """«кэшбэк СБП» находил только точную фразу (1 из 4)."""
    _unicode_lower(session)
    _add(session, "a", "Кэшбэк за переводы через СБП")
    _add(session, "b", "Выгодные покупки (кешбэк, кредитки)", snippet="оплата через сбп")
    _add(session, "c", "Ветка форума", headline="Кешбэк за переводы между картами через СБП")
    _add(session, "d", "Только кэшбэк")
    found = repo.list_catalog_cases(query_text="кэшбэк СБП", classification="all", session=session)
    assert {r["title"] for r in found} == {"Кэшбэк за переводы через СБП",
                                           "Выгодные покупки (кешбэк, кредитки)", "Ветка форума"}
    assert repo.count_catalog_cases(query_text="кэшбэк СБП", classification="all",
                                    session=session) == 3
    s = repo.catalog_summary(query_text="кэшбэк СБП", classification="all", session=session)
    assert s["facets"]["types"]["all"] == 3
    # глагол с другим окончанием
    _add(session, "e", "Кэшбэк не начислили за коммуналку")
    assert [r["title"] for r in repo.list_catalog_cases(
        query_text="кэшбэк не начисляют", classification="all", session=session)] == [
        "Кэшбэк не начислили за коммуналку"]


def test_any_dash_and_no_noun_stemming(session):
    _unicode_lower(session)
    _add(session, "t", "Т\u2011Банк заблокировал карту")
    _add(session, "s", "Спор с банком о комиссии")
    _add(session, "b", "Банкомат не выдал наличные")
    found = repo.list_catalog_cases(query_text="Т-Банк", classification="all", session=session)
    assert [r["title"] for r in found] == ["Т\u2011Банк заблокировал карту"]
    found = repo.list_catalog_cases(query_text="банкомат", classification="all", session=session)
    assert [r["title"] for r in found] == ["Банкомат не выдал наличные"]


def test_empty_confirmed_view_counts_hidden_offtopic_matches(session):
    """Тип «уязвимости и схемы», совпадение только среди скрытых — кнопка к ним."""
    _unicode_lower(session)
    _add(session, "x", "Буланова рассказала о мечте", url="https://lenta.ru/n/1",
         classification="not_confirmed", is_loophole=False)
    relevance.tag_batch(session, after_id=0, limit=100)
    s = repo.catalog_summary(query_text="буланова", classification="confirmed", session=session)
    assert s["facets"]["types"]["all"] == 0
    assert s["facets"]["offtopic"] == 0 and s["facets"]["offtopic_all"] == 1


def test_hay_expression_matches_index_migration():
    sql = (ROOT / "migrations" / "093_loophole_word_search_index.sql").read_text(encoding="utf-8")
    assert repo._HAY_SQL.format(p="") in sql


def test_offtopic_rules():
    r = relevance.offtopic_reason
    assert r(title="Владимир Путин принял участие в форуме", snippet=None, raw_text=None,
             url="https://ok.ru/region/topic/1") == "no_bank_terms"
    assert r(title="Обрушение на шахте", snippet="спасатели", raw_text=None,
             url="https://lenta.ru/news/1") == "no_bank_terms"
    assert r(title="Украине спрогнозировали судьбу банкрота", snippet=None, raw_text=None,
             url="https://lenta.ru/n/2") == "no_bank_terms"           # «банкрот» — не банк
    assert r(title="Как выгодно платить налоги (кешбэк, кредитки)", snippet=None, raw_text=None,
             url="https://hranidengi.com/t/1") is None
    assert r(title="Счёт заблокирован без объяснений", snippet=None, raw_text=None,
             url="https://x.ru") is None
    # деньги и мошенничество без слова «банк» не скрываются
    for title in ("Аферисты выманили у пенсионерки 2 млн рублей",
                  "Списали деньги без моего ведома", "Перевёл 50 тысяч незнакомцу",
                  "Как вернуть деньги, если обманули на Авито", "ЦБ сохранил ключевую ставку",
                  "Sberbank Online не работает", "Tinkoff Black: новые условия"):
        assert r(title=title, snippet=None, raw_text=None, url="https://ok.ru/x/1") is None, title
    for title in ("Звонок от службы безопасности: назвал код из СМС", "Тинькоф заблокировал",
                  "Подозрительная транзакция", "Apple Pay перестал работать",
                  "СБОЛ не открывается", "Блокировка по 161", "Фишинговый сайт",
                  "Вывод средств с биржи"):
        assert r(title=title, snippet=None, raw_text=None, url="https://x.ru/1") is None, title
    assert r(title="Новая выставка в музее", snippet=None, raw_text=None,
             url="https://lenta.ru/n/3") == "no_bank_terms"
    assert r(title="Переезжаем", snippet=None, raw_text=None,
             url="https://t.me/s/sberbank/123") is None
    # сообщество банка в соцсети — о банке, даже без банковских слов в посте
    assert r(title="Переезжаем в новый офис", snippet=None, raw_text=None,
             url="https://ok.ru/sber/topic/1") is None


def test_offtopic_hidden_by_default_and_never_hides_findings(session):
    _unicode_lower(session)
    news = _add(session, "n", "Буланова рассказала о мечте", url="https://lenta.ru/n/1",
                classification="not_confirmed", is_loophole=False)
    _add(session, "m", "Ручное решение без банковских слов", url="https://lenta.ru/n/2",
         classification="not_confirmed", is_loophole=False, model="manual")
    _add(session, "f", "Находка без банковских слов", url="https://lenta.ru/n/3")
    _add(session, "k", "Кэшбэк за переводы через СБП", classification="not_confirmed",
         is_loophole=False)
    last, marks = relevance.tag_batch(session, after_id=0, limit=100)
    assert last is not None and len(marks) == 4
    rows = dict(session.execute(text(
        "SELECT record_id, offtopic_reason FROM loophole_record_topic")).all())
    assert rows[news] == "no_bank_terms"
    shown = {r["title"] for r in repo.list_catalog_cases(classification="all", session=session)}
    assert "Буланова рассказала о мечте" not in shown
    assert {"Ручное решение без банковских слов", "Находка без банковских слов",
            "Кэшбэк за переводы через СБП"} <= shown
    every = repo.list_catalog_cases(classification="all", topic="all", session=session)
    assert len(every) == 4
    only = repo.list_catalog_cases(classification="all", topic="offtopic", session=session)
    assert [r["title"] for r in only] == ["Буланова рассказала о мечте"]
    s = repo.catalog_summary(classification="all", session=session)
    assert s["facets"]["offtopic"] == 1 and s["totals"]["offtopic"] == 1
    assert repo.catalog_summary(classification="confirmed", session=session)["facets"]["offtopic"] == 0
    # повторная разметка ничего не меняет
    assert relevance.tag_batch(session, after_id=0, limit=100)[1] == []
    # новая версия правила пересчитывает старые отметки
    old = relevance.RULE_VERSION
    try:
        relevance.RULE_VERSION = old + 1
        assert len(relevance.tag_batch(session, after_id=0, limit=100)[1]) == 4
    finally:
        relevance.RULE_VERSION = old


def test_content_backfill_resets_offtopic_mark(session):
    """Догрузили текст о банке — запись снова видна и будет перепроверена."""
    _unicode_lower(session)
    rid = _add(session, "z", "Новости недели", url="https://lenta.ru/n/9",
               classification="not_confirmed", is_loophole=False)
    relevance.tag_batch(session, after_id=0, limit=100)
    assert repo.list_catalog_cases(classification="all", session=session) == []
    repo.update_content(rid, raw_text="Банк списал деньги со счёта", content_status="full",
                        raw_text_len=27, truncated=False, session=session)
    assert [r["title"] for r in repo.list_catalog_cases(classification="all", session=session)] == [
        "Новости недели"]
    relevance.tag_batch(session, after_id=0, limit=100)
    assert [r["title"] for r in repo.list_catalog_cases(classification="all", session=session)] == [
        "Новости недели"]


def test_export_describes_words_the_server_used():
    from bank_audit.loophole.catalog_export import describe_filters
    rows = dict(describe_filters({"q": "ЦБ отозвал лицензию у банка"}))
    assert rows["Поиск"].startswith("все слова: отозвал, лицензию, банка")
    assert dict(describe_filters({"q": "по на"}))["Поиск"].startswith("фраза «по на»")
    assert "Записи не о банках" not in dict(describe_filters({"q": "", "record_ids": [1, 2]}))


def test_catalog_endpoint_returns_terms_and_validates_topic(client, session):
    access._access(session)
    r = client.get("/api/loophole/catalog?q=кэшбэк%20СБП&classification=all",
                   headers=access._HEADERS)
    assert r.status_code == 200
    assert r.json()["terms"] == [["кэшбэк", "кешбек"], ["сбп", "сбп"]]
    assert client.get("/api/loophole/catalog?topic=xxx",
                      headers=access._HEADERS).status_code == 422
