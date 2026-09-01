"""Слой фактов: между чтением страниц и написанием отчёта.

ЗАЧЕМ. Писатель gpt-researcher получает сжатый ком контекста и сочиняет прозу.
Всё, что нужно аудиту дальше — цитируемость, пробелы, сверка, список реально
использованных источников, — приходится угадывать регулярками по готовому
тексту. Отсюда и «цитирований 0» при 44 источниках, и приложение, не связанное
с текстом, и выброшенные отзывы, и пробелы, показывающие одну строку там, где
в отчёте десяток «не найдено».

Здесь между чтением и письмом появляется типизированный слой. Каждая
прочитанная страница превращается в факты, и у каждого факта есть ДОСЛОВНАЯ
цитата, которая проверяется машинно: подстрочный поиск в сохранённом тексте
страницы. Не нашлась — факт отбрасывается. Модель не может выдумать факт; она
может только показать пальцем на кусок текста.

ДВА ИЗМЕРЕНИЯ, а не темы:
  attribute — что именно за характеристика (берётся из плана, не из словаря);
  stance    — declared (сказано самой организацией на её сайте) против
              observed (видно со стороны: жалобы, отзывы, сторонние разборы).

`stance` определяется структурно — по тому, чей это сайт, — и потому лечит
пропажу отзывов без списка «тем про отзывы»: наблюдаемая сторона либо есть в
фактах, либо её отсутствие становится видимым пробелом.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..entity_extractor import _BANK_DOMAINS
from ..v2.tools.web_tools import REGULATOR_DOMAINS

log = logging.getLogger(__name__)

# Сколько текста страницы отдаём извлекателю. Больше — дороже и хуже фокус.
_PAGE_BUDGET = 14000
# Какую долю бюджета берём с КОНЦА страницы. Наш парсер приклеивает блок
# «# Таблицы страницы» в самый хвост, а обрезка с головы его срезала — то есть
# из тарифной страницы терялась ровно таблица тарифов.
_TAIL_SHARE = 0.3
# Ниже этой близости к контракту страница считается не по теме.
_MIN_TOPIC_SIM = 0.25

# Факт с цитатой короче этого не проверить осмысленно: «да», «0 ₽» найдутся
# в любом тексте и подтвердят что угодно.
_MIN_VERBATIM = 12


@dataclass
class Fact:
    """Одно проверяемое утверждение с дословной опорой в источнике."""
    id: int
    subject: str            # slug субъекта («sberbank») или "" для общих
    attribute: str          # характеристика из плана
    value: str              # значение как есть: число, срок, условие, шаг
    unit: str               # единица, если применимо («%», «₽», «дн.»)
    verbatim: str           # цитата из источника, как её привела модель
    url: str
    stance: str             # declared | observed | regulatory
    date: str = ""          # дата высказывания, если источник её знает
    confidence: float = 1.0

    def to_ui(self) -> dict:
        return {"id": self.id, "subject": self.subject,
                "attribute": self.attribute, "value": self.value,
                "unit": self.unit, "verbatim": self.verbatim,
                "url": self.url, "stance": self.stance, "date": self.date}


# ── Дословная проверка ────────────────────────────────────────────────────

_SOFT = dict.fromkeys(map(ord, "­​‌‍﻿"), None)


def _norm(s: str) -> str:
    """Нормализация для подстрочного поиска.

    Гасим ровно то, что различает один и тот же текст в HTML и в ответе
    модели: неразрывные пробелы, мягкие переносы, ё/е, разные тире и кавычки,
    ₽ против «руб». Смысл не меняем — иначе проверка перестанет быть проверкой.
    """
    s = (s or "").translate(_SOFT)
    s = s.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = re.sub(r"[—–−-]", "-", s)
    s = re.sub(r"[«»„“”\"']", "", s)
    s = re.sub(r"\bруб(?:лей|ля|\.)?", "₽", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def verbatim_found(quote: str, page_text: str) -> bool:
    """Цитата дословно присутствует в тексте страницы.

    Здесь НИЧЕГО не решается: слой извлечения только собирает факты и не
    выносит суждений о их годности. Функция оставлена как инструмент для
    критика (research/gptr/critic.py) — отдельного модуля, который и решает,
    что идёт в отчёт, а что помечается или снимается.
    """
    q = _norm(quote)
    if len(q) < _MIN_VERBATIM:
        return False
    return q in _norm(page_text)


# ── Сторона доказательства ────────────────────────────────────────────────

# Площадки отзывов: наблюдаемая сторона по определению. Держим отдельно от
# прочих агрегаторов, чтобы отличать жалобу клиента от обзорной статьи.
REVIEW_DOMAINS = ("banki.ru", "sravni.ru", "finuslugi.ru", "otzovik.com",
                  "irecommend.ru", "vbr.ru")


def stance_for(url: str, subject: str, subject_domains: dict[str, str]) -> str:
    """Чей это голос: организации, регулятора или взгляд со стороны.

    Признак структурный — по владельцу домена. Норма регулятора это не мнение
    и не обещание банка, а третье измерение аудита: чем предмет обязан
    соответствовать. Без него отчёт сравнивает игроков между собой, но не с
    требованием.
    """
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if any(host == d or host.endswith("." + d) for d in REGULATOR_DOMAINS):
        return "regulatory"
    if host.endswith(".gov.ru"):
        return "regulatory"
    own = subject_domains.get(subject)
    if own and (host == own or host.endswith("." + own)):
        return "declared"
    # Субъект мог быть не определён — тогда declared, если домен принадлежит
    # ЛЮБОМУ из объектов исследования.
    if not subject and any(host == d or host.endswith("." + d)
                           for d in subject_domains.values() if d):
        return "declared"
    return "observed"


# ── Реестр ────────────────────────────────────────────────────────────────

@dataclass
class FactRegistry:
    """Все проверенные факты прогона. Идентификатор факта — якорь цитаты."""
    facts: list[Fact] = field(default_factory=list)
    _next_id: int = 1

    def add(self, **kw) -> Fact:
        f = Fact(id=self._next_id, **kw)
        self._next_id += 1
        self.facts.append(f)
        return f

    def urls(self) -> list[str]:
        """Источники, реально давшие хотя бы один факт."""
        seen, out = set(), []
        for f in self.facts:
            if f.url not in seen:
                seen.add(f.url); out.append(f.url)
        return out

    def by_cell(self) -> dict[tuple[str, str], list[Fact]]:
        """Матрица «субъект × атрибут» — основа расчёта пробелов."""
        cells: dict[tuple[str, str], list[Fact]] = {}
        for f in self.facts:
            cells.setdefault((f.subject, f.attribute), []).append(f)
        return cells

    def select_for_writer(self, per_cell: int = 3) -> list[Fact]:
        """Отбор фактов в контекст писателя.

        Сотни фактов в контексте топят инструкции: на прогоне 31.08 писатель
        получил 1127 фактов (~170 КБ) и проигнорировал требование ставить
        якоря. Берём немного на каждую клетку матрицы и обязательно обе
        стороны, если они есть, — покрытие сохраняется, объём падает на
        порядок.
        """
        out: list[Fact] = []
        for facts in self.by_cell().values():
            declared = [f for f in facts if f.stance == "declared"]
            observed = [f for f in facts if f.stance == "observed"]
            regulatory = [f for f in facts if f.stance == "regulatory"]
            take = list(regulatory[:per_cell])       # норму не теряем никогда
            room = max(1, per_cell - len(take))
            if observed:
                take += declared[:max(1, room - 1)] + observed[:max(1, room - 1)]
            else:
                take += declared[:room]
            out.extend(take)
        return sorted(out, key=lambda f: f.id)

    def render_for_writer(self, labels: dict[str, str],
                          per_cell: int = 3) -> str:
        """Контекст писателя: только факты, каждый со своим якорем."""
        if not self.facts:
            return ""
        lines = [
            "НИЖЕ — ПРОВЕРЕННЫЕ ФАКТЫ. Каждый подтверждён дословной цитатой из",
            "источника. Пиши отчёт ТОЛЬКО по ним и после каждого утверждения",
            "ставь якорь вида [f:12] с идентификатором факта. Факта нет — так и",
            "напиши; не додумывай и не обобщай сверх фактов.",
            "",
            "Поле «сторона»: «заявлено» — со слов самой организации, "
            "«наблюдается» — взгляд со стороны (жалобы, отзывы, разборы), "
            "«норма регулятора» — требование закона или ЦБ. Показывай все, "
            "какие есть, и называй расхождения: заявленное против практики и "
            "заявленное против нормы.",
            "",
        ]
        for f in self.select_for_writer(per_cell):
            side = {"declared": "заявлено", "regulatory": "норма регулятора"
                    }.get(f.stance, "наблюдается")
            subj = labels.get(f.subject, f.subject) or "—"
            unit = f" {f.unit}" if f.unit else ""
            when = f" | дата: {f.date}" if f.date else ""
            lines.append(
                f"[f:{f.id}] {subj} | {f.attribute} | {f.value}{unit} "
                f"| сторона: {side}{when} | источник: {f.url}\n"
                f"      цитата: «{f.verbatim}»")
        return "\n".join(lines)


# ── Извлечение ────────────────────────────────────────────────────────────

_ATTRS_SYSTEM = """Ты готовишь список характеристик для сравнительного разбора.

