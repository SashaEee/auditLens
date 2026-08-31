"""Ранжирование по полноте раскрытия — считается, а не сочиняется.

ЗАЧЕМ. В старом конвейере был раздел «Рейтинг» с обязательным объяснением
критерия; в новом он пропал вместе с типом `Ranking`, а флаг плана
`needs_ranking` не читался нигде. Просить писателя «проранжируй» бессмысленно:
он сочинит порядок, который нечем проверить.

Здесь ранг считается по контракту: сколько клеток матрицы «характеристика ×
сторона» объект закрыл. Это не оценка продукта, а измеримое свойство —
насколько полно объект раскрывает то, о чём спрашивает аудитор, и есть ли по
нему взгляд со стороны. Порядок воспроизводим и проверяем по фактам.

Содержательный ранг (кто выгоднее, где проще) остаётся за писателем, но уже
поверх этой таблицы и с обязательным указанием критерия.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class RankRow:
    subject: str
    label: str
    closed: int             # характеристик, закрытых заявленной стороной
    with_observed: int      # из них подтверждённых или оспоренных со стороны
    total: int              # всего характеристик в контракте

    @property
    def share(self) -> float:
        return (self.closed / self.total) if self.total else 0.0

    def to_ui(self) -> dict:
        return {"subject": self.subject, "label": self.label,
                "closed": self.closed, "with_observed": self.with_observed,
                "total": self.total, "share": round(self.share, 3)}


def build(plan, registry, attributes) -> list[RankRow]:
    """Матрица покрытия по объектам, отсортированная по полноте раскрытия."""
    labels = dict(getattr(plan, "subject_labels", None) or {})
    subjects = list(getattr(plan, "subjects", None) or [])
    attrs = list(attributes)
    if not subjects or not attrs:
        return []
    cells = registry.by_cell()
    rows: list[RankRow] = []
    for s in subjects:
        closed = with_obs = 0
        for a in attrs:
            facts = cells.get((s, a)) or []
            if any(f.stance == "declared" for f in facts):
                closed += 1
            if any(f.stance == "observed" for f in facts):
                with_obs += 1
        rows.append(RankRow(subject=s, label=labels.get(s, s), closed=closed,
                            with_observed=with_obs, total=len(attrs)))
    # Сортируем по РАСКРЫТИЮ и его проверяемости со стороны, а не по числу
    # собранных фактов: иначе первым оказывается тот, про кого мы прочитали
    # больше страниц, что к объекту исследования отношения не имеет.
    rows.sort(key=lambda r: (-r.closed, -r.with_observed, r.label))
    return rows


def is_degenerate(rows: list[RankRow]) -> bool:
    """Все объекты неразличимы — тогда таблица мест бессмысленна и вредна."""
    if len(rows) < 2:
        return True
    first = (rows[0].closed, rows[0].with_observed)
    return all((r.closed, r.with_observed) == first for r in rows)


def render(rows: list[RankRow]) -> str:
    """Текст для контекста писателя.

    Если объекты неразличимы, места не присваиваем и прямо говорим об этом:
    ранг «ВТБ первый, Т-Банк второй» при одинаковом раскрытии — выдумка,
    основанная на том, сколько страниц про кого удалось прочитать.
    """
    if not rows:
        return ""
    if is_degenerate(rows):
        r = rows[0]
        return (
            "ПОЛНОТА РАСКРЫТИЯ: объекты НЕРАЗЛИЧИМЫ — каждый закрыл "
            f"{r.closed} из {r.total} характеристик, со стороны проверено "
            f"{r.with_observed}. Ранжировать по раскрытию нельзя: мест не "
            "присваивай, так и напиши, что по этому критерию различий нет. "
            "Если по существу вопроса уместен другой порядок — построй его "
            "сам, назови критерий и обоснуй фактами.")
    lines = ["ПОЛНОТА РАСКРЫТИЯ (посчитано по фактам). Закрыто характеристик "
             "заявленной стороной; из них проверено взглядом со стороны.", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"  {i}. {r.label}: раскрыто {r.closed} из {r.total} "
                     f"({r.share:.0%}), проверено со стороны {r.with_observed}")
    return "\n".join(lines)
