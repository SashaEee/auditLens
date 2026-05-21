"""Matrix Renderer — превращает структурированную Matrix в markdown отчёт.

Детерминированный рендер: ноль LLM, никаких галлюцинаций. Структура отчёта:
  1. Краткое резюме (количество банков, атрибутов, coverage)
  2. Сравнительная таблица entities × top attributes
  3. Per-bank секции с deep-dive
  4. Конфликты и расхождения
  5. Пробелы (null cells) и рекомендации аудитору
  6. Source list

Также формирует Chart specs из numerical атрибутов с высокой variance.
"""
from __future__ import annotations
import logging
import re
from typing import Any

from .matrix_builder import Matrix
from .entity_extractor import Entity
from .triple_extractor import Triple

log = logging.getLogger(__name__)


# Категория атрибута по эмодзи — для красивого отображения
def _attr_emoji(attr: str) -> str:
    a = attr.lower()
    if any(k in a for k in ("ставк", "процент", "%")): return "💹"
    if any(k in a for k in ("комисси", "плат", "тариф", "₽")): return "💵"
    if any(k in a for k in ("лимит", "максимум", "минимум")): return "📊"
    if any(k in a for k in ("срок", "период", "лет", "дней")): return "🕐"
    if any(k in a for k in ("документ", "требован", "паспорт")): return "📋"
    if any(k in a for k in ("кешбэк", "бонус", "процент_на_остаток")): return "🎁"
    if any(k in a for k in ("онлайн", "приложен", "дистанц")): return "📱"
    return "•"


def _humanize_attr(attr: str) -> str:
    """snake_case → Человекочитаемая фраза."""
    return attr.replace("_", " ").capitalize()


def _format_value(t: Triple) -> str:
    """Triple → строка для таблицы."""
    if t is None:
        return "⚠ Не раскрыто"
    val = t.value
    unit = t.unit or ""
    return f"{val} {unit}".strip()


def _render_summary(matrix: Matrix, question: str) -> str:
    """Краткое резюме отчёта."""
    n_banks = len(matrix.entities)
    n_attrs = len(matrix.attributes)
    coverage_pct = round(matrix.coverage * 100)
    n_conflicts = len(matrix.conflicts)
    lines = [
        "## 📊 Краткое резюме",
        "",
        f"Сравнение **{n_banks} банков** по **{n_attrs} ключевым параметрам**. "
        f"Полнота данных: **{coverage_pct}%** "
        f"({sum(1 for v in matrix.cells.values() if v)} из {n_banks * n_attrs} ячеек).",
    ]
    if n_conflicts:
        lines.append(f"⚠ Найдено **{n_conflicts} расхождений** в источниках — см. раздел «Конфликты».")
    if matrix.variance:
        top_var = [a for a, _ in matrix.variance[:3] if _ > 0]
        if top_var:
            lines.append("")
            lines.append("**Где банки больше всего расходятся:** " +
                          ", ".join(f"_{_humanize_attr(a)}_" for a in top_var) + ".")
    return "\n".join(lines)


def _render_comparison_table(matrix: Matrix, max_attrs: int = 12) -> str:
    """Главная таблица: банки × top attributes по покрытию."""
    if not matrix.entities or not matrix.attributes:
        return "_Нет данных для сравнения._"
    # Берём top-N attribute'ов по числу заполненных клеток
    attr_filled: dict[str, int] = {}
    for attr in matrix.attributes:
        attr_filled[attr] = sum(1 for e in matrix.entities
                                   if matrix.cell(e.bank_slug, attr) is not None)
    top_attrs = sorted(attr_filled.keys(), key=lambda a: -attr_filled[a])[:max_attrs]

    lines = ["## 📋 Сравнительная таблица", ""]
    header = "| Параметр | " + " | ".join(e.bank_name for e in matrix.entities) + " |"
    sep    = "|---" + ("|---" * len(matrix.entities)) + "|"
    lines.append(header)
    lines.append(sep)
    for attr in top_attrs:
        emoji = _attr_emoji(attr)
        row_label = f"{emoji} {_humanize_attr(attr)}"
        row_cells = []
        for e in matrix.entities:
            t = matrix.cell(e.bank_slug, attr)
            if t:
                cite = f" [{t.source_idx}]" if t.source_idx else ""
                row_cells.append(f"{_format_value(t)}{cite}")
            else:
                row_cells.append("⚠ Не раскрыто")
        lines.append(f"| {row_label} | " + " | ".join(row_cells) + " |")
    return "\n".join(lines)


def _render_per_bank_sections(matrix: Matrix) -> str:
    """Детальный per-bank раздел: все факты сгруппированные по банку."""
    if not matrix.entities:
        return ""
    lines = ["", "## 🏦 Детально по каждому банку", ""]
    for e in matrix.entities:
        # Подсчёт фактов на банк
        facts = [(attr, matrix.cell(e.bank_slug, attr)) for attr in matrix.attributes]
        filled = [(a, t) for a, t in facts if t is not None]
        nulls  = [a for a, t in facts if t is None]
        lines.append(f"### {e.bank_name} ({e.bank_slug})")
        lines.append(f"_Продукт: {e.product}_")
        lines.append("")
        if filled:
            lines.append(f"**Установленные параметры ({len(filled)}):**")
            for attr, t in filled:
                emoji = _attr_emoji(attr)
                cite = f" [{t.source_idx}]" if t.source_idx else ""
                lines.append(f"- {emoji} **{_humanize_attr(attr)}**: {_format_value(t)}{cite}")
                if t.excerpt:
                    # Сокращённая цитата
                    ex = re.sub(r"\s+", " ", t.excerpt)[:200]
                    lines.append(f"  > _{ex}…_" if len(t.excerpt) > 200 else f"  > _{ex}_")
        if nulls and len(nulls) <= 8:
            lines.append("")
            lines.append(f"**⚠ Не раскрыто ({len(nulls)}):** " +
                          ", ".join(_humanize_attr(a).lower() for a in nulls))
        elif nulls:
            lines.append("")
            lines.append(f"**⚠ Не раскрыто:** {len(nulls)} параметров — требуется ручная проверка")
        lines.append("")
    return "\n".join(lines)


