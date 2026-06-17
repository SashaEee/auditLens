"""Knowledge Bundle — единый контейнер артефактов между агентами.

Все агенты пишут сюда свои находки в едином формате. Analyst читает bundle,
Critic верифицирует против bundle. Никаких «каждый на своём языке».

Артефакты намеренно универсальны — НЕ под конкретный продукт (автоперевод vs
ипотека vs качество обслуживания). Любая тема укладывается в:
  • Fact        — конкретное утверждение со ссылкой
  • Complaint   — кластер жалоб с цитатами
  • Regulation  — норматив/закон
  • Insight     — аналитический вывод (терминологическая ловушка, реформа ЦБ...)
  • Source      — источник с n-маркером для цитирования
"""
from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any


def _fact_year(f) -> int | None:
    """Год факта из as_of (или verbatim) — для пометки устаревших данных в отчёте."""
    for txt in (getattr(f, "as_of", "") or "", getattr(f, "verbatim", "") or ""):
        m = re.search(r"\b(20[0-2]\d)\b", txt)
        if m:
            return int(m.group(1))
    return None


def _stale_age_years() -> int:
    """Старше скольких лет факт считается устаревшим (тюнится V2_STALE_YEARS)."""
    import os
    try:
        return int(os.getenv("V2_STALE_YEARS", "2"))
    except ValueError:
        return 2


# ════════════════════════════════════════════════════════════════════════
# SOURCE — единый индекс источников с n-маркерами
# ════════════════════════════════════════════════════════════════════════


@dataclass
class Source:
    """Источник для цитирования [N]. Глобально дедуплицируется по URL."""
    url: str
    title: str = ""
    domain: str = ""
    bank_slug: str | None = None
    # trust: офиц.сайт банка/PDF/НПА=0.95-1.0, агрегатор=0.6-0.8, отзыв=0.5-0.7
    trust: float = 0.6
    kind: str = "web"  # bank_official | aggregator | regulatory | review | news | web
    excerpt: str = ""  # короткая выдержка для контекста в промпте

    def trust_marker(self) -> str:
        if self.trust >= 0.9:
            return "●●●"
        if self.trust >= 0.65:
            return "●●○"
        return "○○○"


class SourceRegistry:
    """Реестр источников с автонумерацией [N]. Дедуп по URL."""

    def __init__(self) -> None:
        self._by_url: dict[str, int] = {}
        self._items: list[Source] = []

    def add(self, src: Source) -> int:
        """Добавляет источник (или возвращает существующий n). Возвращает n."""
        if not src.url:
            return 0
        # Нормализуем URL для дедупа (без query/fragment для стабильности)
        key = src.url.split("#")[0].split("?")[0].rstrip("/")
        if key in self._by_url:
            # Если новый вариант богаче (есть excerpt/title) — обновляем метаданные
            existing = self._items[self._by_url[key] - 1]
            if src.excerpt and not existing.excerpt:
                existing.excerpt = src.excerpt
            if src.title and (not existing.title or len(existing.title) < len(src.title)):
                existing.title = src.title
            existing.trust = max(existing.trust, src.trust)
            return self._by_url[key]
        n = len(self._items) + 1
        self._by_url[key] = n
        self._items.append(src)
        return n

    def n_for(self, url: str) -> int:
        return self._by_url.get(url.split("#")[0].split("?")[0].rstrip("/"), 0)

    def all(self) -> list[Source]:
        return list(self._items)

    def to_ui(self) -> list[dict]:
        out = []
        for i, s in enumerate(self._items, 1):
            out.append({
                "n": i,
                "url": s.url,
                "title": s.title or s.url[:80],
                "domain": s.domain,
                "bank_slug": s.bank_slug,
                "trust_score": s.trust,
                "source_kind": s.kind,
                "excerpt": s.excerpt[:600],
            })
        return out


