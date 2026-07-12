"""Тесты дайджеста «Обзора» без сети и БД: парсеры источников, дедуп, ключи."""
from datetime import datetime, timezone
from pathlib import Path

from bank_audit.digest.news import (_dedupe, _norm_title, _parse_rss,
                                    _parse_rss_fallback, _parse_tg)

FIXTURES = Path(__file__).parent / "fixtures"


RSS_OK = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Новость раз</title><link>https://cbr.ru/press/1</link>
<pubDate>Wed, 02 Jul 2026 10:00:00 +0300</pubDate>
<description>Описание &lt;b&gt;раз&lt;/b&gt;</description></item>
<item><title>Новость два</title><link>https://cbr.ru/press/2</link></item>
</channel></rss>"""

# banki.ru-стиль: сырой <script> с "<" внутри item → ElementTree падает
RSS_BROKEN = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<script>for (i=0; i<x.length; i++) {}</script>
<item><title><![CDATA[Сбой в приложении банка]]></title>
<link>https://www.banki.ru/news/1</link>
<pubDate>Wed, 02 Jul 2026 09:00:00 +0300</pubDate>
<description><![CDATA[Клиенты жалуются <br> массово]]></description></item>
</channel></rss>"""

SRC = {"key": "test", "tag": "market"}


def test_rss_ok_parses():
    items = _parse_rss(RSS_OK, SRC)
    assert len(items) == 2
    assert items[0]["title"] == "Новость раз"
    assert items[0]["url"] == "https://cbr.ru/press/1"
    assert items[0]["ts"].tzinfo is not None
    assert "раз" in items[0]["snippet"] and "<b>" not in items[0]["snippet"]
    assert items[1]["ts"] is None          # без pubDate — не падаем


def test_rss_broken_falls_back_to_regex():
    items = _parse_rss(RSS_BROKEN, SRC)    # ET падает → regex-fallback
    assert len(items) == 1
    assert items[0]["title"] == "Сбой в приложении банка"
    assert items[0]["url"] == "https://www.banki.ru/news/1"
    assert "массово" in items[0]["snippet"]
    assert items[0]["ts"] is not None


def test_rss_fallback_direct():
    items = _parse_rss_fallback(RSS_BROKEN, SRC)
    assert len(items) == 1 and items[0]["title"].startswith("Сбой")


def test_tg_parses_real_fixture():
    """Живой снапшот t.me/s/banksta: каждый пост = data-post + text + time."""
    html = (FIXTURES / "tg_banksta.html").read_text()
    items = _parse_tg(html, {"key": "tg_banksta", "tag": "incident"})
    assert len(items) >= 8                       # 19 постов минус медиа/короткие
    for it in items:
        assert it["url"].startswith("https://t.me/banksta/")
        assert it["ts"] is not None and it["ts"].tzinfo is not None
        assert len(it["title"]) >= 15
        assert it["tag"] == "incident"


def test_dedupe_same_title_cross_host():
    ts = datetime.now(timezone.utc)
    items = [
        {"title": "ЦБ повысил ключевую ставку до шестнадцати процентов",
         "url": "https://cbr.ru/1", "ts": ts},
        {"title": "ЦБ повысил ключевую ставку до шестнадцати процентов",
         "url": "https://rbc.ru/2", "ts": ts},           # перепечатка — режем
        {"title": "Другая новость про вклады банков России сегодня",
         "url": "https://rbc.ru/3", "ts": ts},
    ]
    out = _dedupe(items)
    assert len(out) == 2


def test_norm_title():
    assert _norm_title("ЦБ: повысил, ставку!") == _norm_title("цб повысил ставку")
