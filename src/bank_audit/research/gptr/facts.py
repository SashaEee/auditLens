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

log = logging.getLogger(__name__)

# Сколько текста страницы отдаём извлекателю. Больше — дороже и хуже фокус;
# меньше — теряем таблицы тарифов в хвосте страницы.
_PAGE_BUDGET = 14000
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
    stance: str             # declared | observed
    confidence: float = 1.0

    def to_ui(self) -> dict:
        return {"id": self.id, "subject": self.subject,
                "attribute": self.attribute, "value": self.value,
                "unit": self.unit, "verbatim": self.verbatim,
                "url": self.url, "stance": self.stance}


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

def stance_for(url: str, subject: str, subject_domains: dict[str, str]) -> str:
    """declared — если это сайт самой организации; иначе observed.

    Признак структурный: чей домен, тот и «заявляет». Никаких слов про отзывы
    и жалобы — сторонний разбор условий и жалоба клиента одинаково являются
    взглядом со стороны, и для аудита это одна категория.
    """
    host = urlparse(url).netloc.lower().removeprefix("www.")
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
            take = declared[:per_cell]
            if observed:
                take = declared[:max(1, per_cell - 1)] + observed[:max(1, per_cell - 1)]
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
            "«наблюдается» — взгляд со стороны (жалобы, отзывы, разборы). "
            "Где есть обе стороны, показывай обе и называй расхождение.",
            "",
        ]
        for f in self.select_for_writer(per_cell):
            side = "заявлено" if f.stance == "declared" else "наблюдается"
            subj = labels.get(f.subject, f.subject) or "—"
            unit = f" {f.unit}" if f.unit else ""
            lines.append(
                f"[f:{f.id}] {subj} | {f.attribute} | {f.value}{unit} "
                f"| сторона: {side} | источник: {f.url}\n"
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

Пиши коротко, по-русски, существительными. Ответ строго JSON:
{"attributes": ["...", "..."], "observed": "..."}"""


async def plan_attributes(client, model: str, question: str, plan) -> list[str]:
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
        log.warning("характеристики плана: %s — берём предмет как единственную",
                    type(e).__name__)
        return [product or "условия"]
    attrs = [str(a).strip()[:120] for a in (data.get("attributes") or []) if a]
    attrs = attrs[:8]
    # Наблюдаемая сторона добавляется ВСЕГДА: без неё отчёт пересказывает
    # обещания организаций, а расхождение с практикой остаётся непроверенным.
    # Формулировку даёт модель под конкретный вопрос — списка тем в коде нет.
    observed = str(data.get("observed") or "").strip()[:120]
    if observed and observed not in attrs:
        attrs.append(observed)
    return attrs or [product or "условия"]


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
            + f"\n\n# Текст страницы\n{text[:_PAGE_BUDGET]}")
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


def select_pages(pages: dict[str, str], attributes: list[str],
                 subject_domains: dict[str, str], limit: int) -> dict[str, str]:
    """Отбор страниц под извлечение по близости к КОНТРАКТУ.

    Читаем мы широко (80+ страниц), а извлекать из всего дорого: замер 31.08 —
    196 секунд, 58% времени прогона, при том что писателю уходит около сотни
    фактов из семисот. Отбираем страницы, семантически близкие к
    характеристикам, которые обязаны закрыть.

    Сайты самих объектов сохраняем всегда, независимо от близости: без
    первоисточника отчёт теряет заявленную сторону, а это дороже любой
    экономии. Порог не по словам — по эмбеддингам, поэтому работает для
    любого вопроса.
    """
    if len(pages) <= limit:
        return pages
    own = [d for d in subject_domains.values() if d]
    must, rest = {}, {}
    for url, text in pages.items():
        host = urlparse(url).netloc.lower().removeprefix("www.")
        (must if any(host == d or host.endswith("." + d) for d in own)
         else rest)[url] = text
    room = max(0, limit - len(must))
    if room <= 0 or not rest:
        return must or pages
    try:
        from ...rag.embedder import embed_batch
        probe = "; ".join(attributes)[:2000]
        urls = list(rest)
        vecs = embed_batch([probe] + [rest[u][:3000] for u in urls])
        q = vecs[0]
        qn = sum(x * x for x in q) ** 0.5 or 1.0

        def sim(v):
            n = sum(x * x for x in v) ** 0.5 or 1.0
            return sum(a * b for a, b in zip(q, v)) / (qn * n)

        ranked = sorted(zip(urls, vecs), key=lambda p: -sim(p[1]))
        chosen = [u for u, _ in ranked[:room]]
    except Exception as e:
        log.info("отбор страниц: эмбеддинги недоступны (%s) — берём длиннейшие",
                 type(e).__name__)
        chosen = sorted(rest, key=lambda u: -len(rest[u]))[:room]
    out = dict(must)
    out.update({u: rest[u] for u in chosen})
    log.info("извлечение: %d страниц из %d (%d первоисточников + %d по близости)",
             len(out), len(pages), len(must), len(chosen))
    return out


async def build_registry(client, model: str, *, pages: dict[str, str],
                         attributes: list[str], plan,
                         concurrency: int = 10,
                         page_limit: int | None = None) -> FactRegistry:
    """Страницы → реестр фактов."""
    subjects = list(getattr(plan, "subjects", None) or [])
    labels = dict(getattr(plan, "subject_labels", None) or {})
    domains = {s: _BANK_DOMAINS.get(s, "") for s in subjects}
    reg = FactRegistry()
    if not attributes:
        return reg
    limit = page_limit if page_limit is not None else int(
        os.getenv("GPTR_EXTRACT_PAGES", "35"))
    pages = select_pages(pages, attributes, domains, limit)

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
            subj = str(it.get("subject") or "")
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
