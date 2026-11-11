"""Каналы и исследования из списка руководства (11.11.2026): страницы со списком
публикаций как источник потока новостей, чтение PDF, полка «Исследования и обзоры»,
подписи источников и «пропущенное» у ночного судьи."""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.digest import judge, news as nm, newsflow as nf, writer  # noqa: E402
from bank_audit.web import sources_catalog as sc  # noqa: E402

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
SRC = {"key": "t_research", "kind": "page", "url": "https://www.example.org/list/",
       "link": r"^/a/[\w-]{3,}/$", "tag": "research", "cls": "bank", "dimension": "market"}


# ── разбор страницы-списка ───────────────────────────────────────────────────

def test_listing_takes_matching_links_with_date_before():
    page = ('<div>23 сентября 2026 года</div><a href="/a/first-report/">Банки и экономика в цифрах</a>'
            '<p>Обзор сектора за квартал: кредиты, вклады и прибыль банков.</p>'
            '<div>14.09.2026</div><a href="/a/second/">Изменение требований к стресс-тестированию</a>'
            '<a href="/b/other/">Раздел сайта, не публикация</a>')
    got = nf.parse_listing(page, SRC["url"], {**SRC, "date": "before"})
    assert [g["url"] for g in got] == ["https://www.example.org/a/first-report/",
                                       "https://www.example.org/a/second/"]
    assert [g["date"] for g in got] == [date(2026, 9, 23), date(2026, 9, 14)]
    assert got[0]["note"].startswith("Обзор сектора за квартал")


def test_listing_date_after_does_not_take_the_next_cards_date():
    page = ('<a href="/a/one-report/">Ипотечное кредитование в 2026 году</a><span>05.11.2026</span>'
            '<a href="/a/two-report/">Коллекторский рынок по итогам года</a>'
            '<a href="/a/three-report/">Рынок кредитования малого бизнеса</a><span>01.10.2026</span>')
    got = nf.parse_listing(page, SRC["url"], {**SRC, "date": "after"})
    assert [g["date"] for g in got] == [date(2026, 11, 5), None, date(2026, 10, 1)]


def test_note_after_the_link_stops_at_the_cards_date_and_drops_a_cut_tag():
    tail = "<div>Вся аналитика по тематике</div>" + "x" * 1300
    page = ('<a href="/a/one-report/">Итоги банковского сектора за полугодие</a>'
            '<p>Доля десяти крупнейших банков в прибыли сектора снизилась с 75 до 72 процентов.</p>'
            '<span>15.10.2026</span>' + tail +
            '<a href="/a/two-report/">Подтверждён рейтинг банка</a><span>29.10.2026</span>'
            '<p>Введите результат вычисления:</p><img src="/captcha' + "y" * 1300)
    got = nf.parse_listing(page, SRC["url"], {**SRC, "date": "after"})
    assert got[0]["date"] == date(2026, 10, 15)
    assert got[0]["note"] == "Доля десяти крупнейших банков в прибыли сектора снизилась с 75 до 72 процентов."
    assert got[1]["date"] == date(2026, 10, 29) and got[1]["note"] == ""
    # без указанной стороны оборванный тег в конце окна в пояснение не попадает
    page2 = '<a href="/a/one-report/">Обзор банковского сектора</a><p>' + "Текст пояснения к обзору. " * 60 + '<img src="/x'
    assert "<" not in nf.parse_listing(page2, SRC["url"], SRC)[0]["note"]


def test_listing_reads_the_date_from_the_address_and_ignores_text_without_a_side():
    src = {**SRC, "link": r"^/files/\d+/note_[^/]+\.pdf$"}
    page = ('<span>01.01.2020</span><a href="/files/197439/note_20260821_ddkp.pdf">Нейтральная ставка: оценки '
            '2026 года</a><a href="/files/1/note_draft.pdf">Потенциальный выпуск в российской экономике</a>'
            '<span>02.02.2021</span>')
    got = nf.parse_listing(page, SRC["url"], src)
    assert [g["date"] for g in got] == [date(2026, 8, 21), None]


