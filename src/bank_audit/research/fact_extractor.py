"""Fact Extractor — расширенная версия triple_extractor.

В отличие от triple_extractor, извлекает обогащённые Fact-объекты:
  • верним verbatim-цитату (для demo-style narrative)
  • условия применения значения
  • квалификации (segment / requirement)
  • исключения
  • категория (fee / rate / limit / feature / requirement)
  • audit_priority (high / medium / low) — для focus-фильтра

Это база для качественного narrative-генератора.
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from typing import Any

from openai import AsyncOpenAI

from .entity_extractor import Entity
from .source_finder import GoldSource
from .fact import Fact
from .triple_extractor import _parse_json_array, _try_parse_numeric

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — старший аналитик данных банковских продуктов. Извлекаешь
ВСЕ конкретные характеристики продукта из источников, в виде ОБОГАЩЁННЫХ фактов.

Каждый факт = JSON-объект с обязательными и опциональными полями:

ОБЯЗАТЕЛЬНЫЕ:
  • attribute       — snake_case на русском, "годовое_обслуживание"
  • value           — строковое представление, "0", "от 6 до 22", "паспорт+СНИЛС"
  • unit            — "₽","%","лет","дней","руб/мес","" (для перечислений)
  • verbatim_quote  — ДОСЛОВНАЯ цитата из источника, 1-2 предложения (50-300 chars)
  • source_idx      — НОМЕР источника (1-based в переданном списке)
  • category        — fee / rate / limit / feature / requirement / regulation
  • audit_priority  — high / medium / low

ОПЦИОНАЛЬНЫЕ (но КРИТИЧНЫЕ для качества аудита):
  • conditions      — массив УСЛОВИЙ применения значения, если они есть
                       Например для "0₽ обслуживание":
                       ["при зачислении пенсии", "при остатке от 30000 ₽",
                        "при тратах от 5000 ₽/мес"]
  • qualifications  — ТЕКСТОМ кому ДОСТУПЕН продукт/условие
                       Например: "только Premium-клиенты (от 5 млн ₽)"
                       Например: "только для граждан РФ старше 18"
                       Если общедоступно — пустая строка
  • exceptions      — массив ИСКЛЮЧЕНИЙ из общего правила
                       Например: ["для счетов в долларах комиссия 100₽",
                                  "первые 3 месяца — бесплатно"]

ПРАВИЛА:

1) АТРИБУТЫ — нормализованные имена. Не копируй фразу дословно.
   "комиссия за выпуск карты" → "комиссия_за_выпуск"
   "плата за обслуживание счёта в год" → "годовое_обслуживание"

2) КАТЕГОРИЯ — обязательна:
   • fee          — комиссия / стоимость / тариф (₽)
   • rate         — ставка / процент (%)
   • limit        — лимит / макс / мин (₽ / шт)
   • feature      — функция / опция (да / нет / список)
   • requirement  — требование к клиенту (документ / возраст / доход)
   • regulation   — норматив / правило (ссылка на закон)

3) AUDIT_PRIORITY:
   • high   — критичные параметры аудита (тариф основной, ставка, лимит, требования к клиенту)
   • medium — важные но не критичные (бонусы, доп.условия, оформление)
   • low    — периферия (дизайн карты, упаковка, ник-нейм)

4) CONDITIONS — это «ПРИ КАКИХ УСЛОВИЯХ это значение справедливо»:
   Если в источнике «0 ₽ при зачислении зарплаты от 15000 ₽» —
   conditions = ["при зачислении зарплаты от 15000 ₽"]
   Если в источнике «обслуживание 990 ₽/мес, бесплатно для Premium» —
   value=990, unit=₽/мес, exceptions=["бесплатно для Premium"]
   conditions и exceptions — РАЗНЫЕ:
     conditions = при каких условиях значение становится валидным
     exceptions = когда правило НЕ применяется

5) VERBATIM_QUOTE — ДОСЛОВНО из текста, 50-300 chars, без переформулировок.
   Это для аудитора чтобы он мог самостоятельно проверить.

6) ИЗВЛЕКАЙ ТОЛЬКО ПРО ЗАПРОШЕННЫЙ ПРОДУКТ (см. блок ENTITY → Продукт).
   Страница банка часто описывает НЕСКОЛЬКО продуктов рядом. Бери факты
   ТОЛЬКО про запрошенный продукт и его синонимы. Игнорируй СОСЕДНИЕ продукты:
   • спросили «накопительный счёт» → НЕ бери «Вклад N», «Копилку», «Сейф»,
     дебетовую/кредитную карту, другие отдельные продукты банка;
   • если на странице таблица/список разных продуктов — бери только строку
     запрошенного.
   НЕ ИЗВЛЕКАЙ: маркетинговые слоганы; промо-числа вне базовых условий;
   универсальные правила («звоните 900»); характеристики ДРУГИХ продуктов банка.

7) ГЛУБИНА СТАВОК/ЦЕН «до X» / «от X» — «витрина против реальности»:
   Рекламная «до 16 %» / «от 0 ₽» почти всегда условна. Извлеки СУТЬ:
   • заголовочное значение (value="до 16", unit="%") +
     conditions с тем, ЧТО активирует максимум/минимум
     («первые 2 месяца», «при покупках от 100 000 ₽», «только новым клиентам»);
   • если в тексте ЕСТЬ базовая/обычная ставка после акции — добавь ОТДЕЛЬНЫЙ
     факт (attribute="базовая_ставка"). Аудитору критична разница витрина↔база.

8) ИМЕНА АТРИБУТОВ — КАНОНИЧЕСКИЕ и максимально ОБЩИЕ. Один смысл = одно имя,
   чтобы банки сравнивались по одинаковым полям.
   ⛔ КРИТИЧНО: НЕ кодируй min/max/мин/макс/от/до/«по автокредиту»/режим В ИМЯ
      атрибута. ЗАПРЕЩЕНЫ имена вида: "процент_ставка_min", "процентная_ставка_макс",
      "процент_по_автокредиту_min", "полная_стоимость_кредита_мин",
      "максимальная_сумма_кредита", "минимальный_срок_кредита".
      Вместо них — ОДНО имя: "процентная_ставка", "полная_стоимость_кредита",
      "сумма_кредита", "срок_кредита". Минимум/максимум выражай в VALUE (диапазон),
      а режим (база/промо/для новых) — в conditions, НЕ в имени.
   Канон для кредитов: процентная_ставка, полная_стоимость_кредита, сумма_кредита,
      срок_кредита, первоначальный_взнос, комиссия_за_выдачу, страхование,
      требуемый_доход, возраст_клиента, досрочное_погашение, залог.
   Канон для вкладов/счетов: процент_на_остаток, срок, капитализация,
      годовое_обслуживание, минимальный_остаток.

9) ДИАПАЗОНЫ vs РЕЖИМЫ — ключевое различие, чтобы не плодить дубли:
   • ДИАПАЗОН ОДНОГО параметра («ставка 20,9–34,6 %», «сумма 100 000 – 8 000 000 ₽»,
     «срок 1–5 лет») → ОДИН факт: value="20,9–34,6" (или "100000–8000000"), unit="%".
     НЕ создавай отдельные факты «min» и «max».
   • РЕЖИМЫ/СТУПЕНИ одного параметра (база 6,5 % / промо 12,5 % / для зарплатных) →
     НЕСКОЛЬКО фактов с ОДНИМ И ТЕМ ЖЕ именем "процентная_ставка", различающихся
     через conditions (["первые 2 мес"], ["базовая, с 3-го мес"], ["для новых"]).
   • ТОЧНОСТЬ: числа ровно как в источнике (не округляй); десятичный разделитель —
     запятая («20,9», не «20.9»); срок указывай в годах, если в источнике годы.

10) ЕСЛИ В ТЕКСТЕ НЕТ КОНКРЕТНЫХ ФАКТОВ ПРО ЗАПРОШЕННЫЙ ПРОДУКТ — верни [].
    Лучше 0, чем факты про чужой продукт или выдуманные.

ВЫХОД: JSON массив фактов. БЕЗ преамбулы, БЕЗ markdown-fences.
[
  {"attribute":"годовое_обслуживание", "value":"0", "unit":"₽",
   "verbatim_quote":"Бесплатное обслуживание при зачислении пенсии от 1 руб/мес",
   "source_idx":1, "category":"fee", "audit_priority":"high",
   "conditions":["при зачислении пенсии"], "qualifications":"",
   "exceptions":[]},
  ...
]"""


