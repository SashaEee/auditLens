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
from ..v2.tools.web_tools import _looks_like_stub

log = logging.getLogger(__name__)

# Ниже этого объёма страница считается подозрительно пустой: у настоящей
# продуктовой страницы после очистки остаются сотни символов условий.
_TOO_SHORT = 400


class AuditLensScraper:
    """Забор страницы нашим fetcher-ом с эскалацией до браузера."""

    def __init__(self, link: str, session=None, scraper_name: str = "auditlens"):
        self.link = link
        self.session = session

    def _read(self, *, browser: bool) -> tuple[str, str]:
        try:
            res = fetcher.fetch(self.link, prefer_browser=browser,
                                force_refresh=browser)
        except Exception as e:
            log.info("fetch %s: %s", self.link[:80], type(e).__name__)
            return "", ""
        if not res or not res.content:
            return "", ""
        try:
            doc = parse_html(res.content, res.final_url or self.link)
        except Exception as e:
            log.info("parse %s: %s", self.link[:80], type(e).__name__)
            return "", ""
        return (doc.text or ""), (getattr(doc, "title", "") or "")

    def scrape(self) -> tuple[str, list, str]:
        text, title = self._read(browser=False)
        if len(text) < _TOO_SHORT or _looks_like_stub(title, text):
            log.info("scrape %s: дёшево не вышло (%d символов) — идём браузером",
                     self.link[:70], len(text))
            btext, btitle = self._read(browser=True)
            if len(btext) > len(text):
                text, title = btext, btitle
        return text, [], title


def install() -> None:
    """Регистрирует наш скрапер в реестре gpt-researcher."""
    from gpt_researcher.scraper import scraper as _s

    original = _s.Scraper.get_scraper

    def get_scraper(self, link):
        # PDF и arxiv оставляем их классам: у нас fetcher вернёт байты, которые
        # HTML-парсер не поймёт.
        path = link.split("?", 1)[0].lower()
        if path.endswith(".pdf") or "arxiv.org" in link:
            return original(self, link)
        return AuditLensScraper

    _s.Scraper.get_scraper = get_scraper
