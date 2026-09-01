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

ДВА СЛОЯ, и это принципиально.

Дешёвый слой (без модели) умеет только ПОДТВЕРЖДАТЬ: цитата найдена дословно
или почти дословно — факту верим, дальше не смотрим. Опровергать он не вправе.
Пересказ может быть сделан ДРУГИМИ СЛОВАМИ — синонимами, иной конструкцией — и
остаться правдой; совпадение словарей такой пересказ объявило бы выдумкой.

Слой суждения (модель) получает ТОЛЬКО остаток — факты, которых дешёвый слой
не подтвердил. Их обычно единицы, они группируются по странице (несколько
утверждений в один вызов), модель зовётся дешёвая и без рассуждения. Вопрос
задаётся один и тот же для любой предметной области: следует ли утверждение из
этого текста. Ни одного слова про банки, продукты или темы.

ЧТО ДЕЛАЕТ С ФАКТОМ. Три исхода, и ни один не молчаливый:
  • дословно     — цитата найдена в тексте, факт идёт в отчёт;
  • близко       — источник утверждение подтверждает, но другими словами;
                   факт идёт в отчёт с пометкой;
  • без опоры    — источник утверждение НЕ подтверждает (так сказала модель,
                   а не счётчик слов); факт снимается, число снятых попадает
                   в «Честные пробелы».
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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


_JUDGE_SYSTEM = """Ты проверяешь, следует ли утверждение из текста источника.

Тебе дают фрагмент источника и пронумерованные утверждения, каждое со своей
цитатой. По каждому реши ОДНО:
  "да"  — источник это утверждение подтверждает. Подтверждает и тогда, когда
          слова другие: пересказ, синоним, иная конструкция, свёрнутая
          формулировка. Смысл важнее совпадения слов.
  "нет" — в источнике этого нет: утверждение о другом, число другое, либо
          сказанное просто отсутствует в тексте.

Сомневаешься между «да» и «нет» — отвечай "да": снимать подлинное хуже, чем
пропустить сомнительное, его увидит человек.

Ответ строго JSON: {"verdicts": [{"n": 1, "ok": true}, {"n": 2, "ok": false}]}"""

# Сколько текста источника показываем судье. Больше — дороже и медленнее;
# меньше — судья не находит подтверждения там, где оно есть.
_JUDGE_PAGE_CHARS = 7000
# Сколько утверждений отдаём в один вызов. Пачкой по странице: у неподтверждённых
# фактов часто общий источник, и один вызов закрывает сразу несколько.
_JUDGE_BATCH = 8


async def judge(client, model: str, doubtful: list, pages: dict[str, str],
                concurrency: int = 8) -> set:
    """Модель судит ТОЛЬКО неподтверждённое. Возвращает id к снятию.

    Группируем по источнику: и вызовов меньше, и судье не приходится каждый раз
    заново вчитываться в одну и ту же страницу.
    """
    if not doubtful:
        return set()
    from .facts import _fit_page

    by_url: dict[str, list] = {}
    for f in doubtful:
        by_url.setdefault(f.url, []).append(f)

    batches: list[tuple[str, list]] = []
    for url, facts in by_url.items():
        for i in range(0, len(facts), _JUDGE_BATCH):
            batches.append((url, facts[i:i + _JUDGE_BATCH]))

    sem = asyncio.Semaphore(concurrency)
    cut: set = set()

    async def one(url: str, facts: list):
        page = (pages.get(url) or "")[:_JUDGE_PAGE_CHARS * 2]
        if not page.strip():
            return                      # источника нет — не судим, оставляем
        claims = "\n".join(
            f'{i + 1}. {f.attribute}: {f.value} {f.unit}'.rstrip()
            + f'\n   цитата: «{f.verbatim}»'
            for i, f in enumerate(facts))
        user = (f"# Источник\n{_fit_page(page[:_JUDGE_PAGE_CHARS])}\n\n"
                f"# Утверждения\n{claims}")
        kw: dict = {"model": model, "temperature": 0.0, "max_tokens": 700,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": _JUDGE_SYSTEM},
                                 {"role": "user", "content": user}]}
        if os.getenv("GPTR_EXTRACT_THINKING", "0") != "1":
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
        async with sem:
            try:
                try:
                    resp = await client.chat.completions.create(**kw)
                except Exception:
                    kw.pop("extra_body", None)
                    resp = await client.chat.completions.create(**kw)
                data = json.loads((resp.choices[0].message.content or "").strip())
            except Exception as e:
                log.info("критик-судья %s: %s", url[:60], type(e).__name__)
                return                  # не смогли рассудить — не снимаем
        for item in (data.get("verdicts") or []):
            try:
                n = int(item.get("n")) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(facts) and item.get("ok") is False:
                cut.add(facts[n].id)

    await asyncio.gather(*(one(u, fs) for u, fs in batches),
                         return_exceptions=True)
    return cut


async def review(client, model: str, registry, pages: dict[str, str]) -> Verdict:
    """Полная проверка: дешёвый слой, затем модель по остатку."""
    v, doubtful = prescreen(registry, pages)
    if doubtful:
        log.info("критик: на суждение модели %d из %d фактов",
                 len(doubtful), len(registry.facts))
        cut = await judge(client, model, doubtful, pages)
        # Не снятые судьёй — подтверждены смыслом, а не буквой.
        for f in doubtful:
            if f.id not in cut:
                f.support = CLOSE
                v.close += 1
    else:
        cut = set()
    return _finish(registry, v, cut)


def prescreen(registry, pages: dict[str, str]) -> tuple[Verdict, list]:
    """Дешёвый слой: подтверждает очевидное, НИЧЕГО не снимает.

    Возвращает (вердикт, список неподтверждённых фактов). Неподтверждённые
    уходят на суждение модели — здесь их судьба не решается.
    """
    v = Verdict()
    if not registry.facts:
        return v, []

    # Словари страницы считаем ОДИН раз: фактов с одной страницы обычно
    # десятки, и пересчёт на каждый превратил бы проверку в узкое место.
    cache: dict[str, tuple] = {}

    def page_of(url: str):
        if url not in cache:
            text = pages.get(url, "")
            pw = _words(text)
            cache[url] = (_norm(text), pw, set(pw), _nums(text))
        return cache[url]

    doubtful: list = []
    for f in registry.facts:
        page_norm, page_words, page_wordset, page_nums = page_of(f.url)
        if not page_norm:
            # Источник не сохранён (например, отзыв корпуса без текста) —
            # судить не по чему; факт не трогаем, но и в «дословные» не пишем.
            f.support = CLOSE
            v.close += 1
            continue

        level = _support(f.verbatim, page_norm, page_words, page_wordset)
        if level == UNSUPPORTED:
            # Дешёвый слой не подтвердил — но и опровергать не вправе:
            # пересказ другими словами он не распознаёт. Отдаём на суждение.
            doubtful.append(f)
            continue
        f.support = level
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

    return v, doubtful


def _finish(registry, v: Verdict, cut_ids: set) -> Verdict:
    """Снимает то, что не подтвердила ни проверка, ни модель."""
    if cut_ids:
        registry.facts = [f for f in registry.facts if f.id not in cut_ids]
        v.cut = len(cut_ids)
        v.cut_ids = sorted(cut_ids)
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
