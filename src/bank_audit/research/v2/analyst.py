"""Analyst — писатель итогового отчёта.

Получает KnowledgeBundle (все артефакты от агентов) и пишет связный
аудиторский отчёт. Это последний «мозговой» вызов перед critic.

Принципы (вшиты в промпт):
  • Отвечает на ВСЕ части вопроса аудитора.
  • Аналитика, а не пересказ фактов: дельты, витрина↔реальность, риски.
  • Каждое число — со ссылкой [N] из bundle.
  • Честные пробелы — first-class (не маскируются).
  • Глубина как в эталоне: терминологические ловушки, регуляторный контекст.
"""
from __future__ import annotations

import logging
import os

from openai import AsyncOpenAI

from ...ai.llm_utils import deep_reasoning_extra
from .knowledge_bundle import KnowledgeBundle
from .conductor import ResearchPlan

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — главный аналитик службы внутреннего аудита Сбербанка.
Пишешь ИТОГОВЫЙ отчёт по результатам исследования для коллеги-аудитора. Цена
ошибки высокая — достоверность важнее красоты. Аудит смотрит на банк ГЛАЗАМИ
Сбера: где Сбер выгоднее/хуже рынка, какие у него и у конкурентов подводные
камни, какие регуляторные и репутационные риски.

Тебе передан KNOWLEDGE BUNDLE: факты по субъектам со ссылками [N] и ДОСЛОВНЫМИ
цитатами из источников, жалобы клиентов с цитатами, регуляторные нормы, инсайты,
рейтинг (если есть), честные пробелы, а также блок «ВЫДЕРЖКИ ИЗ ИСТОЧНИКОВ».

ГЛУБИНА — главное требование (от тебя ждут анализ уровня профильного аудитора,
а не пересказ таблицы дженерик-моделью):
  • ОПИРАЙСЯ НА ТЕКСТ источников (выдержки/цитаты), а не только на пары
    «атрибут: значение». Где уместно — приведи короткую цитату с [N].
  • ПО КАЖДОМУ субъекту × каждому измерению — минимум 2-3 предложения конкретики
    с числами и [N], ЛИБО явное «нет данных». Не отделывайся одной строкой.
  • ВСЕГДА выделяй позицию Сбера относительно рынка (дешевле/дороже/строже на
    столько-то ₽/п.п./раз), даже если вопрос прямо об этом не просит.
  • Числовые дельты — в ₽, % или процентных пунктах (п.п.), «в N раз».
  • Называй аномалии и «подводные камни»: скрытые условия, оговорки мелким
    шрифтом, расхождение витрины и реальных тарифов, методологические ловушки.

СТРУКТУРА отчёта (выбери секции ПО НАЛИЧИЮ данных — не плоди пустые):

## TL;DR / Главный вывод
1 абзац. Главный вывод, меняющий рамку сравнения. Если есть ключевой инсайт
(напр. «цены уравнены регулятором → ранжируем по гибкости») — это заголовок.

## Ключевые выводы (3-5 пунктов)
НЕ пересказ фактов, а АНАЛИТИКА:
  • Где субъекты расходятся сильнее всего (с числами «в N раз / на X ₽»).
  • Витрина↔реальность (реклама vs реальные условия).
  • Терминологические/методологические ловушки (если есть).
  • Регуляторный контекст, меняющий сравнение.

## Сравнение условий
ПОСТРОЙ markdown-таблицу сравнения САМ из фактов bundle. Колонки — субъекты
(банки), строки — параметры сравнения.

КРИТИЧНО — СЕМАНТИЧЕСКОЕ ОБЪЕДИНЕНИЕ АТРИБУТОВ: факты по разным банкам собирали
РАЗНЫЕ агенты, и один и тот же параметр назван по-разному:
  «автоперевод: комиссия» ≈ «комиссия за перевод с кредитки» ≈
  «комиссия автоперевода (C2C)»  →  ОДНА строка «Комиссия за перевод (C2C)».
Сведи близкие по смыслу атрибуты разных банков в ОБЩИЕ строки, чтобы банки
стояли в одну линию и реально сравнивались. БЕЗ этого объединения таблица
разваливается на десятки одиночных строк — это главная задача.