# ════════════════════════════════════════════════════════════════════════
# FACT — конкретное утверждение со ссылкой
# ════════════════════════════════════════════════════════════════════════


@dataclass
class Fact:
    """Атомарное утверждение: субъект = параметр = значение (с условиями).

    Универсально для любого домена: тариф, характеристика, требование,
    метрика качества обслуживания, факт о компании и т.д.
    """
    subject: str            # банк или объект («Сбербанк», «СБП», «ЦБ РФ»)
    attribute: str          # что утверждается («комиссия внешнего перевода»)
    value: str              # значение («0,5%, макс 1500 ₽»)
    source_n: int           # номер источника [N]
    verbatim: str = ""      # дословная цитата из источника
    conditions: list[str] = field(default_factory=list)  # «при условии X»
    as_of: str = ""         # дата/период действия («с 1 ноября 2024»)
    confidence: float = 0.8  # 0..1 (зависит от доверия источника)
    tags: list[str] = field(default_factory=list)  # свободные теги для группировки


# ════════════════════════════════════════════════════════════════════════
# COMPLAINT — кластер жалоб/отзывов
# ════════════════════════════════════════════════════════════════════════


@dataclass
class Complaint:
    """Кластер однотипных жалоб по субъекту."""
    subject: str            # банк
    theme: str              # «несработавший автоплатёж», «скрытая комиссия»
    n_reviews: int = 0      # сколько отзывов в кластере
    sentiment: str = "neg"  # neg | neu | pos | mixed
    sample_quotes: list[str] = field(default_factory=list)  # дословные цитаты
    period: str = ""        # «2024–2026», «2016 (устаревшие)»
    source_ns: list[int] = field(default_factory=list)
    rating_avg: float | None = None
    is_stale: bool = False  # устаревшие (давние) жалобы


@dataclass
class SentimentProfile:
    """Сентимент-срез по субъекту."""
    subject: str
    total: int = 0
    pos: float = 0.0
    neu: float = 0.0
    neg: float = 0.0
    avg_rating: float | None = None
    source_ns: list[int] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════
# REGULATION — нормативный акт
# ════════════════════════════════════════════════════════════════════════


@dataclass
class Regulation:
    subject: str = ""        # к чему применяется («переводы физлиц»)
    cite: str = ""           # «ст. 855 ГК РФ», «Положение ЦБ 762-П», «реформа 01.05.2024»
    summary: str = ""        # что предписывает
    source_n: int = 0
    effective_from: str = ""


# ════════════════════════════════════════════════════════════════════════
# INSIGHT — аналитический вывод (то, что делает отчёт «глубоким»)
# ════════════════════════════════════════════════════════════════════════


@dataclass
class Insight:
    """Аналитическое наблюдение, меняющее рамку сравнения.

    Примеры: «терминологическая ловушка автоплатёж vs автоперевод»,
    «реформа ЦБ уравняла цены → рейтинг не по цене».
    Это и есть то, чего не хватало старому pipeline."""
    headline: str           # короткая формулировка
    explanation: str        # развёрнутое объяснение
    evidence_ns: list[int] = field(default_factory=list)
    impact: str = ""        # что значит для аудитора


# ════════════════════════════════════════════════════════════════════════
# RANKING — рейтинг субъектов (first-class артефакт)
# ════════════════════════════════════════════════════════════════════════


@dataclass
class RankEntry:
    subject: str
    rank: int               # 1 = лучший
    score: float            # 0..10
    rationale: str          # почему этот ранг (1-2 предложения со ссылками)
    evidence_ns: list[int] = field(default_factory=list)
    data_gap: bool = False  # «недостаточно данных»


@dataclass
class Ranking:
    criterion: str = ""     # «по совокупности цена+гибкость+надёжность»
    entries: list[RankEntry] = field(default_factory=list)

    def sorted_entries(self) -> list[RankEntry]:
        return sorted(self.entries, key=lambda e: e.rank)


