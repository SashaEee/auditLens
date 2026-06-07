"""Narrative Renderer — orchestrator narrative-генераторов.

Заменяет старый matrix_renderer.py. Использует:
  1. outline_planner.plan_sections() — LLM выбирает 5-8 секций для отчёта
  2. Для каждой секции вызывает соответствующий narrative-генератор
     (key_findings, per_entity_breakdown, pricing_breakdown, ...)
  3. Собирает финальный markdown

Параллелизация: секции которые не зависят друг от друга — генерируются
параллельно через asyncio.gather (значимое ускорение).

Чарты: extract_chart_specs() остаётся detect numerical attributes из Matrix.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from .fact import Fact
from .entity_extractor import Entity
from .matrix_builder import Matrix
from .outline_planner import Section, plan_sections
from .core_schema import CoreAttr
from .narrative_generators import (
    NarrativeContext,
    key_findings,
    per_entity_breakdown,
    pricing_breakdown,
    regulatory_box,
    cant_do_box,
    requirements_box,
    digital_channels,
    risks_recommendations,
    conflict_explainer,
    government_programs,
)

log = logging.getLogger(__name__)


# Маппинг kind → async-generator-функция.
# Каждая принимает (ctx, **kwargs) → markdown string.
SECTION_GENERATORS: dict[str, Callable[..., Awaitable[str]]] = {
    "key_findings":          key_findings.generate,
    "per_entity_breakdown":  per_entity_breakdown.generate,
    "pricing_breakdown":     pricing_breakdown.generate,
    "regulatory_box":        regulatory_box.generate,
    "cant_do_box":           cant_do_box.generate,
    "requirements_box":      requirements_box.generate,
    "digital_channels":      digital_channels.generate,
    "risks_recommendations": risks_recommendations.generate,
    "government_programs":   government_programs.generate,
}


def _comparison_table_md(matrix: Matrix) -> str:
    """Markdown сравнительной таблицы из Matrix (детерминированный, без LLM)."""
    if not matrix.entities or not matrix.attributes:
        return "## 📋 Сравнительная таблица\n\n_Нет данных для сравнения._"

    core = getattr(matrix, "core_attrs", []) or []
    if core:
        top_attrs = [a for a in core if a in matrix.attributes][:15]
    else:
        attr_filled = {}
        for attr in matrix.attributes:
            attr_filled[attr] = sum(1 for e in matrix.entities
                                       if matrix.cell(e.bank_slug, attr) is not None)
        top_attrs = sorted(attr_filled.keys(), key=lambda a: -attr_filled[a])[:15]

    lines = ["## 📋 Сравнительная таблица core-атрибутов", ""]
    header = "| Параметр | " + " | ".join(e.bank_name for e in matrix.entities) + " |"
    sep    = "|---" + ("|---" * len(matrix.entities)) + "|"
    lines.append(header)
    lines.append(sep)
    for attr in top_attrs:
        row_label = attr.replace("_", " ").capitalize()
        row_cells = []
        for e in matrix.entities:
            t = matrix.cell(e.bank_slug, attr)
            if t is None:
                row_cells.append("⚠ Не раскрыто")
                continue
            val = f"{t.value} {t.unit}".strip()
            cite = f" [{t.source_idx}]" if getattr(t, "source_idx", 0) else ""
            row_cells.append(f"{val}{cite}")
        lines.append(f"| {row_label} | " + " | ".join(row_cells) + " |")
    return "\n".join(lines)


def _methodology_md(matrix: Matrix, entities: list[Entity], facts: list[Fact],
                      sources_index: list[dict],
                      core_schema: list[CoreAttr] | None) -> str:
    """Детерминированный раздел «Методология и охват» — делает отчёт аудит-grade.

    Показывает per-bank охват (источники/факты/покрытие core), микс источников
    и ЯВНЫЕ ограничения (дисбаланс данных, банки только из агрегаторов).
    Это честно вскрывает, где данных мало, а не маскирует пробелы.
    """
    core_names = [a.name for a in (core_schema or [])]
    n_core = len(core_names)
    lines = ["## 🔍 Методология и охват данных", ""]
    lines.append(
        "**Метод:** факты извлечены из открытых источников и привязаны к цитате "
        "[N]; числовые значения сверены с текстом источника (анти-галлюцинация); "
        "характеристики, не подтверждённые источником, помечены «⚠ Не раскрыто». "
        "Покрытие core-схемы = доля ключевых параметров продукта, по которым "
        "найдено подтверждённое значение.")
    lines.append("")

    # Факты и источники по банкам
    facts_by_bank: dict[str, list[Fact]] = {}
    for f in facts:
        facts_by_bank.setdefault(f.entity_bank_slug, []).append(f)
    src_by_bank: dict[str, list[dict]] = {}
    for s in sources_index:
        src_by_bank.setdefault(s.get("bank_slug"), []).append(s)

    lines.append("**Охват по банкам:**")
    lines.append("")
    lines.append("| Банк | Источников | Фактов | high | Core покрыто | Офиц. сайт |")
    lines.append("|---|---|---|---|---|---|")
    coverages: dict[str, int] = {}
    fact_counts: dict[str, int] = {}
    no_official: list[str] = []
    for e in entities:
        bf = facts_by_bank.get(e.bank_slug, [])
        bf_real = [f for f in bf if f.attribute != "продукт_доступен"]
        n_high = sum(1 for f in bf_real if f.audit_priority == "high")
        ncov = sum(1 for a in core_names
                    if matrix.cell(e.bank_slug, a) is not None) if n_core else 0
        cov_pct = round(100 * ncov / n_core) if n_core else 0
        coverages[e.bank_slug] = cov_pct
        fact_counts[e.bank_slug] = len(bf_real)
        bsrc = src_by_bank.get(e.bank_slug, [])
        dom = (e.bank_domain or "").replace("www.", "")
        has_official = bool(dom) and any(dom in (s.get("domain") or "") for s in bsrc)
        if not has_official and dom:
            no_official.append(e.bank_name)
        lines.append(f"| {e.bank_name} | {len(bsrc)} | {len(bf_real)} | {n_high} | "
                      f"{ncov}/{n_core} | {'да' if has_official else '— агрегаторы'} |")
    lines.append("")

    # Микс источников — регуляторные по ДОМЕНУ (не по trust: банк-сайты тоже high-trust)
    try:
        from .topic_classifier import REGULATORY_DOMAIN_CATALOG as _REG
    except Exception:
        _REG = {}
    bank_domains = {(e.bank_domain or "").replace("www.", "") for e in entities if e.bank_domain}
    def _dom(s): return (s.get("domain") or "").replace("www.", "")
    n_reg = sum(1 for s in sources_index
                  if _dom(s) in _REG or _dom(s).endswith(".fas.gov.ru"))
    n_official = sum(1 for s in sources_index
                       if any(bd and bd in _dom(s) for bd in bank_domains))
    n_pdf = sum(1 for s in sources_index
                  if (s.get("url") or "").lower().split("?")[0].endswith(".pdf"))
    n_other = len(sources_index) - n_reg - n_official - n_pdf
    lines.append(
        f"**Источники:** всего {len(sources_index)} "
        f"(офиц. сайты банков: {n_official}, PDF-документы: {n_pdf}, "
        f"регуляторные/НПА: {n_reg}, агрегаторы/пресса: {max(0, n_other)}).")
    lines.append("")

    # Ограничения — честно вскрываем слабые места
    limitations: list[str] = []
    if fact_counts:
        mx = max(fact_counts.values()) or 1
        weak = [next(e.bank_name for e in entities if e.bank_slug == b)
                 for b, c in fact_counts.items() if c < mx * 0.4]
        if weak and len(weak) < len(entities):
            limitations.append(
                f"**Неравномерный охват:** по банкам {', '.join(weak)} найдено "
                f"существенно меньше данных, чем по остальным. Меньшее число фактов "
                f"означает меньшую доступность информации в открытых источниках, "
                f"а НЕ отсутствие у банка соответствующих условий — требуется "
                f"запрос тарифной документации напрямую.")
    if no_official:
        limitations.append(
            f"**Только агрегаторы:** по банкам {', '.join(no_official)} официальный "
            f"сайт не попал в выборку (недоступен/не проиндексирован) — данные взяты "
            f"из агрегаторов и прессы; рекомендуется сверка с первоисточником банка.")
    # Не раскрытые core-параметры
    if n_core:
        undisclosed: list[str] = []
        for a in core_names:
            missing = [e.bank_name for e in entities
                        if matrix.cell(e.bank_slug, a) is None]
            if len(missing) == len(entities):
                undisclosed.append(a.replace("_", " "))
        if undisclosed:
            limitations.append(
                f"**Не раскрыто ни одним банком:** {', '.join(undisclosed[:10])} — "
                f"эти параметры отсутствуют в открытых источниках у всех "
                f"сравниваемых банков.")
    limitations.append(
        "**Актуальность:** данные собраны из публичных источников на дату "
        "формирования отчёта; тарифы банков меняются — перед использованием "
        "в аудите сверьте ключевые цифры с действующими тарифными документами.")

    lines.append("**Ограничения и оговорки:**")
    lines.append("")
    for lim in limitations:
        lines.append(f"- {lim}")
    return "\n".join(lines)


def _sources_md(sources_index: list[dict]) -> str:
    """Список источников."""
    if not sources_index:
        return ""
    lines = [f"## 📚 Источники ({len(sources_index)})", ""]
    for s in sources_index:
        n = s.get("n", "?")
        title = (s.get("title") or s.get("url", ""))[:90]
        ts = s.get("trust_score", 0)
        domain = s.get("domain", "")
        trust = "●●●" if ts >= 0.9 else "●●○" if ts >= 0.6 else "○○○"
        lines.append(f"{n}. [{title}]({s.get('url')}) — _{domain}_ {trust}")
    return "\n".join(lines)


def _conflicts_md(matrix: Matrix) -> str:
    """Детерминированный раздел «Расхождения в источниках».

    Аудит-критично: когда источники дают РАЗНЫЕ значения одного параметра,
    это должно быть видно ВСЕГДА (а не по усмотрению outline-планировщика).
    Показывает банк, параметр и конфликтующие значения с их источниками."""
    conflicts = getattr(matrix, "conflicts", None) or {}
    if not conflicts:
        return ""
    name_by_slug = {e.bank_slug: e.bank_name for e in matrix.entities}
    lines = ["## ⚠️ Расхождения в источниках", ""]
    lines.append("По следующим параметрам источники дают **разные значения** — "
                  "аудитору необходимо сверить с первоисточником банка:")
    lines.append("")
    for (bank, attr), group in list(conflicts.items())[:12]:
        bank_name = name_by_slug.get(bank, bank)
        variants = []
        seen = set()
        for t in group:
            val = f"{t.value} {t.unit}".strip()
            if val in seen:
                continue
            seen.add(val)
            cite = f" [{t.source_idx}]" if getattr(t, "source_idx", 0) else ""
            variants.append(f"{val}{cite}")
        lines.append(f"- **{bank_name} — {attr.replace('_', ' ')}**: "
                      + " ↔ ".join(variants))
    return "\n".join(lines)


_CRITIC_SYSTEM = """Ты — придирчивый рецензент аудиторских отчётов. Тебе дают
ВОПРОС аудитора и ЧЕРНОВИК аналитических секций. Оцени КАЧЕСТВО АНАЛИЗА (не стиль):
  1) отвечает ли отчёт на вопрос;
  2) есть ли НАСТОЯЩАЯ аналитика (почему, что это значит, сравнение банков
     относительно друг друга, витрина↔реальность) — или это просто пересказ фактов;
  3) есть ли голословные сильные выводы без опоры;
  4) есть ли пустые/водянистые места и повторы.

