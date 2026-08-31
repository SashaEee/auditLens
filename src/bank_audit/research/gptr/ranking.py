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
    closed: int             # закрытых характеристик
    total: int              # всего характеристик в контракте
    declared: int           # фактов со слов самой организации
    observed: int           # фактов со стороны
    regulatory: int         # фактов-норм

    @property
    def share(self) -> float:
        return (self.closed / self.total) if self.total else 0.0

    def to_ui(self) -> dict:
        return {"subject": self.subject, "label": self.label,
                "closed": self.closed, "total": self.total,
                "share": round(self.share, 3), "declared": self.declared,
                "observed": self.observed, "regulatory": self.regulatory}


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
        facts = [f for f in registry.facts if f.subject == s]
        rows.append(RankRow(
            subject=s, label=labels.get(s, s),
            closed=sum(1 for a in attrs if cells.get((s, a))),
            total=len(attrs),
            declared=sum(1 for f in facts if f.stance == "declared"),
            observed=sum(1 for f in facts if f.stance == "observed"),
            regulatory=sum(1 for f in facts if f.stance == "regulatory"),
        ))
    rows.sort(key=lambda r: (-r.closed, -r.observed, r.label))
    return rows


def render(rows: list[RankRow]) -> str:
    """Таблица для контекста писателя. Пустой список — раздела не будет."""
    if not rows:
        return ""
    lines = [
        "ПОЛНОТА РАСКРЫТИЯ (посчитано по фактам, не оценочно). Столбцы: "
        "закрыто характеристик из контракта; фактов заявленных / со стороны / "
        "норм регулятора.",
        "",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(f"  {i}. {r.label}: {r.closed} из {r.total} "
                     f"({r.share:.0%}) | заявлено {r.declared}, "
                     f"со стороны {r.observed}, норм {r.regulatory}")
    return "\n".join(lines)
