"""Наш забор страниц как скрапер gpt-researcher.

Их штатный `bs`-скрапер — это requests + BeautifulSoup, и на банковских сайтах
его не хватает: sberbank.ru при HTTP 200 отдаёт 91-символьную заглушку
антибота, каталоги banki.ru и sravni.ru — пустой каркас SPA. У нас для этого
есть fetcher (кэш → HTTP → Playwright) и парсер, сохраняющий таблицы и строки
с числами.

Решение о браузере принимается ПО СОДЕРЖИМОМУ, а не по списку доменов: забрали
дёшево, увидели заглушку или подозрительно пустую страницу — переспросили
браузером. Список доменов пришлось бы вести вручную, и он всё равно устареет
на следующем редизайне.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date

from ...rag import fetcher
from ...rag.parsers.html_parser import parse_html
from ...rag.parsers.pdf_parser import parse_pdf
from ..v2.tools.web_tools import _looks_like_stub
from . import runstate

log = logging.getLogger(__name__)

# Ниже этого объёма страница считается подозрительно пустой: у настоящей
# продуктовой страницы после очистки остаются сотни символов условий.
_TOO_SHORT = 400
# Копию Яндекса старше этого не берём: условия продуктов меняются.
_COPY_MAX_AGE_DAYS = int(os.getenv("SEARCH_COPY_MAX_AGE_DAYS", "90"))
# Строка такой длины — это уже проза, а не заголовок и не пункт меню.
_PROSE_LINE = 120

# Прочитанное и причины нечитаемости живут в состоянии ПРОГОНА (runstate), а
# не в модульных словарях: иначе параллельные вопросы затирают друг друга.
# Различать «организация не раскрывает» и «мы не смогли прочитать» обязательно:
# на странице ВТБ «Сколько делается карта» есть заголовки «Что влияет на время
# изготовления» и «Доставка в цифрах», а чисел нет — их подгружает скрипт.
# Прежний конвейер объявлял это непрозрачностью банка. Это ложный вывод.


# Архив базы знаний (аудит 03.10, ДАН-02): всё, что движок отчётов прочитал,
# раньше терялось — архив рос только из быстрых ответов (44 свежих документа
# из 4 292 за месяц). Теперь прочитанное напрямую (HTTP или браузер) уходит в
# очередь индексации. Копии Яндекса не отдаём: их дата старше дня загрузки, и
# «текущей» версией страницы стала бы старая. Очередь — только поднятая
# приложением (lifespan): в тестах и разовых прогонах архив не трогается.
ARCHIVE_ON = os.getenv("GPTR_ARCHIVE", "1") != "0"
ARCHIVE_MAX = int(os.getenv("GPTR_ARCHIVE_MAX", "40"))
_ARCHIVE_TTL_S = 6 * 3600              # одну и ту же страницу не чаще раза в 6 ч
_archived_at: dict[str, float] = {}
_archived_lock = threading.Lock()
# В архив — только то, что поиск базы знаний потом найдёт (trust ≥ 0.5):
# сайт банка, регулятор или известный источник. Неизвестный домен заводится
# блогом с весом 0.3 — такую страницу резали и эмбеддили впустую.
_ARCHIVE_MIN_W = 0.5
_trusted: dict[str, tuple[bool, float]] = {}


def _archivable(url: str) -> bool:
    from ...rag.trust import domain_of, is_bank_official, is_govt_official
    if is_bank_official(url)[0] or is_govt_official(url)[0]:
        return True
    d = domain_of(url)
    if not d:
        return False
    hit = _trusted.get(d)
    if hit and time.time() - hit[1] < 3600:
        return hit[0]
    from sqlalchemy import text
    from ... import db
    with db.session() as s:
        w = s.execute(text("SELECT max(weight) FROM source_trust WHERE domain = :d"),
                      {"d": d}).scalar()
    ok = w is not None and float(w) >= _ARCHIVE_MIN_W
    _trusted[d] = (ok, time.time())
    return ok


def _is_skeleton(text: str) -> bool:
    """Каркас страницы: заголовки есть, содержания нет.

    Признак структурный и не знает ни сайта, ни языка: в тексте нет ни одной
    строки прозаической длины, зато есть несколько коротких строк-заголовков.
    Так выглядит SPA, отдавшая разметку без данных.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if len(text or "") > 4000:
        return False                 # длинная страница — точно не каркас
    prose = sum(1 for l in lines if len(l) >= _PROSE_LINE)
    headings = sum(1 for l in lines if l.startswith("#"))
    return prose == 0 and (headings >= 3 or len(lines) >= 6)

