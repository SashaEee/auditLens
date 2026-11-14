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
         ("write", "Письмо и карточки"), ("verify", "Сверка чисел"),
         ("deck", "Презентация")]


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


# ── Сколько обычно длится отчёт — для оценки «осталось» в ходе прогона ───────
# Этапы — те, что видит человек в консоли хода отчёта (фазы потока). Запасные
# значения — замер живых отчётов 11.11.2026; как только накопятся свои отчёты
# с разбивкой по шагам, берётся их медиана.
PHASES = ("planning", "research", "extraction", "writing", "verification")
_TYPICAL_DEFAULT = {"planning": 15, "research": 60, "extraction": 55, "writing": 125,
                    "verification": 8}
_PHASE_STEPS = {"planning": ("plan",), "research": ("collect",),
                "extraction": ("facts", "critic", "lead"), "writing": ("write",),
                "verification": ("verify",)}
_TYPICAL_TTL = 1800.0
_typical_cache: dict = {"at": 0.0, "val": None}


def typical_from(summary: dict | None, *, min_reports: int = 5) -> dict:
    """Медианы шагов → секунды по этапам консоли. Мало отчётов — запасные значения."""
    out = dict(_TYPICAL_DEFAULT)
    if not summary or (summary.get("timed") or 0) < min_reports:
        return {"steps": out, "total": sum(out.values()), "reports": 0}
    med = {s["key"]: float(s.get("median_s") or 0) for s in summary.get("steps") or []}
    for phase, keys in _PHASE_STEPS.items():
        sec = sum(med.get(k, 0.0) for k in keys)
        if sec > 0:
            out[phase] = int(min(max(sec, 3), 900))
    return {"steps": out, "total": sum(out.values()), "reports": int(summary.get("timed") or 0)}


def typical(days: int = 30) -> dict:
    """Обычная длительность этапов отчёта (кэш на полчаса). Никогда не бросает."""
    import time
    now = time.monotonic()
    if _typical_cache["val"] is not None and now - _typical_cache["at"] < _TYPICAL_TTL:
        return _typical_cache["val"]
    try:
        val = typical_from(steps(days))
    except Exception:  # noqa: BLE001 — нет базы: оценка по запасным значениям
        val = typical_from(None)
    _typical_cache.update(at=now, val=val)
    return val


# ── Сколько обычно длится короткий ответ и презентация ───────────────────────
# Для строки хода работы: «обычно около 25 с», «осталось примерно 2 мин». Берётся
# из сохранённых ответов (chat_message.meta: mode, engine, elapsed_s, status).
# Запасные значения — замеры 11–12.11.2026; своих данных нужно не меньше восьми.
_ANSWER_DEFAULT = {"answer": {"p50": 30, "p75": 60, "n": 0},
                   "deck": {"p50": 140, "p75": 190, "n": 0}}
_answer_cache: dict = {"at": 0.0, "val": None}


def quantile(xs: list[float], q: float) -> float | None:
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def typical_answer_from(rows: list[tuple[str, float]] | None, *, min_rows: int = 8) -> dict:
    """rows: [(движок, секунды)] → {"answer": {p50, p75, n}, "deck": {…}}. Презентация —
    движок deck; всё остальное — короткий ответ. Мало данных — запасные значения."""
    out = {k: dict(v) for k, v in _ANSWER_DEFAULT.items()}
    groups: dict[str, list[float]] = {"answer": [], "deck": []}
    for engine, sec in rows or []:
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            continue
        if 1 <= sec <= 1800:
            groups["deck" if engine == "deck" else "answer"].append(sec)
    for key, xs in groups.items():
        if len(xs) >= min_rows:
            out[key] = {"p50": int(round(quantile(xs, 0.5))), "p75": int(round(quantile(xs, 0.75))),
                        "n": len(xs)}
    return out


def typical_answer(days: int = 30) -> dict:
    """Обычная длительность короткого ответа и презентации (кэш на полчаса). Никогда не бросает."""
    import time
    now = time.monotonic()
    if _answer_cache["val"] is not None and now - _answer_cache["at"] < _TYPICAL_TTL:
        return _answer_cache["val"]
    try:
        with db.session() as s:
            s.execute(text("SET LOCAL statement_timeout = '4s'"))
            rows = [(r[0] or "", r[1]) for r in s.execute(text("""
                SELECT meta->>'engine', (meta->>'elapsed_s')::numeric
                  FROM chat_message
                 WHERE role = 'assistant'
                   AND created_at > now() - make_interval(days => :d)
                   AND meta->>'mode' = 'quick'
                   AND coalesce(meta->>'status', 'ok') = 'ok'
                   AND meta->>'elapsed_s' ~ '^[0-9]+(\\.[0-9]+)?$'"""), {"d": days})]
        val = typical_answer_from(rows)
    except Exception:  # noqa: BLE001 — нет базы: оценка по запасным значениям
        val = typical_answer_from(None)
    _answer_cache.update(at=now, val=val)
    return val
