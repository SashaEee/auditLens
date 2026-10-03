"""Тема документа базы знаний: по адресу, а если адрес её не даёт — по
заголовку и началу текста. Без модели: детерминированные основы слов.

Зачем: тему ставил только адрес, и 44 % документов (новости, PDF, страницы
/help/) были «вне карты» покрытия, а подпись объясняла это актами ЦБ и
судебной практикой, хотя их среди таких документов меньше трети (аудит 03.10,
ДАН-03). Тема по тексту записывается только там, где продуктовой темы в
адресе нет.
"""
from __future__ import annotations

import re

from .url_discovery import classify_url

# продуктовые темы: если адрес назвал одну из них, текст не нужен
PRODUCT_TOPICS = {"mortgage", "cards_credit", "cards_debit", "auto", "deposits", "credits",
                  "cards", "rko", "transfers", "transfers_intl", "investments"}

TEXT_RULES = [
    ("mortgage", r"ипотек|ипотечн"),
    ("cards_credit", r"кредитн\w* карт"),
    ("cards_debit", r"дебетов\w* карт"),
    ("auto", r"автокредит|кредит\w* на (?:авто|автомобил|машин)"),
    ("transfers_intl", r"перевод\w* за (?:рубеж|границ)|swift|трансграничн"),
    ("deposits", r"(?<![а-яё])вклад|(?<![а-яё])депозит|накопительн\w* сч[её]т"),
    ("credits", r"потребительск\w* кредит|кредит\w* наличн|рефинансир|(?<![а-яё])займ|(?<![а-яё])заём"),
    ("rko", r"расч[её]тн\w* сч[её]т|(?<![а-яё])рко(?![а-яё])"),
    ("fees", r"комисси"),
    ("tariffs", r"тариф"),
    ("transfers", r"(?<![а-яё])перевод|(?<![а-яё])сбп(?![а-яё])|систем\w* быстрых платеж"),
    ("investments", r"инвестиц|брокерск|(?<![а-яё])иис(?![а-яё])"),
    ("mobile_app", r"мобильн\w* (?:приложени|банк)"),
    ("premium", r"премиальн|private banking"),
]
_TXT = [(t, re.compile(p, re.I)) for t, p in TEXT_RULES]
_TXT_SUPPRESS = {"credits": {"mortgage", "auto", "cards_credit"},
                 "transfers": {"transfers_intl"}}


def classify_text(title: str | None, text: str | None, head_chars: int = 3000) -> list[str]:
    """По заголовку хватает одного совпадения (до двух тем); по тексту нужна
    явная доминанта: ≥3 упоминаний в начале и вдвое больше следующей темы."""
    t = (title or "").lower()
    by_title = [tp for tp, rx in _TXT if rx.search(t)]
    by_title = [x for x in by_title if not (_TXT_SUPPRESS.get(x, set()) & set(by_title))]
    if by_title:
        return by_title[:2]
    body = (text or "")[:head_chars].lower()
    cnt = sorted(((len(rx.findall(body)), tp) for tp, rx in _TXT), reverse=True)
    if cnt and cnt[0][0] >= 3 and cnt[0][0] >= 2 * max(1, cnt[1][0] if len(cnt) > 1 else 0):
        return [cnt[0][1]]
    return []


def classify_document(url: str, title: str | None, text: str | None) -> tuple[list[str], str]:
    """(темы, источник темы: 'url' | 'text')."""
    tags = classify_url(url)
    if PRODUCT_TOPICS & set(tags):
        return tags, "url"
    extra = [x for x in classify_text(title, text) if x not in tags]
    return (tags + extra, "text") if extra else (tags, "url")