Выдели 5-9 характеристик, по которым нужно собрать данные, чтобы ответить на
вопрос. Характеристика — это то, что сопоставимо между объектами и что можно
подтвердить цитатой из источника: условие, срок, требование, шаг процесса,
числовой параметр.

Отдельно назови ОДНУ характеристику наблюдаемой стороны: то, что видно про
предмет вопроса со стороны, а не со слов самой организации (с чем сталкиваются
на практике, какие расхождения с заявленным, на что жалуются). Она обязательна:
аудит сопоставляет заявленное с наблюдаемым.

Ещё отдельно — ОДНА характеристика нормативной рамки: какие требования
регулятора или закона действуют на предмет вопроса (что обязаны раскрывать,
какие пределы и сроки установлены, какая ответственность). Если предмет
вопроса ничем не регулируется — верни для неё пустую строку, не выдумывай.

Пиши коротко, по-русски, существительными. Ответ строго JSON:
{"attributes": ["...", "..."], "observed": "...", "regulatory": "..."}"""


@dataclass
class Contract:
    """Что отчёт обязан закрыть. Три измерения аудита в одной структуре."""
    attributes: list[str]          # все характеристики, включая две ниже
    observed: str = ""             # взгляд со стороны (жалобы, разборы)
    regulatory: str = ""           # нормативная рамка, если предмет регулируется
    degraded: str = ""             # почему контракт аварийный (пусто — всё в порядке)

    def __iter__(self):            # чтобы вести себя как список характеристик
        return iter(self.attributes)

    def __len__(self):
        return len(self.attributes)

    def __getitem__(self, i):
        return self.attributes[i]


async def plan_attributes(client, model: str, question: str, plan) -> Contract:
    """План + вопрос → матрица характеристик, которую обязан закрыть отчёт.

    Это контракт: по нему считается покрытие и пробелы. Список приходит от
    модели под конкретный вопрос, а не из зашитого перечня тем, — иначе
    инструмент снова окажется заточен под те вопросы, которые я предугадал.
    """
    intent = (getattr(plan, "intent_summary", "") or "").strip()
    product = (getattr(plan, "product", "") or "").strip()
    sections = ", ".join(getattr(plan, "output_sections", None) or [])
    user = (f"# Вопрос аудитора\n{question}\n\n"
            f"# Что он хочет узнать\n{intent or '—'}\n\n"
            f"# Предмет\n{product or '—'}\n\n"
            f"# Разделы отчёта по плану\n{sections or '—'}")
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _ATTRS_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0, max_tokens=800,
            response_format={"type": "json_object"})
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
    except Exception as e:
        # Молча продолжать нельзя: без контракта не будет ни наблюдаемой
        # стороны, ни нормативной рамки, ни осмысленных пробелов — отчёт
        # выйдет пустым, а аудитор не узнает почему.
        log.warning("характеристики плана: %s — контракт аварийный",
                    type(e).__name__)
        return Contract([product or "условия"], degraded=type(e).__name__)
    attrs = [str(a).strip()[:120] for a in (data.get("attributes") or []) if a]
    attrs = attrs[:8]
    # Наблюдаемая сторона добавляется ВСЕГДА: без неё отчёт пересказывает
    # обещания организаций, а расхождение с практикой остаётся непроверенным.
    # Формулировку даёт модель под конкретный вопрос — списка тем в коде нет.
    observed = str(data.get("observed") or "").strip()[:120]
    if observed and observed not in attrs:
        attrs.append(observed)
    # Нормативная рамка — третье измерение аудита рядом с заявленным и
    # наблюдаемым. Пустую строку модель возвращает, когда предмет ничем не
    # регулируется («дизайн карты»), и тогда мы её не навязываем.
    regulatory = str(data.get("regulatory") or "").strip()[:120]
    if regulatory and regulatory not in attrs:
        attrs.append(regulatory)
    return Contract(attrs or [product or "условия"],
                    observed=observed, regulatory=regulatory)


_EXTRACT_SYSTEM = """Ты извлекаешь ПРОВЕРЯЕМЫЕ факты со страницы для аудита.