# ════════════════════════════════════════════════════════════════════════
# KNOWLEDGE BUNDLE — сборка всего
# ════════════════════════════════════════════════════════════════════════


@dataclass
class CoverageNote:
    """Честный пробел: что НЕ нашли (важно для аудитора)."""
    what: str               # «свежие жалобы 2024–2026»
    subjects: list[str]     # затронутые субъекты
    reason: str             # «не попало в индекс», «мало данных»
    recommendation: str = ""  # что делать аудитору


@dataclass
class KnowledgeBundle:
    """Всё, что собрали агенты. Analyst пишет отчёт из bundle,
    Critic верифицирует отчёт против bundle."""
    question: str = ""
    intent: str = ""            # conductor-classified
    subjects: list[str] = field(default_factory=list)  # банки/объекты сравнения
    subject_labels: dict[str, str] = field(default_factory=dict)  # slug→human

    sources: SourceRegistry = field(default_factory=SourceRegistry)
    facts: list[Fact] = field(default_factory=list)
    complaints: list[Complaint] = field(default_factory=list)
    sentiments: list[SentimentProfile] = field(default_factory=list)
    regulations: list[Regulation] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    ranking: Ranking | None = None
    coverage_notes: list[CoverageNote] = field(default_factory=list)

    @property
    def _slugset(self) -> set[str]:
        """Множество нормализованных slug'ов для O(1) lookup в canonical_subject."""
        return {_norm(s) for s in self.subjects if s}

    # ── helpers ────────────────────────────────────────────────────────
    def facts_for(self, subject: str) -> list[Fact]:
        subject = self.canonical_subject(subject)
        return [f for f in self.facts if self.canonical_subject(f.subject) == subject]

    def complaints_for(self, subject: str) -> list[Complaint]:
        subject = self.canonical_subject(subject)
        return [c for c in self.complaints
                if self.canonical_subject(c.subject) == subject]

    def canonical_subject(self, raw: str) -> str:
        """Сводит «Сбербанк»/«Sberbank»/«Сбер»/«sberbank» → slug, по которому
        субъект хранится в bundle (self.subjects). Возвращает slug, если нашли;
        иначе — нормализованный текст (нижний регистр, trim). Это критично:
        кондуктор держит субъектов как slug'и, а LLM-агенты возвращают метки
        («Сбербанк»). Без канонизации факт физически в bundle есть, но не
        находится в facts_for(subject)/to_prompt_context → рассыпается grounding
        и у аналитика, и у ranking-агента."""
        if not raw:
            return ""
        text = str(raw).strip()
        low = _norm(text)
        # 1. Прямой матч по slug
        if low in self._slugset:
            return low
        # 2. Матч по человекочитаемой метке (subject_labels: slug→«Сбербанк»)
        for slug, label in self.subject_labels.items():
            if _norm(label) == low:
                return slug
        # 2.5. Известный словарь синонимов банков (общий detect_bank_slugs):
        # покрывает Cyrillic↔Latin синонимы без общей подстроки, главное —
        # «Тинькофф»↔«Т-Банк»↔tinkoff (substring-матч их НЕ ловит). Возвращаем
        # slug, только если он среди субъектов этого bundle.
        try:
            from ...ai.llm_utils import detect_bank_slugs
            for slug in detect_bank_slugs(text):
                if _norm(slug) in self._slugset:
                    return _norm(slug)
        except Exception:
            pass
        # 3. Нечёткий/частичный: метка или slug содержат строку (или наоборот).
        # Покрывает «Сбер»→sberbank, «Т-Банк»/«Тинькофф»→tinkoff и т.п.
        for slug, label in self.subject_labels.items():
            lab = _norm(label)
            if lab and (low in lab or lab in low):
                return slug
            sl = _norm(slug)
            if sl and (low in sl or sl in low):
                return slug
        # 4. Не опознали — возвращаем нормализованный текст как есть (факт всё
        # равно сохранится, просто не привяжется к субъекту из списка).
        return low

    def add_fact(self, fact: Fact) -> None:
        # Канонизируем subject в slug при записи — иначе факт с меткой
        # («Сбербанк») не найдётся в facts_for(slug)/to_prompt_context.
        fact = _clone_fact(fact, subject=self.canonical_subject(fact.subject))
        # дедуп по (subject, attribute, value) — оставляем с большим confidence
        for existing in self.facts:
            if (existing.subject == fact.subject and
                    existing.attribute == fact.attribute and
                    _norm(existing.value) == _norm(fact.value)):
                if fact.confidence > existing.confidence:
                    self.facts.remove(existing)
                    self.facts.append(fact)
                return
        self.facts.append(fact)

    def add_complaint(self, c: Complaint) -> None:
        # Канонизация subject (см. add_fact).
        c = _clone_complaint(c, subject=self.canonical_subject(c.subject))
        # объединяем по (subject, theme)
        for existing in self.complaints:
            if existing.subject == c.subject and _norm(existing.theme) == _norm(c.theme):
                existing.n_reviews += c.n_reviews
                existing.sample_quotes.extend(c.sample_quotes)
                existing.sample_quotes = existing.sample_quotes[:5]
                existing.source_ns = list(set(existing.source_ns + c.source_ns))
                return
        self.complaints.append(c)

    # ── сериализация для промпта Analyst ───────────────────────────────
    def to_prompt_context(self, max_chars: int = 24000, *,
                          rich: bool = False) -> str:
        """Собирает bundle в текстовый блок для промпта писателя.
        Группирует по субъектам — Analyst видит полную картину по каждому.

        rich=True (для аналитика): добавляет дословные цитаты под фактами и блок
        «ВЫДЕРЖКИ ИЗ ИСТОЧНИКОВ» — чтобы писатель РАССУЖДАЛ по тексту источника, а
        не пересказывал голые «атрибут: значение». Это ключевой рычаг глубины.
        Для критика/ranking/repair rich=False (у них свой контекст-бюджет)."""
        parts: list[str] = []
        parts.append(f"# ВОПРОС АУДИТОРА\n{self.question}")
        parts.append(f"# ИНТЕНТ\n{self.intent}")
        if self.subjects:
            labels = [self.subject_labels.get(s, s) for s in self.subjects]
            parts.append(f"# СУБЪЕКТЫ СРАВНЕНИЯ\n{', '.join(labels)}")

        if self.insights:
            parts.append("# КЛЮЧЕВЫЕ ИНСАЙТЫ (поменяй рамку сравнения)")
            for ins in self.insights:
                cite = "".join(f"[{n}]" for n in ins.evidence_ns)
                parts.append(f"• **{ins.headline}** — {ins.explanation} {cite}"
                             + (f" | Влияние: {ins.impact}" if ins.impact else ""))

        if self.regulations:
            parts.append("# РЕГУЛЯТОРНОЕ ПОЛЕ")
            for reg in self.regulations:
                cite = f"[{reg.source_n}]" if reg.source_n else ""
                parts.append(f"• {reg.cite}: {reg.summary} {cite}"
                             + (f" (действует с {reg.effective_from})" if reg.effective_from else ""))

        if self.ranking and self.ranking.entries:
            parts.append(f"# РЕЙТИНГ ({self.ranking.criterion})")
            for e in self.ranking.sorted_entries():
                label = self.subject_labels.get(e.subject, e.subject)
                cite = "".join(f"[{n}]" for n in e.evidence_ns)
                gap = " [недостаточно данных]" if e.data_gap else ""
                parts.append(f"{e.rank}. {label} ({e.score:g}/10){gap} — {e.rationale} {cite}")

        # По субъектам — факты + жалобы
        _cur_year = datetime.now().year
        _stale_before = _cur_year - _stale_age_years()
        _stale_facts: list[str] = []   # консолидируем для блока «Актуальность данных»
        for subj in self.subjects:
            label = self.subject_labels.get(subj, subj)
            fs = self.facts_for(subj)
            cs = self.complaints_for(subj)
            if not fs and not cs:
                parts.append(f"### {label}\n_Нет данных в открытых источниках._")
                continue
            lines = [f"### {label}"]
            if fs:
                lines.append("**Факты:**")
                for f in fs:
                    cite = f"[{f.source_n}]"
                    cond = f" (при: {', '.join(f.conditions)})" if f.conditions else ""
                    fy = _fact_year(f)
                    asof = f" [{f.as_of}]" if f.as_of else (f" [{fy}]" if fy else "")
                    # ⚠ НЕ ставим инлайн (шум в тексте) — копим в _stale_facts.
                    lines.append(f"  • {f.attribute}: {f.value}{cond}{asof} {cite}")
                    if fy and fy < _stale_before:
                        _stale_facts.append(
                            f"{label} — {f.attribute}: данные за {fy} [{f.source_n}]")
                    if rich and f.verbatim:
                        vb = re.sub(r"\s+", " ", f.verbatim).strip()
                        if vb:
                            lines.append(f"      └ цитата из источника: «{vb[:300]}»")
            if cs:
                lines.append("**Жалобы/отзывы:**")
                qn, qlen = (3, 280) if rich else (2, 180)
                for c in cs:
                    cite = "".join(f"[{n}]" for n in c.source_ns[:3])
                    stale = " (устаревшие)" if c.is_stale else ""
                    meta = []
                    if c.sentiment and c.sentiment != "neg":
                        meta.append(c.sentiment)
                    if c.period:
                        meta.append(c.period)
                    if c.rating_avg is not None:
                        meta.append(f"ср.рейтинг {c.rating_avg:g}")
                    meta_s = f" ({'; '.join(meta)})" if meta else ""
                    lines.append(f"  • {c.theme} — {c.n_reviews} отзыв{stale}{meta_s} {cite}")
                    for q in c.sample_quotes[:qn]:
                        lines.append(f'      «{q[:qlen]}»')
            parts.append("\n".join(lines))

        # Выдержки реально использованных источников — заземление нарратива.
        if rich:
            ex = self._used_source_excerpts()
            if ex:
                parts.append(
                    "# ВЫДЕРЖКИ ИЗ ИСТОЧНИКОВ (опирайся на ТЕКСТ, не только на «атрибут: значение»)\n"
                    + ex)

        # Честные пробелы — отдельным «хвостом», который НЕ срезается усечением:
        # это first-class сигнал для аудитора и теряться при truncation не должен.
        tail = ""
        if self.coverage_notes:
            cov = ["# ЧЕСТНЫЕ ПРОБЕЛЫ (что НЕ удалось найти)"]
            for n in self.coverage_notes:
                subs = ", ".join(n.subjects) if n.subjects else "—"
                cov.append(f"• {n.what} ({subs}): {n.reason}"
                           + (f" → {n.recommendation}" if n.recommendation else ""))
            tail = "\n\n" + "\n".join(cov)
        if _stale_facts:
            st = ["# АКТУАЛЬНОСТЬ ДАННЫХ (устаревшие факты — оформи ОДНИМ списком в "
                  "«Честных оговорках», НЕ помечай ⚠ в основном тексте и таблице):"]
            st.extend("• " + s for s in _stale_facts[:20])
            tail += "\n\n" + "\n".join(st)

        text = "\n\n".join(parts)
        budget = max(0, max_chars - len(tail))
        if len(text) > budget:
            text = text[:budget] + "\n\n[…контекст сокращён из-за объёма…]"
        return text + tail

    def _used_source_excerpts(self, max_sources: int = 24,
                              per_excerpt: int = 450,
                              max_chars: int = 14000) -> str:
        """Выдержки источников, РЕАЛЬНО использованных в фактах/жалобах/
        регуляторике/инсайтах/рейтинге — по убыванию доверия. Заземление для
        нарратива (аналог _cited_sources_block у критика, но по bundle: отчёта
        ещё нет, цитировать не из чего)."""
        used: set[int] = set()
        for f in self.facts:
            if f.source_n:
                used.add(f.source_n)
        for c in self.complaints:
            used.update(n for n in c.source_ns if n)
        for r in self.regulations:
            if r.source_n:
                used.add(r.source_n)
        for ins in self.insights:
            used.update(n for n in ins.evidence_ns if n)
        if self.ranking:
            for e in self.ranking.entries:
                used.update(n for n in e.evidence_ns if n)
        if not used:
            return ""
        try:
            ui = {s["n"]: s for s in self.sources.to_ui()}
        except Exception:
            return ""
        items = sorted((ui[n] for n in used if n in ui),
                       key=lambda s: (-(s.get("trust_score") or 0), s.get("n", 0)))
        lines: list[str] = []
        total = 0
        for s in items[:max_sources]:
            exc = (s.get("excerpt") or "").strip()
            if not exc:
                continue
            exc = re.sub(r"\s+", " ", exc)[:per_excerpt]
            line = (f"[{s['n']}] {s.get('domain','')} "
                    f"({s.get('source_kind','')}, доверие {s.get('trust_score',0):.2f}): {exc}")
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    def sources_block_for_critic(self) -> str:
        """Только источники с выдержками — для верификации чисел."""
        lines = []
        for s in self.sources.all():
            if s.excerpt:
                lines.append(f"[{self.sources.n_for(s.url)}] {s.domain}: {s.excerpt[:400]}")
        return "\n".join(lines)

    def to_comparison_table(self, max_rows: int = 18) -> str:
        """ДЕТЕРМИНИРОВАННАЯ сравнительная таблица субъектов × атрибутов из
        bundle.facts. Ноль LLM — числа не могут галлюцинировать. Аналитик
        вставляет её в отчёт как есть (или правит только формулировку шапки).

        Стратегия: берём «самые различающиеся» атрибуты (встречающиеся у ≥2
        субъектов), строки — субъекты, столбцы — атрибуты. Значение = value
        факта + [N]. Пусто = «нет данных»."""
        if not self.facts or not self.subjects:
            return ""
        # subject(slug) → {норм.атрибут: лучшая Fact} (по confidence)
        grid: dict[str, dict[str, Fact]] = {s: {} for s in self.subjects}
        for f in self.facts:
            subj = self.canonical_subject(f.subject)
            if subj not in grid:
                grid[subj] = {}
            attr = _norm(f.attribute)
            cur = grid[subj].get(attr)
            if cur is None or f.confidence > cur.confidence:
                grid[subj][attr] = f
        # Атрибуты: только встречающиеся у ≥2 субъектов (несут различие),
        # отсортированы по распространённости (покрытие) затем по имени.
        attr_subjects: dict[str, int] = {}
        for subj, attrs in grid.items():
            for attr in attrs:
                attr_subjects[attr] = attr_subjects.get(attr, 0) + 1
        cmp_attrs = [a for a, cnt in attr_subjects.items() if cnt >= 2]
        cmp_attrs.sort(key=lambda a: (-attr_subjects[a], a))
        if not cmp_attrs:
            # Нет общих атрибутов ≥2 субъектов — покажем топ-N самых частых.
            cmp_attrs = [a for a, _ in sorted(attr_subjects.items(),
                                                key=lambda kv: (-kv[1], kv[0]))]
        cmp_attrs = cmp_attrs[:max_rows]
        if not cmp_attrs:
            return ""

        def _cell(subj: str, attr: str) -> str:
            f = grid.get(subj, {}).get(attr)
            if not f:
                return "—"
            val = f.value.strip()
            cite = f"[{f.source_n}]"
            cond = f" ({'; '.join(f.conditions)})" if f.conditions else ""
            # Несвежесть в таблицу НЕ выводим (визуальный шум, рушит доверие) —
            # устаревшие факты собираются отдельным блоком «Актуальность данных».
            return f"{val}{cond} {cite}".replace("|", "/").replace("\n", " ")

        # Шапка
        header = ["Параметр"] + [self.subject_labels.get(s, s) for s in self.subjects]
        rows = ["| " + " | ".join(header) + " |",
                "|" + "|".join(["---"] * len(header)) + "|"]
        for attr in cmp_attrs:
            label = _humanize(attr)
            cells = [label] + [_cell(s, attr) for s in self.subjects]
            rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows)

    def extract_chart_specs(self, max_charts: int = 3) -> list[dict]:
        """ДЕТЕРМИНИРОВАННЫЕ chart specs (Chart.js) из bundle.facts — без LLM,
        числа не могут галлюцинироваться. Аналог extract_chart_specs(matrix) из
        narrative_renderer, но работает с плоскими Fact'ами: группирует по
        атрибуту, берёт лучшее значение по confidence на субъект, парсит число
        из value-строки. Чартятся только атрибуты с ≥2 числовыми значениями и
        сопоставимыми единицами (₽/мес ↔ ₽/год на одну ось не попадают).

        Возвращает список specs вида:
          {title, chartType:"bar", labels:[...], datasets:[{label, data}],
           sourceCitations:[N,...]}
        Готов под фронтовый ChartCanvas (app.jsx) и event {"type":"chart","spec"}.
        """
        if not self.facts or len(self.subjects) < 2:
            return []

        # subject(slug) → {норм.атрибут: лучшая Fact по confidence}
        grid: dict[str, dict[str, Fact]] = {s: {} for s in self.subjects}
        for f in self.facts:
            subj = self.canonical_subject(f.subject)
            attr = _norm(f.attribute)
            if subj not in grid:
                grid[subj] = {}
            cur = grid[subj].get(attr)
            if cur is None or f.confidence > cur.confidence:
                grid[subj][attr] = f

        # Для каждого атрибута — парсим числа по субъектам (только те, у кого
        # значение реально распарсилось в число с единицей).
        attr_points: dict[str, list[tuple[str, float, str, int]]] = {}
        for attr in {a for subj in grid.values() for a in subj}:
            points: list[tuple[str, float, str, int]] = []
            for subj in self.subjects:
                f = grid.get(subj, {}).get(attr)
                if f is None:
                    continue
                num, unit = _parse_first_number_unit(f.value)
                if num is not None:
                    points.append((self.subject_labels.get(subj, subj),
                                    num, unit, f.source_n))
            if len(points) >= 2:
                attr_points[attr] = points

        # Фильтр по сопоставимости единиц + сортировка (больше различий/охвата).
        chartable: list[tuple[str, int, int, list]] = []
        for attr, points in attr_points.items():
            unit_classes = {_unit_class(p[2]) for p in points}
            unit_classes.discard("")
            if len(unit_classes) > 1:
                continue
            distinct = len({round(p[1], 3) for p in points})
            chartable.append((attr, distinct, len(points), points))
        chartable.sort(key=lambda t: (-t[1], -t[2], t[0]))
        chartable = chartable[:max_charts]

        out: list[dict] = []
        for attr, _distinct, _cov, points in chartable:
            present_map = {p[0]: p for p in points}
            labels: list[str] = []
            values: list = []
            sources_used: list[int] = []
            unit_seen = ""
            for subj in self.subjects:
                label = self.subject_labels.get(subj, subj)
                labels.append(label)
                hit = present_map.get(label)
                if hit is not None:
                    _, num, unit, sn = hit
                    values.append(num)
                    if sn and sn not in sources_used:
                        sources_used.append(sn)
                    if not unit_seen:
                        unit_seen = unit
                else:
                    values.append(None)
            if all(v is None for v in values):
                continue
            title = _humanize(attr)
            if unit_seen:
                title += f", {unit_seen}"
            out.append({
                "title": title,
                "chartType": "bar",
                "labels": labels,
                "datasets": [{"label": _humanize(attr), "data": values}],
                "sourceCitations": sources_used,
            })
        return out