def test_listing_skips_other_hosts_pictures_and_repeats():
    page = ('<a href="https://other.example.com/a/foreign-report/">Чужой сайт с таким же адресом</a>'
            '<a href="/a/main-report/"><img src="x.png"></a>'
            '<a href="/a/main-report/">Итоги банковского сектора за полугодие</a>'
            '<a href="/a/main-report/">Итоги банковского сектора за полугодие</a>'
            '<a href="/a/main-report/#comments">Комментарии</a>')
    got = nf.parse_listing(page, SRC["url"], SRC)
    assert [(g["url"], g["title"]) for g in got] == [
        ("https://www.example.org/a/main-report/", "Итоги банковского сектора за полугодие")]


def test_note_is_dropped_when_it_only_repeats_the_title():
    page = ('<a href="/a/main-report/">Банковский сектор в глобальной экономике</a>'
            '<div>Банковский сектор в глобальной экономике</div>')
    assert nf.parse_listing(page, SRC["url"], SRC)[0]["note"] == ""


# ── что считать новым ────────────────────────────────────────────────────────

def _undated(n):
    return [{"url": f"https://www.example.org/a/report-{i}/", "title": f"Публикация о банках номер {i}",
             "date": None, "note": ""} for i in range(n)]


def test_first_pass_remembers_everything_on_the_site_as_old():
    rows = nf.page_rows(SRC, _undated(12), set(), True, NOW)
    assert len(rows) == 12
    assert all(NOW - r["ts"] >= timedelta(days=nf.PAGE_OLD_DAYS) for r in rows)
    # порядок страницы сохраняется: верхняя публикация — самая «свежая» из старых
    assert rows[0]["ts"] > rows[1]["ts"] > rows[11]["ts"]
    assert rows[0]["s"] == "t_research" and rows[0]["tag"] == "research"


def test_new_publication_without_a_date_gets_the_time_it_was_found():
    assert nf.page_rows(SRC, _undated(1), set(), False, NOW)[0]["ts"] == NOW


def test_known_links_are_not_written_again():
    found = _undated(12)
    known = {nm._url_hash(f["url"]) for f in found[:11]}
    rows = nf.page_rows(SRC, found, known, False, NOW)
    assert [r["u"] for r in rows] == ["https://www.example.org/a/report-11/"] and rows[0]["ts"] == NOW


def test_many_new_undated_links_at_once_mean_changed_addresses_not_news():
    rows = nf.page_rows(SRC, _undated(nf.PAGE_NEW_MAX + 1), set(), False, NOW)
    assert all(NOW - r["ts"] >= timedelta(days=nf.PAGE_OLD_DAYS) for r in rows)


def test_dated_publication_today_is_fresh_and_earlier_one_keeps_its_day():
    found = [{"url": "https://www.example.org/a/today/", "title": "Сегодняшний обзор банков",
              "date": date(2026, 11, 11), "note": ""},
             {"url": "https://www.example.org/a/earlier/", "title": "Обзор банков за сентябрь",
              "date": date(2026, 9, 23), "note": "Пояснение"}]
    rows = nf.page_rows(SRC, found, set(), True, NOW)
    assert rows[0]["ts"] == NOW
    assert rows[1]["ts"] == datetime(2026, 9, 23, 12, tzinfo=nf.MSK) and rows[1]["b"] == "Пояснение"


class _R:
    def __init__(self, code, text_):
        self.status_code, self.text = code, text_


def test_fetch_page_keeps_what_was_read_and_reports_the_failed_page(monkeypatch):
    pages = {"https://www.example.org/list/one/": _R(200, '<a href="/a/main-report/">Итоги банковского сектора</a>'),
             "https://www.example.org/list/two/": _R(503, "")}
    monkeypatch.setattr(nm, "_get", lambda u: pages[u])
    monkeypatch.setattr(nf, "_page_known", lambda hashes: set())
    rows, err = nf._fetch_page({**SRC, "pages": list(pages)})
    assert [r["u"] for r in rows] == ["https://www.example.org/a/main-report/"]
    assert "two" in err and "503" in err
    # ссылок нет вовсе (сменилась вёрстка) — источник помечается ошибкой, а не молчит
    monkeypatch.setattr(nm, "_get", lambda u: _R(200, "<p>пусто</p>"))
    rows, err = nf._fetch_page({**SRC, "pages": ["https://www.example.org/list/one/"]})
    assert rows == [] and "не найдено" in err