Правила, нарушение любого делает результат негодным:
1. Каждый факт обязан опираться на ДОСЛОВНЫЙ фрагмент страницы. Поле "verbatim" —
   это ТОЧНАЯ копия куска текста со страницы, символ в символ, без правок,
   пересказа, сокращения и склейки разных мест. От 12 символов.
2. Не вычисляй, не округляй, не переводи единицы. Что написано, то и пиши.
3. Если характеристики на странице нет — не выдумывай её, просто не включай.
4. "value" — короткое значение (число, срок, условие, шаг процесса).
   "unit" — единица, если применимо ("%", "₽", "дн.", "мес."), иначе "".
5. Факты о ЛЮБОЙ из перечисленных характеристик, даже если страница
   рассказывает о них вскользь.

Ответ — строго JSON: {"facts":[{"subject","attribute","value","unit","verbatim"}]}
"subject" — один из данных слугов объектов или "" если факт общий."""


def _fit_page(text: str) -> str:
    """Урезает страницу с ДВУХ концов: начало и хвост с таблицами."""
    if len(text) <= _PAGE_BUDGET:
        return text
    tail = int(_PAGE_BUDGET * _TAIL_SHARE)
    head = _PAGE_BUDGET - tail
    return text[:head] + "\n\n[…пропущено…]\n\n" + text[-tail:]


async def extract_page(client, model: str, *, url: str, text: str,
                       attributes: list[str], subjects: list[str],
                       labels: dict[str, str]) -> list[dict]:
    """Одна страница → список сырых фактов (ещё без проверки цитат)."""
    if not (text or "").strip():
        return []
    subj_hint = ", ".join(f"{s} ({labels.get(s, s)})" for s in subjects) or "—"
    user = (f"# Страница\n{url}\n\n"
            f"# Объекты исследования (слуги)\n{subj_hint}\n\n"
            f"# Характеристики, которые собираем\n"
            + "\n".join(f"- {a}" for a in attributes)
            + f"\n\n# Текст страницы\n{_fit_page(text)}")
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _EXTRACT_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0, max_tokens=4000,
            response_format={"type": "json_object"})
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.info("извлечение %s: %s", url[:70], type(e).__name__)
        return []
    try:
        data = json.loads(raw)
    except Exception:
        from ...ai.llm_utils import _loose_json_loads
        data = _loose_json_loads(raw) or {}
    items = (data or {}).get("facts") or []
    return [i for i in items if isinstance(i, dict)]


def _rank_pages(pages: dict[str, str],
                attributes: list[str]) -> list[tuple[float, str]]:
    """Все страницы разом, по близости к контракту. Одна прогонка эмбеддингов.

    Раньше близость считалась ТРИЖДЫ за прогон — отдельно для сайтов
    организаций, регуляторов и взгляда со стороны, — и каждый вызов
    блокировал event loop сетевым запросом. Плюс порог по теме применялся
    только к первым двум группам, и мусор («Собеседование на продакт-менеджера
    в Сбере») заходил именно через третью.

    Фолбэк без эмбеддингов — по доле слов контракта, а НЕ по длине страницы:
    сортировка по длине выбирала SEO-подборки, то есть ровно то, от чего
    отбор и защищает.
    """
    urls = list(pages)
    if not urls:
        return []
    probe = "; ".join(attributes)[:2000]
    try:
        from ...rag.embedder import embed_batch
        vecs = embed_batch([probe] + [pages[u][:3000] for u in urls])
        q = vecs[0]
        qn = sum(x * x for x in q) ** 0.5 or 1.0

        def sim(v):
            n = sum(x * x for x in v) ** 0.5 or 1.0
            return sum(a * b for a, b in zip(q, v)) / (qn * n)

        return sorted(((sim(v), u) for u, v in zip(urls, vecs[1:])),
                      key=lambda p: -p[0])
    except Exception as e:
        log.info("отбор страниц: эмбеддинги недоступны (%s) — по словам "
                 "контракта", type(e).__name__)
        want = {w for w in re.findall(r"\w{4,}", _norm(probe))}
        out = []
        for u in urls:
            got = {w for w in re.findall(r"\w{4,}", _norm(pages[u][:4000]))}
            out.append((len(want & got) / (len(want) or 1), u))
        return sorted(out, key=lambda p: -p[0])


def _take(ranked: list[tuple[float, str]], pages: dict[str, str],
          allow: set[str], room: int) -> dict[str, str]:
    """Лучшие из группы с порогом по теме — порог един для всех групп."""
    out: dict[str, str] = {}
    for score, url in ranked:
        if len(out) >= room:
            break
        if url in allow and score >= _MIN_TOPIC_SIM:
            out[url] = pages[url]
    return out


def select_pages(pages: dict[str, str], attributes: list[str],
                 subject_domains: dict[str, str], limit: int) -> dict[str, str]:
    """Отбор страниц под извлечение по близости к КОНТРАКТУ.

    Читаем мы широко (80+ страниц), а извлекать из всего дорого: замер 31.08 —
    196 секунд, 58% времени прогона. Отбираем близкие к характеристикам,
    которые обязаны закрыть, и раздаём места трём сторонам доказательства:
    норма и взгляд со стороны иначе проигрывают продуктовым страницам
    организаций, которых всегда больше.
    """
    if not pages:
        return {}
    own = [d for d in subject_domains.values() if d]
    declared, observed, regulatory = set(), set(), set()
    for url in pages:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if any(host == d or host.endswith("." + d)
               for d in REGULATOR_DOMAINS) or host.endswith(".gov.ru"):
            regulatory.add(url)
        elif any(host == d or host.endswith("." + d) for d in own):
            declared.add(url)
        else:
            observed.add(url)

    ranked = _rank_pages(pages, attributes)
    reg_room = min(len(regulatory), max(1, limit // 5))
    obs_room = min(len(observed), max(1, limit // 3))
    dec_room = max(1, limit - reg_room - obs_room)

    out = _take(ranked, pages, regulatory, reg_room)
    out.update(_take(ranked, pages, declared, dec_room))
    out.update(_take(ranked, pages, observed, obs_room))
    # Недобранное одной группой отдаём остальным: пустые квоты не должны
    # уменьшать общий объём разбора.
    if len(out) < limit:
        out.update(_take([r for r in ranked if r[1] not in out], pages,
                         set(pages), limit - len(out)))
    log.info("извлечение: %d из %d (заявлено %d, норм %d, со стороны %d)",
             len(out), len(pages),
             sum(1 for u in out if u in declared),
             sum(1 for u in out if u in regulatory),
             sum(1 for u in out if u in observed))
    return out or dict(list(pages.items())[:limit])


async def build_registry(client, model: str, *, pages: dict[str, str],
                         attributes: list[str], plan,
                         concurrency: int = 10,
                         page_limit: int | None = None,
                         keep_pages: set | None = None,
                         subject_hints: dict | None = None) -> FactRegistry:
    """Страницы → реестр фактов."""
    subjects = list(getattr(plan, "subjects", None) or [])
    labels = dict(getattr(plan, "subject_labels", None) or {})
    domains = {s: _BANK_DOMAINS.get(s, "") for s in subjects}
    reg = FactRegistry()
    if not attributes:
        return reg
    limit = page_limit if page_limit is not None else int(
        os.getenv("GPTR_EXTRACT_PAGES", "35"))
    # Отзывы из корпуса проходят отбор вне очереди: они и есть наблюдаемая
    # сторона, ради которой всё затевалось, и по близости к формулировкам
    # контракта они заведомо проигрывают продуктовым страницам банков.
    keep = {u: pages[u] for u in (keep_pages or set()) if u in pages}
    other = {u: t for u, t in pages.items() if u not in keep}
    # Предел считается для ВЕБ-страниц; отзывы идут сверху, а не вместо них.
    # Иначе 18 жалоб из корпуса съедали половину бюджета, продуктовые страницы
    # банков не попадали в извлечение, и у Т-Банка оказывалось раскрыто 2
    # характеристики из 10 — не потому что банк не раскрывает, а потому что мы
    # его страницы не прочитали.
    # Отбор считает эмбеддинги — сетевой синхронный вызов. В event loop он
    # замораживает всё, включая отдачу SSE: интерфейс замирает.
    picked = await asyncio.to_thread(select_pages, other, list(attributes),
                                     domains, limit)
    pages = {**keep, **picked}

    sem = asyncio.Semaphore(concurrency)

    async def one(url: str, text: str):
        async with sem:
            return url, await extract_page(
                client, model, url=url, text=text, attributes=attributes,
                subjects=subjects, labels=labels)

    results = await asyncio.gather(*(one(u, t) for u, t in pages.items()),
                                   return_exceptions=True)
    for res in results:
        if isinstance(res, Exception) or not res:
            continue
        url, items = res
        for it in items:
            quote = str(it.get("verbatim") or "")
            # Там, где принадлежность объекту ИЗВЕСТНА (отзыв из корпуса),
            # берём её из источника, а не из догадки модели: жалоба на
            # брокерские комиссии одного банка уехала в раздел про другой.
            forced = (subject_hints or {}).get(url)
            subj = forced or str(it.get("subject") or "")
            if subj and subj not in subjects:
                subj = ""
            reg.add(subject=subj,
                    attribute=str(it.get("attribute") or "").strip()[:120],
                    value=str(it.get("value") or "").strip()[:300],
                    unit=str(it.get("unit") or "").strip()[:20],
                    verbatim=quote.strip()[:600],
                    url=url,
                    stance=stance_for(url, subj, domains))
    log.info("факты: %d со %d страниц (без отбраковки — это дело критика)",
             len(reg.facts), len(pages))
    return reg