def _render_conflicts(matrix: Matrix) -> str:
    """Если есть конфликтующие триплы — отдельная секция."""
    if not matrix.conflicts:
        return ""
    lines = ["", "## ⚠ Расхождения в источниках", ""]
    for (bank, attr), group in matrix.conflicts.items():
        bank_name = next((e.bank_name for e in matrix.entities
                            if e.bank_slug == bank), bank)
        lines.append(f"**{bank_name} — {_humanize_attr(attr)}:**")
        for t in group:
            cite = f" [{t.source_idx}]" if t.source_idx else ""
            lines.append(f"- {_format_value(t)}{cite} (confidence: {t.confidence})")
        lines.append("")
    return "\n".join(lines)


def _render_gaps_and_recommendations(matrix: Matrix) -> str:
    """Пробелы и рекомендации аудитору."""
    lines = ["", "## 📌 Рекомендации аудитору", ""]
    nulls = matrix.null_cells()
    if not nulls:
        lines.append("✅ Все ключевые параметры раскрыты — отчёт полный.")
        return "\n".join(lines)
    # Группируем nulls по attribute
    by_attr: dict[str, list[str]] = {}
    for bank, attr in nulls:
        by_attr.setdefault(attr, []).append(bank)
    lines.append(f"Полнота данных: **{round(matrix.coverage * 100)}%**. "
                  f"Требуется уточнение по следующим параметрам:")
    lines.append("")
    for attr, banks in sorted(by_attr.items()):
        if len(banks) == len(matrix.entities):
            lines.append(f"- **{_humanize_attr(attr)}** — у ВСЕХ банков; запросить актуальные тарифные документы")
        else:
            bank_names = [next((e.bank_name for e in matrix.entities
                                  if e.bank_slug == b), b) for b in banks]
            lines.append(f"- **{_humanize_attr(attr)}** — у {', '.join(bank_names)}")
    return "\n".join(lines)


def _render_sources(matrix: Matrix) -> str:
    """Список источников с trust-индикаторами."""
    if not matrix.sources:
        return ""
    lines = ["", f"## 📚 Источники ({len(matrix.sources)})", ""]
    for s in matrix.sources:
        n = s.get("n", "?")
        title = s.get("title") or s.get("url", "")
        ts = s.get("trust_score") or 0
        domain = s.get("domain", "")
        trust_marker = "●●●" if ts >= 0.9 else "●●○" if ts >= 0.6 else "○○○"
        lines.append(f"{n}. [{title[:80]}]({s.get('url')}) — {domain} {trust_marker}")
    return "\n".join(lines)


def render_report(matrix: Matrix, question: str) -> str:
    """Главная: матрица → полный markdown отчёт."""
    return "\n\n".join(filter(None, [
        f"# Аудит-отчёт: {question}",
        _render_summary(matrix, question),
        _render_comparison_table(matrix),
        _render_per_bank_sections(matrix),
        _render_conflicts(matrix),
        _render_gaps_and_recommendations(matrix),
    ]))


def extract_chart_specs(matrix: Matrix, max_charts: int = 3) -> list[dict]:
    """Из матрицы извлекает chart specs для Chart.js.

    Логика: для каждого numerical атрибута с заполненностью ≥2 банков и
    variance > 0 — отдельный bar-chart.
    """
    if not matrix.entities or len(matrix.entities) < 2:
        return []

    chartable: list[tuple[str, float]] = []
    for attr, var_score in matrix.variance:
        if var_score <= 0:
            continue
        # Проверяем что у ≥2 банков есть numeric value
        numeric_cells = [matrix.cell(e.bank_slug, attr) for e in matrix.entities]
        numeric_values = [(e.bank_name, c.value_numeric) for e, c in zip(matrix.entities, numeric_cells)
                          if c is not None and c.value_numeric is not None]
        if len(numeric_values) < 2:
            continue
        chartable.append((attr, var_score))

    chartable = chartable[:max_charts]
    out: list[dict] = []
    for attr, _ in chartable:
        # Подготавливаем chart spec
        labels = []
        values = []
        sources_used = []
        unit_seen = ""
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
                values.append(None)   # null показывает что данных нет
        if all(v is None for v in values):
            continue
        title = _humanize_attr(attr)
        if unit_seen:
            title += f", {unit_seen}"
        out.append({
            "title": title,
            "chartType": "bar",
            "labels": labels,
            "datasets": [{
                "label": _humanize_attr(attr),
                "data": values,
            }],
            "sourceCitations": sources_used,
        })
    log.warning("[matrix_renderer] extracted %s chart specs", len(out))
    return out