ПРАВИЛА таблицы:
  • 8-15 строк — те, что РАЗЛИЧАЮТ банки или важны аудитору (направления,
    триггеры, комиссии по типам операций, лимиты сутки/операция/месяц,
    отмена/пауза, сроки зачисления).
  • Значения бери ДОСЛОВНО из фактов, с ссылкой [N]. НЕ меняй и НЕ выдумывай числа.
  • У банка нет данных по строке → «—». Не угадывай.
  • Несколько значений у банка (внутри/вне банка) → через «; » каждое со своим [N].
  • На одинаковых параметрах («у всех 0 ₽») — явно укажи, что одинаково.

## Рейтинг (если есть в bundle)
Презентуй рейтинг с обоснованием. Если критерий нестандартный (напр.
«по гибкости т.к. цены уравнены») — объясни почему.

## На что жалуются клиенты
По каждому субъекту — топ-жалобы с цитатами. Разделяй свежие/устаревшие.
Если жалобы устарели — честно скажи «свежей выборки нет».

## Регуляторика (если есть)
Кратко: какие нормы регулируют тему.

## Риски и рекомендации аудитору
КОНКРЕТНЫЕ, привязанные к числам/субъектам:
  • «Сбер дороже Альфы на X ₽ — сверить условие Y из [n]».
  • «Жалобы на сбои у N — проверить надёжность в проде».
  НЕ общие «запросить тарифы».

## Честные оговорки
  • Что НЕ нашли (из coverage_notes).
  • Даты/актуальность данных.
  • Где данные вторичные (требуют сверки с первоисточником).

ЖЁСТКИЕ ПРАВИЛА:
  • КАЖДОЕ число — ТОЛЬКО из bundle, со ссылкой [N]. Не выдумывай.
  • Если данные по субъекту отсутствуют — пиши «нет данных», не угадывай.
  • Не складывай разнотипные величины (разовую комиссию + годовую ставку).
  • Стиль аудитора: сухо, по делу, с цифрами и рисками. Без маркетинга.
  • НЕ повторяй одно и то же в разных секциях.
  • Опирайся на «ВЫДЕРЖКИ ИЗ ИСТОЧНИКОВ»: если цитата источника расходится с
    заявленным значением (витрина vs реальность) — обязательно отметь это.
  • АКТУАЛЬНОСТЬ: устаревшие факты перечислены в блоке «АКТУАЛЬНОСТЬ ДАННЫХ».
    НЕ помечай их ⚠ и НЕ пиши «устарело» в основном тексте и таблице — это
    визуальный шум, рушит доверие к отчёту. Вместо этого собери их ОДНИМ
    аккуратным списком в самом конце, в «Честных оговорках» (подраздел
    «Актуальность данных»): «<банк> — <параметр>: данные за YYYY, требуют сверки
    с первоисточником». В таблице и выводах значения подавай как есть, без меток.

