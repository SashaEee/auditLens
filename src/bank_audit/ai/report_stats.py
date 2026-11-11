"""Сводка по отчётам ИИ-помощника для «Пульса»: задачи, время шагов, обрывы.

Берётся из сохранённых отчётов (report.payload): `plan` — как понят вопрос,
`timings` — секунды по шагам, `elapsed_s` — весь прогон. Отчёты до 11.11.2026
этих полей не имеют и в разбивку по шагам не попадают.
"""
from __future__ import annotations

from sqlalchemy import text

from .. import db

TASKS = {"compare": "Сравнение с конкурентами", "product": "Всё о продукте", "complaints": "Жалобы",
         "regulation": "Нормы", "market_data": "Рынок и цифры", "explain": "Справка",
         "news": "Разбор новости", "check": "План проверки"}
STEPS = [("plan", "План"), ("collect", "Сбор источников"), ("facts", "Выписка фактов"),
         ("critic", "Проверка цитат"), ("lead", "Добор недостающего"),
         ("write", "Письмо и карточки"), ("verify", "Сверка чисел")]


def median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def summarize(rows: list[dict], days: int) -> dict:
    """rows: {task, elapsed_s, timings: {шаг: секунды}, broken: bool}."""
    by_task: dict[str, list[dict]] = {}
    for r in rows:
        by_task.setdefault(r.get("task") or "", []).append(r)
    tasks = [{"key": k, "label": TASKS.get(k, "Не определена" if not k else k), "n": len(v),
              "median_s": median([x.get("elapsed_s") for x in v]),
              "broken": sum(1 for x in v if x.get("broken"))}
             for k, v in sorted(by_task.items(), key=lambda kv: -len(kv[1]))]
    timed = [r for r in rows if r.get("timings")]
    steps = [{"key": k, "label": label,
              "median_s": median([float((r["timings"] or {}).get(k) or 0) for r in timed])}
             for k, label in STEPS] if timed else []
    return {"days": days, "reports": len(rows), "timed": len(timed),
            "median_s": median([r.get("elapsed_s") for r in rows]),
            "broken": sum(1 for r in rows if r.get("broken")),
            "tasks": tasks, "steps": steps}


def steps(days: int = 30) -> dict:
    with db.session() as s:
        s.execute(text("SET LOCAL statement_timeout = '8s'"))
        rows = [dict(r) for r in s.execute(text("""
            SELECT payload->'plan'->>'task' AS task,
                   (payload->>'elapsed_s')::numeric AS elapsed_s,
                   payload->'timings' AS timings,
                   (body LIKE '%Отчёт неполный%' OR body LIKE '%Раздел не дописан%'
                    OR coalesce(payload->>'status', 'ok') <> 'ok') AS broken
              FROM report
             WHERE created_at > now() - make_interval(days => :d)
               AND payload->>'mode' = 'deep'"""), {"d": days}).mappings()]
    for r in rows:
        r["elapsed_s"] = float(r["elapsed_s"]) if r["elapsed_s"] is not None else None
        r["timings"] = r["timings"] if isinstance(r["timings"], dict) else None
    return summarize(rows, days)