def test_page_that_failed_on_day_one_is_still_remembered_as_old_later(monkeypatch):
    """Первая страница источника уже прочитана, вторая открылась только сейчас:
    её две публикации без даты лежали на сайте давно — это не новости дня."""
    one = "https://www.example.org/list/one/"
    two = "https://www.example.org/list/two/"
    pages = {one: _R(200, '<a href="/a/known-report/">Известный доклад о банках</a>'
                          '<a href="/a/fresh-report/">Свежий доклад о вкладах граждан</a>'),
             two: _R(200, '<a href="/a/old-paper-one/">Давняя работа о кредитах</a>'
                          '<a href="/a/old-paper-two/">Давняя работа о платежах</a>')}
    monkeypatch.setattr(nm, "_get", lambda u: pages[u])
    known = {nm._url_hash("https://www.example.org/a/known-report/")}
    monkeypatch.setattr(nf, "_page_known", lambda hashes: known)
    rows, err = nf._fetch_page({**SRC, "pages": [one, two]})
    by = {r["u"].rsplit("/", 2)[-2]: r["ts"] for r in rows}
    now = datetime.now(timezone.utc)
    assert err is None and set(by) == {"fresh-report", "old-paper-one", "old-paper-two"}
    assert now - by["fresh-report"] < timedelta(minutes=1)
    assert now - by["old-paper-one"] >= timedelta(days=nf.PAGE_OLD_DAYS)
    assert now - by["old-paper-two"] >= timedelta(days=nf.PAGE_OLD_DAYS)


def test_publication_listed_on_two_pages_is_written_once(monkeypatch):
    link = '<a href="/a/same-report/">Один и тот же доклад о банках</a>'
    monkeypatch.setattr(nm, "_get", lambda u: _R(200, link))
    monkeypatch.setattr(nf, "_page_known", lambda hashes: set())
    rows, _err = nf._fetch_page({**SRC, "pages": ["https://www.example.org/list/one/",
                                                 "https://www.example.org/list/two/"]})
    assert [r["u"] for r in rows] == ["https://www.example.org/a/same-report/"]


# ── источники ────────────────────────────────────────────────────────────────

def test_every_source_is_named_and_page_sources_are_well_formed():
    keys = [s["key"] for s in nm.SOURCES]
    assert len(keys) == len(set(keys))
    for s in nm.SOURCES:
        assert s["kind"] in ("rss", "tg", "page"), s["key"]
        assert sc.news_source_label(s["key"]) != s["key"], f"нет подписи у {s['key']}"
        if s["kind"] == "page":
            re.compile(s["link"])
            assert s.get("date") in (None, "before", "after")
            assert all(u.startswith("https://") for u in s.get("pages") or [s["url"]])
    assert set(nm.RESEARCH_KEYS) == {s["key"] for s in nm.SOURCES if s["kind"] == "page"}
    for k in ("tg_truba", "tg_ipoteka", "tg_fm_invest", "tg_dengi_efir", "tg_bitkogan", "tg_kot_finance",
              "tg_rapsi", "tg_ej", "tg_expoforum", "tg_schema", "tg_finpodbor"):
        assert k in keys


def test_catalog_shows_research_pages_in_plain_words():
    rows = {r["domain"]: r for r in sc._news_sources()}
    r = rows["Эксперт РА — исследования"]
    assert r["kind"] == "Страница публикаций" and r["role"] == "Исследования и аналитика"
    assert rows["Труба под Неглинной"]["kind"] == "Телеграм-канал"


# ── PDF ──────────────────────────────────────────────────────────────────────