ВЫХОД строго JSON без преамбулы:
{"ok": true|false,
 "issues": ["короткие конкретные претензии"],
 "key_findings_fix": "если ключевые выводы слабы/поверхностны — короткая инструкция, что усилить; иначе пустая строка"}"""


async def _critique_and_repair(ctx, results, question: str):
    """Критик черновика + одна перегенерация key_findings при слабости (#5)."""
    import os as _os
    from .narrative_generators import key_findings as _kf
    # Собираем черновик только из LLM-секций (детерминированные пропускаем)
    skip = {"comparison_table"}
    draft = "\n\n".join(md for sec, md in results
                          if md and md.strip() and sec.kind not in skip)
    if len(draft) < 400:
        return results
    model = _os.getenv("LLM_MODEL_REASONING") or _os.getenv("LLM_MODEL_SMART") or \
              _os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
    user = (f"# ВОПРОС\n{question}\n\n# ЧЕРНОВИК ОТЧЁТА\n{draft[:14000]}\n\n"
            f"Оцени качество анализа. JSON.")
    try:
        resp = await asyncio.wait_for(
            ctx.client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _CRITIC_SYSTEM},
                          {"role": "user", "content": user}],
                max_tokens=1500, temperature=0.0,  # дефолтный effort: чистый JSON
            ), timeout=90)
    except Exception as e:
        log.warning("[critic] failed: %s", e)
        return results
    from .narrative_generators.base import parse_json_object
    data = parse_json_object(resp.choices[0].message.content or "") or {}
    issues = data.get("issues") or []
    fix = str(data.get("key_findings_fix") or "").strip()
    log.warning("[critic] ok=%s, issues=%d, kf_fix=%s",
                 data.get("ok"), len(issues) if isinstance(issues, list) else 0,
                 bool(fix))
    if data.get("ok") is True or not fix:
        return results
    # есть ли секция key_findings для починки
    if not any(sec.kind == "key_findings" for sec, _ in results):
        return results
    # Инъекция критики в директиву меморандума и перегенерация key_findings
    try:
        from .research_brief import ResearchBrief
        if ctx.brief is None:
            ctx.brief = ResearchBrief()
        prev = ctx.brief.section_directives.get("key_findings", "")
        ctx.brief.section_directives["key_findings"] = (
            prev + " | ИСПРАВЬ ПО ЗАМЕЧАНИЯМ КРИТИКА: " + fix
            + (" Проблемы: " + "; ".join(map(str, issues[:5])) if issues else ""))
        new_md = await _kf.generate(ctx)
        if new_md and len(new_md) > 200:
            results = [((sec, new_md) if sec.kind == "key_findings" else (sec, md))
                        for sec, md in results]
            log.warning("[critic] key_findings перегенерирован по критике")
    except Exception as e:
        log.warning("[critic] repair failed: %s", e)
    return results


