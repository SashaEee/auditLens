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

from ...rag import fetcher
from ...rag.parsers.html_parser import parse_html
from ...rag.parsers.pdf_parser import parse_pdf
from ..v2.tools.web_tools import _looks_like_stub
from . import runstate

log = logging.getLogger(__name__)

# Ниже этого объёма страница считается подозрительно пустой: у настоящей
# продуктовой страницы после очистки остаются сотни символов условий.
_TOO_SHORT = 400
# Строка такой длины — это уже проза, а не заголовок и не пункт меню.
_PROSE_LINE = 120

# Прочитанное и причины нечитаемости живут в состоянии ПРОГОНА (runstate), а
# не в модульных словарях: иначе параллельные вопросы затирают друг друга.
# Различать «организация не раскрывает» и «мы не смогли прочитать» обязательно:
# на странице ВТБ «Сколько делается карта» есть заголовки «Что влияет на время
# изготовления» и «Доставка в цифрах», а чисел нет — их подгружает скрипт.
# Прежний конвейер объявлял это непрозрачностью банка. Это ложный вывод.


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

    def _read(self, *, browser: bool) -> tuple[str, str]:
        try:
            res = fetcher.fetch(self.link, prefer_browser=browser,
                                force_refresh=browser)
        except Exception as e:
            log.info("fetch %s: %s", self.link[:80], type(e).__name__)
            return "", ""
        if not res or not res.content:
            return "", ""
        # PDF разбираем СВОИМ парсером (таблицы + провенанс), иначе документ
        # уходил штатному классу gpt-researcher и в факты не попадал вовсе:
        # реестр страниц заполняет только этот скрапер. Для регуляторных
        # документов, которые почти всегда PDF, это была дыра в покрытии.
        ctype = (res.content_type or "").lower()
        self.is_pdf = ("pdf" in ctype
                       or self.link.split("?", 1)[0].lower().endswith(".pdf")
                       or res.content[:5] == b"%PDF-")
        try:
            doc = (parse_pdf(res.content, res.final_url or self.link)
                   if self.is_pdf
                   else parse_html(res.content, res.final_url or self.link))
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
                ts = date_from_html(res.content.decode("utf-8", "ignore"))
                if ts:
                    self.state.page_dates[self.link] = ts.date().isoformat()
            except Exception:      # noqa: BLE001 — дата необязательна
                pass
        return (doc.text or ""), (getattr(doc, "title", "") or "")

    def scrape(self) -> tuple[str, list, str]:
        text, title = self._read(browser=False)
        if not self.is_pdf and (len(text) < _TOO_SHORT
                                or _looks_like_stub(title, text)):
            log.info("scrape %s: дёшево не вышло (%d символов) — идём браузером",
                     self.link[:70], len(text))
            btext, btitle = self._read(browser=True)
            if len(btext) > len(text):
                text, title = btext, btitle
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