def _pdf(text_: str) -> bytes:
    """Одностраничный PDF с текстовым слоем (латиница, встроенный шрифт Helvetica)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text_}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF"
    return out


def test_pdf_is_read_as_text_and_broken_file_gives_nothing():
    doc = _pdf("Bank of Russia directive on consumer loans effective from January 2027")
    assert writer._is_pdf(doc) and writer._is_pdf(b"x", "application/pdf; charset=binary")
    assert "directive on consumer loans" in writer._pdf_text(doc, 6000)
    assert writer._pdf_text(doc, 20) == "Bank of Russia direc"
    assert writer._pdf_text(b"%PDF-1.7 <>/Filter/FlateDecode/ID[<9BECE185>]", 6000) == ""


def test_bodies_never_store_raw_bytes_of_a_file(monkeypatch):
    import httpx
    doc = _pdf("Information letter on mortgage payment holidays for retail borrowers")
    answers = {
        "https://reg.example/File/8619": (200, doc, {"content-type": "application/pdf"}),
        "https://reg.example/archive.zip": (200, b"PK\x03\x04\x14\x00\x00\x00" + b"\x9b" * 400, {}),
        "https://reg.example/news.html": (200, "<html><body><article><p>Регулятор сообщил о новых требованиях "
                                               "к выдаче потребительских кредитов гражданам.</p></article></body></html>"
                                               .encode(), {"content-type": "text/html; charset=utf-8"}),
        "https://reg.example/gone": (404, b"", {}),
    }

    class Resp:
        def __init__(self, code, content, headers):
            self.status_code, self.content, self.headers = code, content, headers

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return Resp(*answers[url])

    monkeypatch.setattr(httpx, "Client", Client)
    got = writer._news_bodies(list(answers), max_chars=6000, max_n=10)
    assert "mortgage payment holidays" in got["https://reg.example/File/8619"]
    assert got["https://reg.example/archive.zip"] == "" and got["https://reg.example/gone"] == ""
    assert "новых требованиях" in got["https://reg.example/news.html"]
    assert all("FlateDecode" not in v and "\x00" not in v for v in got.values())


# ── полка «Исследования и обзоры» ────────────────────────────────────────────

def _item(i, source, ts, seen, **kw):
    return {"id": i, "source": source, "url": f"https://e.org/{i}", "title": f"Исследование номер {i} о банках",
            "ts": ts, "first_seen": seen, "rel": 6, "value": None, "summary": None, "body": "", **kw}


def test_shelf_limits_one_source_and_hides_dates_it_does_not_know():
    msk = nf.MSK
    seen = NOW
    rows = [_item(1, "asros_research", datetime(2026, 11, 9, 12, tzinfo=msk), seen),          # дата со страницы
            _item(2, "asros_research", datetime(2026, 10, 20, 12, tzinfo=msk), seen),
            _item(3, "asros_research", datetime(2026, 10, 20, 12, tzinfo=msk), seen),
            _item(4, "asros_research", datetime(2026, 9, 23, 12, tzinfo=msk), seen),          # четвёртая — лишняя
            _item(5, "raexpert_research", NOW - timedelta(days=60, minutes=3), seen,          # лежало до нас
                  body="Доля топ-10 банков в прибыли сектора снизилась с 75 до 72 процентов."),
            _item(6, "cbr_research", NOW - timedelta(hours=2), NOW - timedelta(hours=2),      # нашли сразу
                  url="https://e.org/note_20261111.pdf", summary="ЦБ оценил долговую нагрузку заёмщиков.")]
    got = nf.shelf_pick(rows, 8, NOW)
    assert [g["id"] for g in got] == [1, 2, 3, 5, 6]
    by = {g["id"]: g for g in got}
    assert by[1]["date"] == "2026-11-09" and by[1]["new"] is True
    assert by[2]["date"] == "2026-10-20" and by[2]["new"] is False
    assert by[5]["date"] is None and by[5]["new"] is False and by[5]["note"].startswith("Доля топ-10")
    assert by[6]["date"] == "2026-11-11" and by[6]["pdf"] is True
    assert by[6]["note"] == "ЦБ оценил долговую нагрузку заёмщиков."
    assert len(nf.shelf_pick(rows, 2, NOW)) == 2


def test_shelf_note_is_empty_when_body_only_repeats_the_title():
    r = _item(1, "hse_bankslab", NOW, NOW)
    r["body"] = r["title"]
    assert nf.shelf_pick([r], 8, NOW)[0]["note"] == ""


# ── ночной судья ─────────────────────────────────────────────────────────────

def test_judge_does_not_call_a_removed_repeat_a_missed_story(monkeypatch):
    news = {"groups": [{"key": "regulatory", "items": [{"url": "u-pub", "title": "Вышло в выпуске"}]}],
            "pool": [{"url": "u-pub", "title": "Вышло в выпуске"},
                     {"url": "u-repeat", "title": "ЦБ оштрафовал банк — повтор вчерашнего"},
                     {"url": "u-missed", "title": "Сильная новость, которой нет в выпуске"}],
            "repeats": [{"url": "u-repeat", "title": "ЦБ оштрафовал банк — повтор вчерашнего"}]}
    monkeypatch.setattr(judge.store, "_read_day_rows",
                        lambda day: {"news": {"payload": news}, "headline": {"payload": {}}})
    _head, items, pool = judge._issue(date(2026, 11, 11))
    assert [i["url"] for i in items] == ["u-pub"]
    assert [p["url"] for p in pool] == ["u-missed"]