async def render_narrative_report(
    client: AsyncOpenAI,
    model: str,
    question: str,
    entities: list[Entity],
    facts: list[Fact],
    matrix: Matrix,
    sources_index: list[dict],
    core_schema: list[CoreAttr] | None = None,
    has_regulatory: bool = False,
    topic_profile = None,
    brief = None,
) -> tuple[list[Section], str]:
    """Главная функция: возвращает (sections_used, final_markdown).

    Пайплайн:
      1. plan_sections() — LLM выбирает структуру
      2. Параллельно генерируем тексты для каждой секции (с research_brief)
      3. Критик/repair, сборка финального markdown
    """
    ctx = NarrativeContext(
        client=client, model=model,
        question=question,
        entities=entities, facts=facts, sources_index=sources_index,
        has_regulatory=has_regulatory,
        brief=brief,
    )

    # 1) Outline planning
    # Если topic_profile определил applicable_section_kinds — используем как hint
    suggested_kinds = (topic_profile.applicable_section_kinds
                         if topic_profile else None)
    try:
        sections = await plan_sections(
            client, question,
            core_schema or [],
            facts,
            has_regulatory_sources=has_regulatory,
            suggested_kinds=suggested_kinds,
        )
    except Exception as e:
        log.warning("[narrative_renderer] outline failed: %s — minimal fallback", e)
        from .outline_planner import _default_outline
        sections = _default_outline(facts)
        # Если есть suggested_kinds — добавляем недостающие
        if suggested_kinds:
            existing = {s.kind for s in sections}
            for kind in suggested_kinds:
                if kind not in existing and kind in SECTION_GENERATORS:
                    sections.append(Section(
                        kind=kind, title=kind.replace("_", " ").capitalize(),
                        focus="", audit_relevance="",
                    ))

    log.warning("[narrative_renderer] outline = %s", [s.kind for s in sections])

    # 2) Параллельная генерация секций
    core_attr_names = [a.name for a in (core_schema or [])]
    gen_tasks = []
    for sec in sections:
        gen = SECTION_GENERATORS.get(sec.kind)
        if gen is None and sec.kind != "comparison_table" and sec.kind != "conflicts_explained":
            log.warning("[narrative_renderer] no generator for %s", sec.kind)
            gen_tasks.append((sec, None))
            continue
        gen_tasks.append((sec, gen))

    async def _run_section(sec: Section, gen_fn) -> tuple[Section, str]:
        # comparison_table — детерминированно из matrix
        if sec.kind == "comparison_table":
            return sec, _comparison_table_md(matrix)
        # conflicts_explained — отдельная сигнатура
        if sec.kind == "conflicts_explained":
            if not matrix.conflicts:
                return sec, ""
            md = await conflict_explainer.generate(ctx, matrix.conflicts)
            return sec, md
        if gen_fn is None:
            return sec, ""
        # risks_recommendations — особый kwarg
        try:
            if sec.kind == "risks_recommendations":
                gaps = matrix.null_cells()
                md = await gen_fn(ctx, gaps=gaps, conflicts=matrix.conflicts)
            elif sec.kind == "per_entity_breakdown":
                md = await gen_fn(ctx, core_attrs=core_attr_names)
            else:
                md = await gen_fn(ctx)
        except Exception as e:
            log.warning("[narrative_renderer] section %s failed: %s", sec.kind, e)
            md = ""
        return sec, md

    results = await asyncio.gather(*[_run_section(s, g) for s, g in gen_tasks],
                                       return_exceptions=False)

    # 2.5) КРИТИК/REPAIR (#5): reasoning-вызов проверяет черновик секций —
    # отвечает ли на вопрос, есть ли «почему/что значит», нет ли голословных
    # выводов и повторов. При проблемах — перегенерация key_findings с критикой.
    try:
        results = await _critique_and_repair(ctx, results, question)
    except Exception as e:
        log.warning("[narrative_renderer] critic/repair failed: %s", e)

    # 3) Сборка финального markdown
    parts = [f"# Аудит-отчёт: {question}", ""]

    # Header-summary (детерминированный)
    n_banks = len(entities)
    n_attrs = len(matrix.attributes)
    cov_pct = round(matrix.coverage * 100)
    n_facts = len(facts)
    n_high = sum(1 for f in facts if f.audit_priority == "high")
    parts.append(
        f"_Сравнение **{n_banks} банков** по **{n_attrs}** параметрам — "
        f"всего **{n_facts}** фактов извлечено ({n_high} приоритет high), "
        f"покрытие core-схемы **{cov_pct}%**._"
    )
    parts.append("")

    # Per-section markdown
    used_sections = []
    for sec, md in results:
        if md and md.strip():
            parts.append(md)
            parts.append("")
            used_sections.append(sec)

    # Расхождения в источниках — всегда при наличии конфликтов (audit-критично),
    # если LLM-секция conflicts_explained не была выбрана outline-планировщиком.
    if getattr(matrix, "conflicts", None) and \
       not any(s.kind == "conflicts_explained" for s in used_sections):
        cmd = _conflicts_md(matrix)
        if cmd:
            parts.append(cmd)
            parts.append("")

    # Методология и охват — всегда (audit-grade прозрачность)
    try:
        parts.append(_methodology_md(matrix, entities, facts, sources_index, core_schema))
        parts.append("")
    except Exception as e:
        log.warning("[narrative_renderer] methodology failed: %s", e)

    # Source list — всегда в конце
    parts.append(_sources_md(sources_index))

    return used_sections, "\n".join(parts).rstrip()