# Сигналы «здесь есть факты»: валюта, проценты, числа, единицы измерения
_FACT_SIGNAL = re.compile(
    r"\d[\d  .,]*\s*(?:₽|руб|%|млн|млрд|тыс|год|лет|мес|дн|₽/|p\.)"
    r"|₽|\bот\s+\d|\bдо\s+\d|\bкомисс|\bставк|\bбаланс|\bлимит|\bобслуживан",
    re.IGNORECASE,
)


def _window_score(window: str, product_terms: list[str]) -> float:
    """Оценка «насколько в окне много фактов про продукт»."""
    low = window.lower()
    score = float(len(_FACT_SIGNAL.findall(window)))   # плотность фактов
    # Бонус за термины продукта
    for t in product_terms:
        if t and t in low:
            score += 2.0
    # Штраф за «меню»: много коротких слов через запятую без чисел
    commas = low.count(",")
    digits = sum(c.isdigit() for c in window)
    if commas > 25 and digits < 8:
        score -= 5.0
    return score


def _relevant_excerpt(text: str, product_terms: list[str],
                       budget: int = 11000, win: int = 1400) -> str:
    """Выбирает наиболее насыщенные фактами фрагменты больших страниц.

    Вместо «первые N символов» (на SPA это меню-шапка) — скользящее окно,
    скоринг по плотности фактов/чисел/терминов, top-окна до budget,
    затем восстановление исходного порядка для читаемости.
    Generic: работает для любого продукта (ставки/комиссии/лимиты/баланс).
    """
    text = (text or "").strip()
    if len(text) <= budget:
        return text
    # Режем на окна с перекрытием
    step = int(win * 0.75)
    windows = []
    for start in range(0, len(text), step):
        chunk = text[start:start + win]
        if len(chunk) < 200:
            continue
        windows.append((start, chunk, _window_score(chunk, product_terms)))
    if not windows:
        return text[:budget]
    # Топ-окна по score, пока не наберём budget
    ranked = sorted(windows, key=lambda x: -x[2])
    picked, total = [], 0
    for start, chunk, sc in ranked:
        if sc <= 0 and picked:
            break
        picked.append((start, chunk))
        total += len(chunk)
        if total >= budget:
            break
    if not picked:  # всё нулевое — берём голову
        return text[:budget]
    # Восстанавливаем порядок по позиции
    picked.sort(key=lambda x: x[0])
    return "\n…\n".join(c for _, c in picked)