# ════════════════════════════════════════════════════════════════════════
# Хелперы для chart specs (парсинг числа+единицы из value-строки Fact'а)
# ════════════════════════════════════════════════════════════════════════

# Число с разрядителями/дробью + опциональная единица (₽, %, лет, мес...).
_NUM_UNIT_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,](\d+))?\s*"
    r"(₽|руб|%|процент|тыс|млн|млрд|лет|год|дн|сут|мес)?",
    re.IGNORECASE)


def _parse_first_number_unit(text: str) -> tuple[float | None, str]:
    """Первое число с единицей из строки значения Fact'а.
    Возвращает (number, unit) или (None, ""). Для chart specs — берём главное
    число ячейки (первое в строке)."""
    m = _NUM_UNIT_RE.search(text or "")
    if not m:
        return None, ""
    raw = re.sub(r"[ \u00a0\u202f]", "", m.group(1))
    frac = m.group(2)
    try:
        num = float(raw + ("." + frac if frac else ""))
    except ValueError:
        return None, ""
    return num, (m.group(3) or "").lower()


def _unit_class(u: str) -> str:
    """Грубый класс единицы для сопоставимости на одной оси (зеркало
    narrative_renderer.extract_chart_specs). ₽/мес и ₽/год — РАЗНЫЕ классы
    (нельзя на одну ось); % — отдельно."""
    u = (u or "").lower().strip()
    if not u:
        return ""
    if "%" in u or "процент" in u:
        return "pct"
    per = ""
    if "мес" in u:
        per = "/mo"
    elif "год" in u or "лет" in u:
        per = "/yr"
    elif "дн" in u or "сут" in u:
        per = "/day"
    if "₽" in u or "руб" in u:
        return "rub" + per
    return u  # прочее как есть


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _humanize(attr: str) -> str:
    """snake_case/сырой атрибут → читаемая фраза для шапки таблицы."""
    t = (attr or "").strip()
    if not t:
        return "—"
    # snake_case → пробелы
    t = t.replace("_", " ")
    # capitalize первого слова, остальное как есть (сохраняет «₽», «%» и т.п.)
    return t[0].upper() + t[1:]