def extract_chart_specs(matrix: Matrix, max_charts: int = 3) -> list[dict]:
    """Из Matrix → chart specs для Chart.js (numerical attrs с variance > 0)."""
    if not matrix.entities or len(matrix.entities) < 2:
        return []

    chartable = []
    for attr, var_score in matrix.variance:
        if var_score <= 0:
            continue
        numeric_cells = [matrix.cell(e.bank_slug, attr) for e in matrix.entities]
        numeric_values = [(e.bank_name, c.value_numeric)
                            for e, c in zip(matrix.entities, numeric_cells)
                            if c is not None and c.value_numeric is not None]
        if len(numeric_values) < 2:
            continue
        chartable.append((attr, var_score))

    chartable = chartable[:max_charts]
    out = []
    for attr, _ in chartable:
        labels, values, sources_used, unit_seen = [], [], [], ""
        for e in matrix.entities:
            t = matrix.cell(e.bank_slug, attr)
            if t and t.value_numeric is not None:
                labels.append(e.bank_name)
                values.append(t.value_numeric)
                if t.source_idx and t.source_idx not in sources_used:
                    sources_used.append(t.source_idx)
                unit_seen = unit_seen or t.unit
            else:
                labels.append(e.bank_name)
                values.append(None)
        if all(v is None for v in values):
            continue
        title = attr.replace("_", " ").capitalize()
        if unit_seen:
            title += f", {unit_seen}"
        out.append({
            "title": title,
            "chartType": "bar",
            "labels": labels,
            "datasets": [{"label": attr.replace("_", " "), "data": values}],
            "sourceCitations": sources_used,
        })
    log.warning("[narrative_renderer] %s chart specs", len(out))
    return out