def _build_sources_block(sources: list[GoldSource],
                          product_terms: list[str] | None = None,
                          total_budget: int = 70000) -> str:
    """Собирает блок источников с релевантной выборкой.

    Бюджет распределяется по источникам: при 5 источниках ~12k каждому,
    при 10 — ~7k. Так общий промпт не раздувается (~18k токенов max),
    но каждый источник получает достаточно для извлечения фактов.
    """
    parts = []
    terms = [t.lower() for t in (product_terms or []) if t]
    n = max(1, len(sources))
    per = max(4000, min(12000, total_budget // n))
    for i, s in enumerate(sources, 1):
        title = (s.title or s.url)[:120]
        body = _relevant_excerpt(s.text or "", terms, budget=per)
        parts.append(f"### Source [{i}] — {title}\nURL: {s.url}\n\n{body}")
    return "\n\n---\n\n".join(parts)


async def extract_facts(client: AsyncOpenAI, entity: Entity,
                          sources: list[GoldSource],
                          core_schema_hint: str | None = None,
                          model: str | None = None) -> list[Fact]:
    """Извлекает Fact-объекты для одного entity из gold sources.

    core_schema_hint — рекомендация какие 10-15 атрибутов искать в первую очередь.
    """
    if not sources:
        return []
    model = model or os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME",
                                                                 "gpt-4o-mini")
    # Термины продукта для релевантной выборки фрагментов больших страниц
    product_terms = [entity.product.lower()]
    product_terms += [s.lower() for s in (entity.product_synonyms or []) if len(s) >= 4]
    for w in entity.product.lower().split():
        if len(w) >= 5:
            product_terms.append(w)
    sources_block = _build_sources_block(sources, product_terms=product_terms)
    user_msg = (
        f"# ENTITY\nБанк: {entity.bank_name} (slug: {entity.bank_slug})\n"
        f"Продукт: {entity.product}\n"
        + (f"Аудитория: {entity.audience}\n" if entity.audience else "")
        + (core_schema_hint or "")
        + f"\n\n# SOURCES\n{sources_block}\n\n"
        f"Извлеки ОБОГАЩЁННЫЕ факты ТОЛЬКО про продукт «{entity.product}» "
        f"(игнорируй другие продукты банка на странице). Для ставок/цен «до X»/«от X» "
        f"раскрывай условия максимума и базовое значение (витрина↔реальность). "
        f"verbatim_quote обязателен, source_idx — номер (1-{len(sources)}). "
        f"НЕ выдумывай чисел."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=6000, temperature=0.0,
            ),
            timeout=90,
        )
    except Exception as e:
        log.warning("[fact_extractor] %s LLM failed: %s", entity.bank_slug, e)
        return []

    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_json_array(raw)
    if not isinstance(data, list):
        log.warning("[fact_extractor] %s no JSON array (raw 200=%r)",
                     entity.bank_slug, raw[:200])
        return []

    facts: list[Fact] = []
    seen: set[tuple] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        attr = (item.get("attribute") or "").strip().lower().replace(" ", "_")
        if not attr:
            continue
        # NB: дедуп НЕ по attribute (это теряло «лестницу» режимов: базовая/промо/
        # после периода/для новых). Дедуп по семантическому ключу ниже.
        value = str(item.get("value") or "").strip()
        if not value or value.lower() in ("null", "none", "—", "-", ""):
            continue
        unit = str(item.get("unit") or "").strip()
        try:
            src_idx = int(item.get("source_idx") or 0)
        except Exception:
            src_idx = 0
        if src_idx < 1 or src_idx > len(sources):
            continue

        # Опциональные обогащающие поля
        verbatim = str(item.get("verbatim_quote") or "").strip()[:400]
        conditions = item.get("conditions") or []
        if not isinstance(conditions, list):
            conditions = []
        conditions = [str(c).strip()[:200] for c in conditions if c][:6]
        qualifications = str(item.get("qualifications") or "").strip()[:300]
        exceptions = item.get("exceptions") or []
        if not isinstance(exceptions, list):
            exceptions = []
        exceptions = [str(e).strip()[:200] for e in exceptions if e][:6]

        category = str(item.get("category") or "feature").strip().lower()
        if category not in ("fee", "rate", "limit", "feature", "requirement", "regulation"):
            category = "feature"
        audit_priority = str(item.get("audit_priority") or "medium").strip().lower()
        if audit_priority not in ("high", "medium", "low"):
            audit_priority = "medium"

        confidence = str(item.get("confidence") or "high").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "high"

        # Семантический дедуп: один и тот же attribute может иметь НЕСКОЛЬКО
        # валидных значений (базовая/промо/после-периода ставка, разные условия).
        # Режем только полные дубли (attr+value+source+условия).
        cond_key = "|".join(sorted(conditions)) + "##" + qualifications + "##" + "|".join(sorted(exceptions))
        dkey = (attr, value.lower(), src_idx, cond_key[:160])
        if dkey in seen:
            continue
        seen.add(dkey)
        facts.append(Fact(
            entity_bank_slug=entity.bank_slug,
            attribute=attr,
            value=value,
            unit=unit,
            value_numeric=_try_parse_numeric(value, unit),
            conditions=conditions,
            qualifications=qualifications,
            exceptions=exceptions,
            verbatim_quote=verbatim,
            category=category,
            audit_priority=audit_priority,
            source_idx=src_idx,
            source_url=sources[src_idx - 1].url,
            confidence=confidence,
        ))

    n_high = sum(1 for f in facts if f.audit_priority == "high")
    n_with_conditions = sum(1 for f in facts if f.conditions)
    log.warning("[fact_extractor] %s × %s → %s facts (%s high-priority, %s w/conditions)",
                 entity.bank_slug, entity.product[:30], len(facts),
                 n_high, n_with_conditions)
    return facts
