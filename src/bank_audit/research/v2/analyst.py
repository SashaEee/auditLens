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

from .knowledge_bundle import KnowledgeBundle
from .conductor import ResearchPlan

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — главный аудитор-аналитик. Пишешь ИТОГОВЫЙ отчёт по
результатам исследования для коллеги-аудитора. Цена ошибки высокая —
достоверность важнее красоты.

Тебе передан KNOWLEDGE BUNDLE: факты по субъектам со ссылками [N], жалобы
клиентов с цитатами, регуляторные нормы, инсайты, рейтинг (если есть),
честные пробелы.

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
Markdown-таблица с ключевыми параметрами. НЕ дублируй ВСЕ факты — выбери
те, что несут различие между субъектами. На одинаковых параметрах (напр.
«всё 0 ₽») — явно укажи что одинаково.

ВАЖНО: тебе передана ГОТОВАЯ детерминированная таблица (секция «ТАБЛИЦА
СРАВНЕНИЯ (готовая)») — собранная напрямую из фактов без LLM. Вставляй ЕЁ
(числа и ссылки [N] точные). Можешь сократить строки/переупорядочить, но
НЕ меняй значения и НЕ добавляй числа, которых там нет.

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

ВЫХОД: чистый markdown отчёта. БЕЗ преамбулы («Вот отчёт...»), БЕЗ финальных
комментариев. Начни сразу с # заголовка.
"""


async def write_report(client: AsyncOpenAI, bundle: KnowledgeBundle,
                        plan: ResearchPlan, model: str | None = None,
                        preview_emitted: bool = False) -> str:
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
    # Bundle → текстовый контекст для промпта
    context = bundle.to_prompt_context(max_chars=28000)

    # Детерминированная таблица сравнения (из фактов, без LLM) — §4b.
    # При preview_emitted таблица уже отрисована ранним preview — НЕ вставляем
    # её в промпт как секцию, лишь напоминаем аналитику НЕ дублировать.
    table_md = bundle.to_comparison_table()
    table_block = ""
    if preview_emitted:
        table_block = (
            "\n\n# ВАЖНО: сравнительная таблица уже отрисована вверху отчёта\n"
            "НЕ повторяй её отдельной секцией «Сравнение условий» и НЕ пиши "
            "повторный заголовок «# Аудит-отчёт». Начинай сразу с анализа "
            "(TL;DR / ключевые выводы), опираясь на факты из bundle. "
            "Числа/ссылки бери из bundle, не из таблицы."
        )
    elif table_md:
        table_block = (
            "\n\n# ТАБЛИЦА СРАВНЕНИЯ (готовая — вставь в раздел «Сравнение условий»)\n"
            "Числа и [N] здесь точные (собрано из фактов детерминированно). "
            "Переупорядочи/сократи строки при необходимости, но НЕ меняй значения "
            "и НЕ добавляй числа, которых тут нет.\n\n" + table_md
        )

    # Сигнализируем структуру из плана
    sections_hint = ", ".join(plan.output_sections) if plan.output_sections else "по умолчанию"

    user_msg = (
        f"# ВОПРОС АУДИТОРА\n{bundle.question}\n\n"
        f"# ИНТЕНТ\n{plan.intent_summary or bundle.intent}\n\n"
        f"# РЕКОМЕНДУЕМЫЕ СЕКЦИИ\n{sections_hint}\n\n"
        f"{context}{table_block}\n\n"
        f"Напиши итоговый отчёт по структуре из системного промпта. "
        f"Отвечай на ВСЕ части вопроса аудитора. Каждое число — со ссылкой [N]. "
        f"Если есть рейтинг/жалобы/инсайты — они ДОЛЖНЫ быть в отчёте."
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_msg}],
            temperature=0.0,
            max_tokens=6000,
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
