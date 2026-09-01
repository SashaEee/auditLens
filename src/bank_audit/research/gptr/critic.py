"""Критик: решает, какому факту можно верить. Без модели и без словарей.

ЗАЧЕМ. Слой извлечения намеренно ничего не отбраковывает — он только собирает.
Судить обязан отдельный модуль, и это он. Пока его не было, выдуманное
утверждение проходило в отчёт наравне с проверенным: получало якорь, ссылку на
реальную страницу и выглядело неотличимо. Отсюда отзыв территориального банка:
«все нормативные акты, постановления, законы придуманы».

ДВА ТРЕБОВАНИЯ, оба выполнены конструкцией:

1. НИКАКОГО ХАРДКОДА. Здесь нет ни одного слова предметной области, ни списка
   тем, ни перечня «подозрительных» формулировок. Все проверки — про ФОРМУ
   отношения факта к источнику: нашлась ли цитата в тексте, откуда взято число,
   чей это домен. Такая проверка одинаково работает для вопроса про ставку,
   про порядок оформления карты и про предельные значения ПСК.

2. НЕ ЗАМЕДЛЯТЬ ПРОГОН. Ни одного обращения к модели. Дословность — это поиск
   подстроки, совпадение слов — пересечение множеств, домен — разбор URL.
   Словари токенов по страницам считаются ОДИН раз и переиспользуются всеми
   фактами этой страницы.

ЧТО ДЕЛАЕТ С ФАКТОМ. Три исхода, и ни один не молчаливый:
  • дословно     — цитата найдена в тексте символ в символ, факт идёт в отчёт;
  • близко       — цитата пересказана, но её слова стоят рядом в источнике;
                   факт идёт в отчёт с пометкой (модель вправе переставить
                   слова, и это не выдумка);
  • без опоры    — слов цитаты в источнике нет; факт СНИМАЕТСЯ, и число снятых
                   попадает в «Честные пробелы», а не теряется тихо.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..v2.tools.web_tools import REGULATOR_DOMAINS
from .facts import _norm

log = logging.getLogger(__name__)

EXACT = "дословно"
CLOSE = "близко к тексту"
UNSUPPORTED = "без опоры"

# Доля слов цитаты, которую нужно встретить рядом в источнике, чтобы счесть
# её пересказом, а не выдумкой. Ниже — уже другой текст.
_CLOSE_RATIO = 0.85
# Цитата короче этого не проверяема: «0 ₽» найдётся на любой странице и
# подтвердит что угодно.
_MIN_QUOTE = 12
# Во сколько раз окно поиска шире самой цитаты: пересказ обычно длиннее
# оригинала за счёт вставок.
_WINDOW = 3


@dataclass
class Verdict:
    """Итог проверки прогона. Всё считается, ничего не теряется молча."""
    exact: int = 0
    close: int = 0
    cut: int = 0
    mislabeled: int = 0          # сторона доказательства не совпала с доменом
    invented_numbers: int = 0    # число в значении, которого нет в источнике
    cut_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return self.exact + self.close + self.cut

    @property
    def trust(self) -> float:
        """Доля фактов с дословной опорой — метрика доверия к отчёту."""
        return (self.exact / self.checked) if self.checked else 1.0

    def to_ui(self) -> dict:
        return {"checked": self.checked, "exact": self.exact,
                "close": self.close, "cut": self.cut,
                "mislabeled": self.mislabeled,
                "invented_numbers": self.invented_numbers,
                "trust": round(self.trust, 3)}


_WORD_RE = re.compile(r"\w{2,}")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _words(s: str) -> list[str]:
    return _WORD_RE.findall(_norm(s))


# «100 000 ₽» в тексте и «100000» в значении — одно число. Разряды в русском
# тексте разделяются пробелом (в том числе неразрывным), и без склейки любая
# сумма выглядела бы выдуманной: на замере это дало 410 ложных срабатываний
# из 501 факта.
_GROUP_RE = re.compile(r"(?<=\d)[\s\u00a0\u202f](?=\d{3}(?!\d))")


def _nums(s: str) -> set[str]:
    """Числа в нормализованном виде: 16,5 · 16.5 · 100 000 · 100000."""
    joined = _GROUP_RE.sub("", s or "")
    out = set()
    for n in _NUM_RE.findall(joined):
        n = n.replace(",", ".")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n)
    return out


def _support(quote: str, page_norm: str, page_words: list[str],
             page_wordset: set[str]) -> str:
    """Насколько цитата опирается на текст страницы."""
    q = _norm(quote)
    if len(q) < _MIN_QUOTE:
        return UNSUPPORTED
    if q in page_norm:
        return EXACT
    qw = _words(quote)
    if len(qw) < 3:
        return UNSUPPORTED
    need = set(qw)
    # Дешёвая отсечка: если и половины слов нет на странице вообще, окно искать
    # незачем. Это снимает подавляющее большинство выдумок за одну операцию.
    if len(need & page_wordset) / len(need) < _CLOSE_RATIO:
        return UNSUPPORTED
    win = max(len(qw) * _WINDOW, 12)
    for i in range(0, max(1, len(page_words) - len(qw) + 1)):
        if len(need & set(page_words[i:i + win])) / len(need) >= _CLOSE_RATIO:
            return CLOSE
    return UNSUPPORTED


def _is_regulator(url: str) -> bool:
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    return (any(host == d or host.endswith("." + d) for d in REGULATOR_DOMAINS)
            or host.endswith(".gov.ru"))


def review(registry, pages: dict[str, str]) -> Verdict:
    """Проверяет реестр по источникам и СНИМАЕТ факты без опоры.

    Меняет registry.facts на месте: снятые удаляются, у остальных
    проставляется `support`. Возвращает вердикт со счётчиками.
    """
    v = Verdict()
    if not registry.facts:
        return v

    # Словари страницы считаем ОДИН раз: фактов с одной страницы обычно
    # десятки, и пересчёт на каждый превратил бы проверку в узкое место.
    cache: dict[str, tuple] = {}

    def page_of(url: str):
        if url not in cache:
            text = pages.get(url, "")
            pw = _words(text)
            cache[url] = (_norm(text), pw, set(pw), _nums(text))
        return cache[url]

    kept = []
    for f in registry.facts:
        page_norm, page_words, page_wordset, page_nums = page_of(f.url)
        if not page_norm:
            # Источник не сохранён (например, отзыв корпуса без текста) —
            # судить не по чему; факт не трогаем, но и в «дословные» не пишем.
            f.support = CLOSE
            v.close += 1
            kept.append(f)
            continue

        level = _support(f.verbatim, page_norm, page_words, page_wordset)
        f.support = level
        if level == UNSUPPORTED:
            v.cut += 1
            v.cut_ids.append(f.id)
            continue
        v.exact += level == EXACT
        v.close += level == CLOSE

        # Число из значения обязано встречаться в источнике: цитата может быть
        # подлинной, а число рядом с ней — подставленным.
        vn = _nums(f.value)
        if vn and not (vn & page_nums):
            v.invented_numbers += 1
            f.support = CLOSE          # не снимаем, но и «дословным» не зовём

        # Норма регулятора обязана приходить от регулятора. Иначе это чей-то
        # пересказ нормы, и называть его требованием закона нельзя.
        if f.stance == "regulatory" and not _is_regulator(f.url):
            f.stance = "observed"
            v.mislabeled += 1
        kept.append(f)

    registry.facts = kept
    if v.cut:
        v.notes.append(
            f"Снято утверждений без опоры на источник: {v.cut} из {v.checked}.")
    if v.mislabeled:
        v.notes.append(
            f"Утверждений, названных нормой, но пришедших не от регулятора: "
            f"{v.mislabeled} — переведены во взгляд со стороны.")
    if v.invented_numbers:
        v.notes.append(
            f"Чисел, которых нет в источнике рядом с цитатой: "
            f"{v.invented_numbers} — помечены как непроверенные.")
    log.info("критик: %s", v.to_ui())
    return v
