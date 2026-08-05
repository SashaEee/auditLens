"""Ночной судья новостного выпуска — метрика качества отбора (этап 6).

Оценивает КАЖДУЮ опубликованную позицию строгой рубрикой (та же, что вскрыла
50 процентов мусора при аудите 05.08.2026) и пишет одну строку в
digest_news_judge. Судит ПУБЛИКАЦИЮ, а не пул: пул фильтруют квоты и триаж,
а аудитор видит только опубликованное. Один вызов модели в сутки.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from sqlalchemy import text

from .. import db
from ..ai.analyst import insight_model
from ..ai.llm_utils import _loose_json_loads
from ..clock import today_anchor
from . import store

log = logging.getLogger(__name__)

_JUDGE_SYSTEM = (
    "Ты — руководитель службы внутреннего аудита розницы Сбербанка, оцениваешь "
    "УЖЕ ОПУБЛИКОВАННЫЙ утренний выпуск новостей. По КАЖДОЙ позиции — score.\n"
    "РЕЛЕВАНТНО (6-10): санкции/предписания ЦБ; законы и нормативы по розничным "
    "операциям; ключевая ставка; сбои/утечки/хищения в банках; схемы мошенничества "
    "против клиентов; продуктовые действия конкурентов; суды по рознице; платёжная "
    "инфраструктура; события Сбера.\n"
    "НЕ РЕЛЕВАНТНО (0-3): политика, спорт, происшествия вне банков, пиар без "
    "продуктовой сути, потребсоветы, ежедневная рутина ЦБ, зарубежное без связи "
    "с РФ, УСТАРЕВШЕЕ (дата события старше выпуска на 2+ суток).\n"
    "ПОГРАНИЧНО (4-5): финансовый сектор без конкретики/связи с розницей.\n"
    'Верни СТРОГО JSON: {"verdicts":[{"n":1,"score":7}]} — по всем позициям.'
)


def _issue_items(day: date) -> list[dict]:
    secs = store._read_day_rows(day)
    nw = (secs.get("news") or {}).get("payload") or {}
    return [it for g in (nw.get("groups") or []) for it in (g.get("items") or [])]


async def judge_issue(day: date, *, force: bool = False) -> dict:
    """Оценка выпуска дня. Идемпотентна: строка уже есть → no-op (кроме force)."""
    with db.session() as s:
        if not force and s.execute(text(
                "SELECT 1 FROM digest_news_judge WHERE digest_date = :d"),
                {"d": day}).first():
            return {"ok": True, "skipped": "уже оценён"}
    items = _issue_items(day)
    if not items:
        return {"ok": False, "reason": "в выпуске нет новостей"}

    from .writer import _chat
    listing = "\n".join(
        f'#{i + 1} {it.get("title")} — {(it.get("summary") or "")[:160]} '
        f'({it.get("domain") or it.get("source")})'
        for i, it in enumerate(items))
    raw, ti, to = await _chat(insight_model(),
                              today_anchor() + "\n\n" + _JUDGE_SYSTEM,
                              f"Выпуск ({len(items)} позиций):\n{listing}",
                              max_tokens=1500, temperature=0.0)
    parsed = _loose_json_loads(raw)
    scores: dict[int, int] = {}
    for v in (parsed.get("verdicts") or []):
        try:
            n, sc = int(v.get("n")), max(0, min(10, int(v.get("score"))))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(items):
            scores[n] = sc
    if len(scores) < len(items) * 0.7:
        return {"ok": False, "reason": f"судья покрыл {len(scores)} из {len(items)}"}

    vals = list(scores.values())
    junk = sum(1 for s_ in vals if s_ <= 3)
    border = sum(1 for s_ in vals if 4 <= s_ <= 5)
    rel = sum(1 for s_ in vals if s_ >= 6)
    detail = [{"n": n, "score": sc, "title": (items[n - 1].get("title") or "")[:120],
               "url": items[n - 1].get("url")} for n, sc in sorted(scores.items())]
    row = {"d": day, "n": len(vals), "j": junk, "b": border, "r": rel,
           "avg": round(sum(vals) / len(vals), 2),
           "det": json.dumps(detail, ensure_ascii=False), "m": insight_model()}
    with db.session() as s:
        s.execute(text("""
            INSERT INTO digest_news_judge
                (digest_date, n_items, junk, borderline, relevant, avg_score,
                 detail, llm_model, generated_at)
            VALUES (:d, :n, :j, :b, :r, :avg, CAST(:det AS jsonb), :m, now())
            ON CONFLICT (digest_date) DO UPDATE SET
                n_items = EXCLUDED.n_items, junk = EXCLUDED.junk,
                borderline = EXCLUDED.borderline, relevant = EXCLUDED.relevant,
                avg_score = EXCLUDED.avg_score, detail = EXCLUDED.detail,
                llm_model = EXCLUDED.llm_model, generated_at = now()
        """), row)
    log.info("судья выпуска %s: %d позиций, мусор %d, погранично %d, средний %.1f",
             day, len(vals), junk, border, row["avg"])
    return {"ok": True, "n": len(vals), "junk": junk, "borderline": border,
            "relevant": rel, "avg": row["avg"], "tokens": (ti, to)}