class AuditLensScraper:
    """Забор страницы нашим fetcher-ом с эскалацией до браузера."""

    def __init__(self, link: str, session=None, scraper_name: str = "auditlens",
                 state=None):
        self.link = link
        self.session = session
        # Состояние связывается при ВЫБОРЕ скрапера (см. install): сам scrape()
        # исполняется в пуле потоков, куда contextvars не переносятся.
        self.state = state or runstate.current()
        self.is_pdf = False          # выясняется при чтении, нужно в scrape()
        self._raw = None             # байты варианта, чей текст вернули (для архива)

    def _read(self, *, browser: bool) -> tuple[str, str]:
        try:
            # Дешёвый проход — строго без браузера: браузер решает scrape(),
            # после копии Яндекса. Иначе fetch звал его сам, а потом scrape()
            # ещё раз — два ожидания по 20 с на одну закрытую страницу.
            res = fetcher.fetch(self.link, prefer_browser=browser,
                                force_refresh=browser, browser_fallback=browser)
        except Exception as e:
            log.info("fetch %s: %s", self.link[:80], type(e).__name__)
            return "", ""
        if not res or not res.content:
            return "", ""
        self._last_raw = (res.content, res.content_type, res.final_url or self.link,
                          "browser" if browser else "http")
        return self._parse(res.content, res.content_type, res.final_url or self.link)

    def _parse(self, content: bytes, content_type: str | None,
               url: str) -> tuple[str, str]:
        # PDF разбираем СВОИМ парсером (таблицы + провенанс), иначе документ
        # уходил штатному классу gpt-researcher и в факты не попадал вовсе:
        # реестр страниц заполняет только этот скрапер. Для регуляторных
        # документов, которые почти всегда PDF, это была дыра в покрытии.
        ctype = (content_type or "").lower()
        self.is_pdf = ("pdf" in ctype
                       or self.link.split("?", 1)[0].lower().endswith(".pdf")
                       or content[:5] == b"%PDF-")
        try:
            doc = (parse_pdf(content, url) if self.is_pdf else parse_html(content, url))
        except Exception as e:
            log.info("parse %s: %s", self.link[:80], type(e).__name__)
            return "", ""
        # Датируем из уже скачанной разметки. Аудитор должен видеть, когда
        # источник опубликован, а не когда мы его прочитали: «шесть месяцев
        # назад» и «сегодня» — разный вес свидетельства, а без даты отчёт
        # выглядит одинаково свежим целиком.
        if not self.is_pdf:
            try:
                from ...digest.news import date_from_html
                # Кодировка нас не волнует: даты в метатегах — латиница и
                # цифры, а «ignore» просто выбросит непрочитанные байты.
                ts = date_from_html(content.decode("utf-8", "ignore"))
                if ts:
                    self.state.page_dates[self.link] = ts.date().isoformat()
            except Exception:      # noqa: BLE001 — дата необязательна
                pass
        return (doc.text or ""), (getattr(doc, "title", "") or "")

    def _read_copy(self) -> tuple[str, str, str | None]:
        """Сохранённая копия страницы из индекса Яндекса.

        Сайты банков закрыты антиботом: sberbank.ru отдаёт заглушку в 91
        символ, браузер ждёт до 20 с и тоже часто проигрывает. Копия той же
        страницы приходит за 1–3 с целиком (замер 24.09.2026: 9 из 10 страниц
        Сбера). Слишком старую копию не берём: условия продуктов меняются.
        """
        from ...rag import search_gateway
        try:
            copy = search_gateway.read_cached_copy(self.link)
        except Exception as e:      # noqa: BLE001 — копия необязательна
            log.info("копия %s: %s", self.link[:80], type(e).__name__)
            return "", "", None
        if copy is None:
            return "", "", None
        if copy.copy_date:
            try:
                age = (date.today() - date.fromisoformat(copy.copy_date)).days
            except ValueError:
                age = 0
            if age > _COPY_MAX_AGE_DAYS:
                log.info("копия %s: от %s — старше %d дн, не берём",
                         self.link[:70], copy.copy_date, _COPY_MAX_AGE_DAYS)
                return "", "", None
        text, title = self._parse(copy.content, "text/html", self.link)
        return text, title, copy.copy_date

    def _usable(self, text: str, title: str) -> bool:
        return (len(text) >= _TOO_SHORT and not _looks_like_stub(title, text)
                and not _is_skeleton(text))

    def _to_archive(self) -> None:
        """Отдать прочитанное в архив базы знаний; никогда не роняет отчёт."""
        r, st = self._raw, self.state
        if not (ARCHIVE_ON and r and st is not None):
            return
        try:
            from ...rag import ingest_queue
            if not ingest_queue.is_running():
                return
            now = time.time()
            content, ctype, final_url, via = r
            # недоверенные страницы не занимают места в лимите отчёта
            if not _archivable(final_url):
                return
            # страница, недавно ушедшая в архив из другого отчёта, слот
            # лимита этого отчёта не занимает
            with _archived_lock:
                if now - _archived_at.get(self.link, 0) < _ARCHIVE_TTL_S:
                    return
            with st.lock:
                if self.link in st.archived or len(st.archived) >= ARCHIVE_MAX:
                    return
                st.archived.add(self.link)
            with _archived_lock:
                _archived_at[self.link] = now
            ok = ingest_queue.submit(final_url, prefer_browser=(via == "browser"),
                                content=content, content_type=ctype, final_url=final_url,
                                origin={**(st.origin or {"kind": "report"}), "fetch_mode": via})
            if ok is False:          # очередь переполнена — не прятать страницу на 6 ч
                with _archived_lock:
                    _archived_at.pop(self.link, None)
                with st.lock:
                    st.archived.discard(self.link)
        except Exception as e:  # noqa: BLE001 — архив не роняет отчёт
            log.info("архив %s: %s", self.link[:70], type(e).__name__)

    def scrape(self) -> tuple[str, list, str]:
        self._last_raw = None
        text, title = self._read(browser=False)
        self._raw = self._last_raw
        cheap_bad = len(text) < _TOO_SHORT or _looks_like_stub(title, text)
        if not self.is_pdf and (cheap_bad or _is_skeleton(text)):
            # Сначала копия Яндекса (секунды), и только потом браузер (десятки
            # секунд, и на сайтах банков часто тот же отказ). Каркасу SPA браузер
            # по-прежнему не положен — только копия: так было и до неё.
            ctext, ctitle, cdate = self._read_copy()
            if self._usable(ctext, ctitle):
                log.info("scrape %s: дёшево не вышло (%d символов) — прочитано "
                         "из сохранённой копии Яндекса от %s", self.link[:70],
                         len(text), cdate or "?")
                text, title = ctext, ctitle
                self._raw = None             # копию Яндекса в архив не кладём
                self.state.cached_copies[self.link] = cdate or ""
            elif cheap_bad:
                log.info("scrape %s: дёшево не вышло (%d символов) — идём браузером",
                         self.link[:70], len(text))
                self._last_raw = None
                btext, btitle = self._read(browser=True)
                if len(btext) > len(text):
                    text, title = btext, btitle
                    self._raw = self._last_raw
        # Причину фиксируем ВСЕГДА, даже если текст всё же вернули: отчёт
        # обязан отличать «нет данных» от «не смогли прочитать».
        if not text:
            self.state.note_unreadable(self.link, "пустой ответ")
        elif _looks_like_stub(title, text):
            self.state.note_unreadable(self.link, "защита от ботов")
        elif len(text) < _TOO_SHORT:
            self.state.note_unreadable(self.link, "почти пустая страница")
        elif not self.is_pdf and _is_skeleton(text):
            self.state.note_unreadable(
                self.link, "каркас без содержимого (данные грузит скрипт)")
        else:
            self.state.note_page(self.link, text)
            self._to_archive()
            return text, [], title
        if text:
            self.state.pages[self.link] = text   # негодную оставляем помеченной
        return text, [], title


def install() -> None:
    """Регистрирует наш скрапер в реестре gpt-researcher."""
    from gpt_researcher.scraper import scraper as _s

    if getattr(_s.Scraper, "_auditlens_patched", False):
        return
    original = _s.Scraper.get_scraper

    def get_scraper(self, link):
        # PDF и arxiv оставляем их классам: у нас fetcher вернёт байты, которые
        # HTML-парсер не поймёт.
        path = link.split("?", 1)[0].lower()
        if path.endswith(".pdf") or "arxiv.org" in link:
            return original(self, link)
        # Здесь мы ещё в контексте прогона — связываем состояние сейчас, потому
        # что сам scrape() уедет в пул потоков без контекста.
        state = runstate.current()

        def make(link_, session=None, *a, **kw):
            return AuditLensScraper(link_, session, state=state)

        return make

    _s.Scraper.get_scraper = get_scraper
    _s.Scraper._auditlens_patched = True