def _clone_fact(f: Fact, **overrides) -> Fact:
    """Не мутируем исходный Fact из артефактов агента; канонизируем в копии."""
    return Fact(
        subject=overrides.get("subject", f.subject),
        attribute=overrides.get("attribute", f.attribute),
        value=overrides.get("value", f.value),
        source_n=overrides.get("source_n", f.source_n),
        verbatim=overrides.get("verbatim", f.verbatim),
        conditions=list(overrides.get("conditions", f.conditions)),
        as_of=overrides.get("as_of", f.as_of),
        confidence=overrides.get("confidence", f.confidence),
        tags=list(overrides.get("tags", f.tags)),
    )


def _clone_complaint(c: Complaint, **overrides) -> Complaint:
    return Complaint(
        subject=overrides.get("subject", c.subject),
        theme=overrides.get("theme", c.theme),
        n_reviews=overrides.get("n_reviews", c.n_reviews),
        sentiment=overrides.get("sentiment", c.sentiment),
        sample_quotes=list(overrides.get("sample_quotes", c.sample_quotes)),
        period=overrides.get("period", c.period),
        source_ns=list(overrides.get("source_ns", c.source_ns)),
        rating_avg=overrides.get("rating_avg", c.rating_avg),
        is_stale=overrides.get("is_stale", c.is_stale),
    )
