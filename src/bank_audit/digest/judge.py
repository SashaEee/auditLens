"""Ночной судья новостного выпуска — метрика качества отбора (этап 6).

Оценивает КАЖДУЮ опубликованную позицию строгой рубрикой (та же, что вскрыла
50 процентов мусора при аудите 05.08.2026) и пишет одну строку в
digest_news_judge. Судит ПУБЛИКАЦИЮ, а не пул: пул фильтруют квоты и триаж,
а аудитор видит только опубликованное. Один вызов модели в сутки.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

from sqlalchemy import text

from .. import db
from ..ai.llm_utils import _loose_json_loads
from ..clock import today_anchor
from . import store

log = logging.getLogger(__name__)

_JUDGE_MODEL = os.getenv("DIGEST_JUDGE_MODEL", "openai/gpt-5.4")

# Рубрика — «повод для проверки», а не «относится к банкам». Прежний судья
# повторял рубрику отбора (ключевая ставка и продукты конкурентов считались
# релевантными на 6–10), поэтому проверял отбор его же критериями: средний балл
# 7 и ноль мусора, пока руководство видело в заголовках ставки. Модель судьи —
# другого семейства, чем у редакции (Opus), чтобы не оценивать саму себя.
_JUDGE_SYSTEM = (
    "Ты — независимый рецензент утреннего брифинга службы внутреннего аудита "
    "РОЗНИЧНОГО бизнеса Сбера. Цель брифинга — дать аудитору повод для НОВОЙ ПРОВЕРКИ "
    "или корректировки текущей.\n"
    "Шкала value 0–10: 9–10 — прямой повод (действие ЦБ/суда/прокуратуры против банка "
    "за нарушение в рознице; новая схема мошенничества или утечка с механикой; сбой у "
    "банка; закон или норматив с датой вступления; событие в Сбере с риском; устойчивый "
    "всплеск жалоб клиентов Сбера); 6–8 — полезный контекст для выбора проверок; 3–5 — "
    "рыночный фон (ставки, тарифы, прогнозы, макро); 0–2 — не про розничный банковский "
    "бизнес или устаревшее.\n"
    "Оцени: headline_value 1–5 — насколько заголовок дня выбран верно (5 — это самое "
    "важное для аудита из всего переданного); каждую карточку (C) и новость (N) по "
    "шкале value; среди НЕОПУБЛИКОВАННЫХ (P) — те, что стоили публикации (value ≥ 8).\n"
    'Верни СТРОГО JSON: {"headline_value":4,"cards":[{"n":1,"value":8}],'
    '"news":[{"n":1,"value":7}],"missed":[{"n":3,"value":8,"why":"до 12 слов"}]}'
)


def _issue(day: date) -> tuple[dict, list[dict], list[dict]]:
    secs = store._read_day_rows(day)
    head = (secs.get("headline") or {}).get("payload") or {}
    nw = (secs.get("news") or {}).get("payload") or {}
    items = [it for g in (nw.get("groups") or []) for it in (g.get("items") or [])]
    pub = {it.get("url") for it in items}
    pool = [p for p in (nw.get("pool") or []) if p.get("url") not in pub][:25]
    return head, items, pool


async def judge_issue(day: date, *, force: bool = False) -> dict:
    """Оценка выпуска дня. Идемпотентна: строка уже есть → no-op (кроме force)."""
    with db.session() as s:
        if not force and s.execute(text(
                "SELECT 1 FROM digest_news_judge WHERE digest_date = :d"),
                {"d": day}).first():
            return {"ok": True, "skipped": "уже оценён"}
    head, items, pool = _issue(day)
    if not items and not (head.get("insights")):
        return {"ok": False, "reason": "в выпуске нет новостей"}

    from .writer import _chat
    cards = head.get("insights") or []
    listing = (f'ЗАГОЛОВОК: {head.get("headline")}\n'
               + "\n".join(f'C{i + 1} [{c.get("kind")}] {c.get("title")} — {(c.get("so_what") or "")[:160]}'
                           for i, c in enumerate(cards))
               + "\n" + "\n".join(f'N{i + 1} {it.get("title")} — {(it.get("summary") or "")[:160]} '
                                  f'({it.get("domain") or it.get("source")})' for i, it in enumerate(items))
               + "\n" + "\n".join(f'P{i + 1} {p.get("title")} — {(p.get("snippet") or "")[:160]}'
                                  for i, p in enumerate(pool)))
    raw, ti, to = await _chat(_JUDGE_MODEL, today_anchor() + "\n\n" + _JUDGE_SYSTEM,
                              f"Выпуск:\n{listing}", max_tokens=2500, temperature=0.0)
    parsed = _loose_json_loads(raw)

    def _vals(key: str, n_max: int) -> dict[int, int]:
        out = {}
        for v in (parsed.get(key) or []):
            try:
                n, sc = int(v.get("n")), max(0, min(10, int(v.get("value"))))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= n_max:
                out[n] = sc
        return out

    news_v = _vals("news", len(items))
    card_v = _vals("cards", len(cards))
    vals = list(news_v.values())
    if items and len(news_v) < len(items) * 0.7:
        return {"ok": False, "reason": f"судья покрыл {len(news_v)} из {len(items)}"}
    missed = [{"title": (pool[int(m["n"]) - 1].get("title") or "")[:140], "url": pool[int(m["n"]) - 1].get("url"),
               "value": m.get("value"), "why": str(m.get("why") or "")[:100]}
              for m in (parsed.get("missed") or [])
              if str(m.get("n", "")).isdigit() and 1 <= int(m["n"]) <= len(pool)]
    try:
        hv = max(1, min(5, int(parsed.get("headline_value"))))
    except (TypeError, ValueError):
        hv = None
    junk = sum(1 for s_ in vals if s_ <= 3)
    border = sum(1 for s_ in vals if 4 <= s_ <= 5)
    rel = sum(1 for s_ in vals if s_ >= 6)
    detail = {"headline_value": hv,
              "strong": sum(1 for s_ in vals if s_ >= 8),
              "cards": [{"n": n, "value": v, "title": (cards[n - 1].get("title") or "")[:120],
                         "kind": cards[n - 1].get("kind")} for n, v in sorted(card_v.items())],
              "news": [{"n": n, "score": sc, "title": (items[n - 1].get("title") or "")[:120],
                        "url": items[n - 1].get("url")} for n, sc in sorted(news_v.items())],
              "missed": missed, "rubric": "audit-lead-v2"}
    row = {"d": day, "n": len(vals), "j": junk, "b": border, "r": rel,
           "avg": round(sum(vals) / len(vals), 2) if vals else None,
           "det": json.dumps(detail, ensure_ascii=False), "m": _JUDGE_MODEL}
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
    log.info("судья выпуска %s: заголовок %s/5, новостей %d, фон %d, мусор %d, пропущено %d",
             day, hv, len(vals), border, junk, len(missed))
    return {"ok": True, "n": len(vals), "junk": junk, "borderline": border,
            "relevant": rel, "avg": row["avg"], "headline_value": hv,
            "missed": len(missed), "tokens": (ti, to)}