ВЫХОД: чистый markdown отчёта. БЕЗ преамбулы («Вот отчёт...»), БЕЗ финальных
комментариев. Начни сразу с # заголовка.
"""


async def write_report(client: AsyncOpenAI, bundle: KnowledgeBundle,
                        plan: ResearchPlan, model: str | None = None,
                        preview_emitted: bool = False,
                        on_reasoning=None) -> str:
    """Пишет итоговый отчёт из bundle. Возвращает markdown.

    preview_emitted=True — оркестратор уже отдал сравнительную таблицу как
    раннее preview ДО вызова писателя (контракт ранней отдачи §5a). Тогда НЕ
    вставляем таблицу в промпт и просим аналитика писать только АНАЛИЗ, не
    дублируя шапку/таблицу — иначе пользователь видит её дважды."""
    if not bundle.facts and not bundle.complaints and not bundle.insights:
        return _empty_report(bundle)

    # Аналитик (нарратив) — reasoning-стадия: держим на сильной модели
    # (LLM_MODEL_ANALYST), даже когда извлечение/critic переведены на быструю
    # (LLM_MODEL_SMART). Приоритет: явный аргумент → ANALYST → SMART → NAME.
    model = (model or os.getenv("LLM_MODEL_ANALYST")
             or os.getenv("LLM_MODEL_SMART")
             or os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"))
    # Bundle → текстовый контекст для промпта. rich=True добавляет дословные
    # цитаты под фактами + блок «ВЫДЕРЖКИ ИЗ ИСТОЧНИКОВ» (главный рычаг глубины:
    # писатель рассуждает по тексту, а не по голым «атрибут: значение»). Бюджет
    # поднят — у модели большой контекст, а заземление важнее экономии токенов.
    context = bundle.to_prompt_context(
        max_chars=int(os.getenv("V2_ANALYST_CONTEXT_CHARS", "44000")), rich=True)

    # Таблицу сравнения аналитик строит САМ в секции «Сравнение условий»
    # (см. SYSTEM_PROMPT): LLM семантически объединяет разнящиеся названия
    # атрибутов от per-bank агентов в общие строки. Готовую детерминированную
    # таблицу больше НЕ передаём (она схлопывалась до пары строк из-за матча
    # имён атрибутов по точному совпадению). Критик потом сверит числа таблицы
    # с фактами bundle — защита от галлюцинаций сохраняется.

    # Сигнализируем структуру из плана
    sections_hint = ", ".join(plan.output_sections) if plan.output_sections else "по умолчанию"

    user_msg = (
        f"# ВОПРОС АУДИТОРА\n{bundle.question}\n\n"
        f"# ИНТЕНТ\n{plan.intent_summary or bundle.intent}\n\n"
        f"# РЕКОМЕНДУЕМЫЕ СЕКЦИИ\n{sections_hint}\n\n"
        f"{context}\n\n"
        f"Напиши итоговый отчёт по структуре из системного промпта. ГЛУБОКО, на "
        f"уровне профильного аудитора: по каждому субъекту×измерению — конкретика "
        f"с числами и [N] (или явное «нет данных»), позиция Сбера vs рынок, "
        f"подводные камни. Опирайся на ВЫДЕРЖКИ ИЗ ИСТОЧНИКОВ, а не только на "
        f"«атрибут: значение». Каждое число — со ссылкой [N]. "
        f"Если есть рейтинг/жалобы/инсайты — они ДОЛЖНЫ быть в отчёте."
    )

    _msgs = [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": user_msg}]
    _max_tok = int(os.getenv("V2_ANALYST_MAX_TOKENS", "10000"))
    try:
        if on_reasoning is not None:
            # Стрим-режим: reasoning льётся наружу живьём, content собираем
            # целиком (прокси его всё равно буферизует) для _clean_citations.
            from ._streaming import stream_completion
            md, _r, _t = await stream_completion(
                client, on_reasoning=on_reasoning,
                model=model, messages=_msgs, temperature=0.2,
                max_tokens=_max_tok, extra_body=deep_reasoning_extra())
            md = (md or "").strip()
        else:
            resp = await client.chat.completions.create(
                model=model, messages=_msgs,
                temperature=0.2,   # лёгкая «вариативность» против плоского пересказа
                max_tokens=_max_tok,
                extra_body=deep_reasoning_extra(),  # нарратив — reasoning-шаг: effort=high
            )
            md = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[analyst] LLM failed: %s — детерминированный фоллбэк", e)
        return _deterministic_report(bundle, preview_emitted=preview_emitted) \
            or _empty_report(bundle)

    if not md:
        return _deterministic_report(bundle, preview_emitted=preview_emitted) \
            or _empty_report(bundle)

    # Анти-галлюцинация: убираем невалидные цитаты [N]
    allowed = {i + 1 for i in range(len(bundle.sources.all()))}
    md = _clean_citations(md, allowed)
    return md


def _deterministic_report(bundle: KnowledgeBundle,
                            preview_emitted: bool = False) -> str:
    """СТРАХОВКА: собрать отчёт из bundle БЕЗ LLM, когда писатель упал/пуст, но
    данные есть. Лучше заземлённая структура из фактов, чем «не удалось собрать»
    (зеркалит детерминированный фоллбэк v1). Помечается как авто-сборка.

    preview_emitted=True — таблица уже отдана ранним preview оркестратора, НЕ
    дублируем секцию «Сравнение условий»."""
    if not bundle.facts and not bundle.complaints and not bundle.ranking:
        return ""
    if preview_emitted:
        p = ["> ⚠ _Рассуждающая модель была недоступна; отчёт собран "
             "детерминированно из извлечённых фактов. Сравнительная таблица "
             "выше — точная, выводы/рейтинг проверьте по разбору ниже._", ""]
    else:
        p = [f"# Аудит-отчёт: {bundle.question}", "",
             "> ⚠ _Рассуждающая модель была недоступна; отчёт собран "
             "детерминированно из извлечённых фактов. Выводы/рейтинг проверьте "
             "по разбору ниже._", ""]
    # Инсайты
    if bundle.insights:
        p.append("## Ключевые инсайты")
        for ins in bundle.insights:
            cite = "".join(f"[{n}]" for n in ins.evidence_ns)
            p.append(f"- **{ins.headline}** — {ins.explanation} {cite}")
        p.append("")
    # Рейтинг
    if bundle.ranking and bundle.ranking.entries:
        p.append(f"## Рейтинг ({bundle.ranking.criterion})")
        for e in bundle.ranking.sorted_entries():
            label = bundle.subject_labels.get(e.subject, e.subject)
            gap = " _(недостаточно данных)_" if e.data_gap else ""
            cite = "".join(f"[{n}]" for n in e.evidence_ns)
            p.append(f"{e.rank}. **{label}** ({e.score:g}/10){gap} — {e.rationale} {cite}")
        p.append("")
    # Сравнительная таблица (детерминированно из фактов — числа не галлюцинируются).
    # При preview_emitted таблица уже отрисована — не дублируем.
    table_md = bundle.to_comparison_table()
    if table_md and not preview_emitted:
        p.append("## Сравнение условий")
        p.append(table_md)
        p.append("")

    # Разбор по субъектам
    p.append("## Разбор по банкам")
    for subj in bundle.subjects:
        label = bundle.subject_labels.get(subj, subj)
        fs = bundle.facts_for(subj)
        if not fs:
            p.append(f"- **{label}** — нет данных в открытых источниках.")
            continue
        items = []
        for f in fs[:10]:
            cond = f" ({'; '.join(f.conditions)})" if f.conditions else ""
            items.append(f"{f.attribute}: {f.value}{cond} [{f.source_n}]")
        p.append(f"- **{label}** — " + "; ".join(items) + ".")
    p.append("")
    # Жалобы
    if bundle.complaints:
        p.append("## На что жалуются клиенты")
        for c in bundle.complaints:
            label = bundle.subject_labels.get(c.subject, c.subject)
            stale = " _(устаревшие)_" if c.is_stale else ""
            cite = "".join(f"[{n}]" for n in c.source_ns[:3])
            p.append(f"- **{label}** — {c.theme}: {c.n_reviews} отзыв{stale} {cite}")
    # Пробелы
    if bundle.coverage_notes:
        p.append("\n## Честные пробелы")
        for n in bundle.coverage_notes:
            subs = ", ".join(n.subjects) if n.subjects else "—"
            p.append(f"- {n.what} ({subs}): {n.reason}")
    # Источники
    if bundle.sources.all():
        p.append("\n## Источники")
        for i, s in enumerate(bundle.sources.all(), 1):
            p.append(f"{i}. [{s.title or s.url[:60]}]({s.url}) — _{s.domain}_")
    return "\n".join(p)


def _empty_report(bundle: KnowledgeBundle) -> str:
    """Минимальный отчёт когда данных нет."""
    parts = [f"# Аудит-отчёт: {bundle.question}", ""]
    parts.append("_Не удалось собрать достаточно данных по вопросу._")
    if bundle.coverage_notes:
        parts.append("\n**Пробелы:**")
        for n in bundle.coverage_notes:
            parts.append(f"- {n.what} ({', '.join(n.subjects) or '—'})")
    if bundle.sources.all():
        parts.append("\n## Источники")
        for i, s in enumerate(bundle.sources.all(), 1):
            parts.append(f"{i}. [{s.title}]({s.url}) — _{s.domain}_")
    return "\n".join(parts)


def _clean_citations(text: str, allowed: set[int]) -> str:
    """Удаляет [N] с несуществующими номерами источников."""
    import re
    def _repl(m):
        n = int(m.group(1))
        return m.group(0) if n in allowed else ""
    return re.sub(r"\[(\d+)\]", _repl, text)
