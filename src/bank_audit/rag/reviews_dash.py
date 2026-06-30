"""Аналитика отзывов для вкладки «Отзывы» (риск-радар голоса клиента).

Агрегаты поверх корпуса banki.ru (БД `bankiru`, ~390к жалоб 1-2★, 2025-2026):
KPI, помесячная динамика + детект спайков, таксономия тем с трендом и
категорией риска, Сбер-vs-рынок, география (per-capita-аномалии), лента.

Все тяжёлые агрегаты bank-scoped (подмножество ≤50к строк) → быстро.
Кэш на процесс с TTL (агрегаты считаются раз в ~час).
"""
from __future__ import annotations

import functools
import logging
import re
import threading
import time

from sqlalchemy import text

from .bankiru_reviews import _get_engine, resolve_bank, search_reviews

log = logging.getLogger(__name__)


def _safe(default):
    """Не давать сбою одной панели ронять весь дашборд: при исключении
    вернуть default (None/[]), а не пробрасывать 500. Фронт тогда покажет
    «нет данных», а соседние панели продолжат работать."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001 — намеренно широкий guard на границе API
                log.warning("reviews_dash.%s упал: %s", fn.__name__, e)
                return default
        return wrapper
    return deco

# ── Аудиторская таксономия тем жалоб ────────────────────────────────────────
# risk: compliance (регуляторика/комплаенс) | conduct (недобросовестные
# практики к клиенту) | ops (операционные сбои/сервис). patterns — ILIKE-
# подстроки, тема засчитывается если совпал ЛЮБОЙ паттерн. Настраивается.
THEMES = [
    {"key": "blocking", "label": "Блокировки счетов · 115-ФЗ", "risk": "compliance",
     "patterns": ["115-фз", "115 фз", "заблокир", "блокиров", "разблокир", "арест счет", "арестова"]},
    {"key": "escalation", "label": "Эскалация в ЦБ/суд/ФАС", "risk": "compliance",
     "patterns": ["в цб", "центробанк", " в суд", "исков", "подам иск", "фас", "прокурат", "роспотреб", "жалобу в"]},
    {"key": "fraud", "label": "Мошенничество / компрометация", "risk": "compliance",
     "patterns": ["мошенник", "компромет", "украли деньг", "несанкционир", "списали без", "сняли деньги без"]},
    {"key": "insurance", "label": "Навязанная страховка", "risk": "conduct",
     "patterns": ["навяз", "страховку без", "страхование без", "без моего согласия"]},
    {"key": "fees", "label": "Скрытые комиссии / рост тарифов", "risk": "conduct",
     "patterns": ["скрыт", "повысили комисс", "подняли тариф", "повышение тариф", "комиссия за", "удержали комисс"]},
    {"key": "missell", "label": "Навязывание / подключили без согласия", "risk": "conduct",
     "patterns": ["подключили без", "оформили без", "без моего ведома", "обманом", "ввели в заблужд", "не предупред"]},
    {"key": "app", "label": "Сбой приложения / ДБО", "risk": "ops",
     "patterns": ["приложение не работает", "не открывается", "зависает", "вылетает", "сбой в приложении", "не работает онлайн", "не работает приложение"]},
    {"key": "support", "label": "Поддержка / SLA", "risk": "ops",
     "patterns": ["не отвечают", "не дозвон", "никто не реш", "долго ждать", "оператор не", "висел на линии", "отписк"]},
    {"key": "transfer", "label": "Переводы / СБП", "risk": "ops",
     "patterns": ["перевод не", "сбп", "деньги не пришли", "не зачисл", "завис перевод", "потерял перевод"]},
    {"key": "collection", "label": "Взыскание / коллекторы", "risk": "conduct",
     "patterns": ["коллектор", "взыскан", "звонят по кредит", "выбивают", "угрожа", "беспокоят родств"]},
]
THEME_BY_KEY = {t["key"]: t for t in THEMES}

# Население городов (тыс.) — для per-capita аномалий географии. Хватает крупных.
_POP = {
    "москва": 13100, "санкт-петербург": 5600, "новосибирск": 1630, "екатеринбург": 1540,
    "казань": 1310, "нижний новгород": 1210, "челябинск": 1180, "красноярск": 1190,
    "самара": 1160, "уфа": 1160, "ростов-на-дону": 1140, "краснодар": 1100,
    "омск": 1110, "воронеж": 1050, "пермь": 1030, "волгоград": 1000, "саратов": 880,
    "тюмень": 870, "тольятти": 680, "махачкала": 700, "барнаул": 620, "ижевск": 650,
    "хабаровск": 610, "ульяновск": 620, "иркутск": 620, "владивосток": 600, "ярославль": 580,
    "якутск": 380, "сочи": 470, "томск": 570, "оренбург": 550, "кемерово": 550,
    "новокузнецк": 540, "рязань": 530, "астрахань": 470, "пенза": 510, "липецк": 500,
    "тула": 470, "киров": 480, "чебоксары": 490, "калининград": 500, "ставрополь": 530,
    "сургут": 400, "симферополь": 340, "грозный": 330, "белгород": 340, "владимир": 350,
}

# ── Кэш с TTL ───────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_TTL = 3600.0


def _cached(key: str, fn, ttl: float = _TTL):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def _theme_sql(theme: dict, prefix: str) -> tuple[str, dict]:
    # ОДИН регистронезависимый regex-скан (~*) на тему вместо N×ILIKE —
    # одна проходка по строке на тему, а не по разу на каждый паттерн.
    k = f"{prefix}rx"
    rx = "(" + "|".join(re.escape(p) for p in theme["patterns"]) + ")"
    return f'r."reviewBody" ~* :{k}', {k: rx}


def _bank_clause(bank_canon, product):
    cl = ['r."bankName" = :bank']
    params = {"bank": bank_canon}
    if product:
        cl.append('r."product" = :product')
        params["product"] = product
    return " AND ".join(cl), params


# ── Агрегаты ────────────────────────────────────────────────────────────────
@_safe([])
def banks(top: int = 60) -> list[dict]:
    """Список банков корпуса banki.ru по объёму жалоб — для фильтра вкладки.
    Сбер первым (даже если по объёму не №1), дальше по убыванию."""
    eng = _get_engine()
    if eng is None:
        return []

    def _compute():
        with eng.connect() as c:
            rows = c.execute(text(
                'SELECT "bankName", count(*) n FROM bankiru.reviews'
                ' GROUP BY 1 ORDER BY 2 DESC LIMIT :top'),
                {"top": top}).all()
        items = [{"bank": r[0], "n": int(r[1])} for r in rows]
        sber = [x for x in items if x["bank"] == "Сбербанк"]
        rest = [x for x in items if x["bank"] != "Сбербанк"]
        return sber + rest
    return _cached(f"banks:{top}", _compute, ttl=6 * 3600)


@_safe(None)
def overview(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        bclause, bp = _bank_clause(bc, product)
        esc, ep = _theme_sql(THEME_BY_KEY["escalation"], "e")
        with eng.connect() as c:
            # объёмы — только по дате (быстро по индексу datePublished)
            cur = c.execute(text(
                f'SELECT count(*) FILTER (WHERE r."datePublished" >= now() - make_interval(days => :d)),'
                f'       count(*) FILTER (WHERE r."datePublished" >= now() - make_interval(days => :d2)'
                f'                          AND r."datePublished" < now() - make_interval(days => :d))'
                f' FROM bankiru.reviews r WHERE {bclause}'),
                {**bp, "d": days, "d2": days * 2}).one()
            total_cur, total_prev = int(cur[0]), int(cur[1])
            # эскалация (ILIKE) — ТОЛЬКО по строкам текущего периода (мало строк → быстро)
            esc_cur = int(c.execute(text(
                f'SELECT count(*) FROM bankiru.reviews r WHERE {bclause}'
                f' AND r."datePublished" >= now() - make_interval(days => :d) AND {esc}'),
                {**bp, **ep, "d": days}).scalar() or 0)
            # доля рынка + ранг за период
            mk = c.execute(text(
                'SELECT "bankName", count(*) AS n FROM bankiru.reviews r'
                ' WHERE r."datePublished" >= now() - make_interval(days => :d)'
                + (' AND r."product" = :product' if product else '') +
                ' GROUP BY 1 ORDER BY 2 DESC'),
                {"d": days, **({"product": product} if product else {})}).all()
        total_market = sum(int(r[1]) for r in mk) or 1
        share = round(100.0 * total_cur / total_market, 1)
        rank = next((i + 1 for i, r in enumerate(mk) if r[0] == bc), None)
        delta = round(100.0 * (total_cur - total_prev) / total_prev, 1) if total_prev else None
        esc_pct = round(100.0 * esc_cur / total_cur, 1) if total_cur else 0.0
        return {
            "bank": bc, "product": product, "days": days,
            "total": total_cur, "prev": total_prev, "delta_pct": delta,
            "market_share_pct": share, "market_rank": rank, "market_banks": len(mk),
            "escalation_pct": esc_pct,
        }
    return _cached(f"ov:{bc}:{product}:{days}", _compute)


@_safe(None)
def trend(bank: str, product: str | None = None, months: int = 14) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        bclause, bp = _bank_clause(bc, product)
        with eng.connect() as c:
            rows = c.execute(text(
                f"SELECT to_char(date_trunc('month', r.\"datePublished\"), 'YYYY-MM') ym, count(*)"
                f" FROM bankiru.reviews r WHERE {bclause}"
                f" AND r.\"datePublished\" >= date_trunc('month', now()) - make_interval(months => :m)"
                f" GROUP BY 1 ORDER BY 1"),
                {**bp, "m": months - 1}).all()
        series = [{"ym": r[0], "n": int(r[1])} for r in rows]
        vals = [s["n"] for s in series]
        # детект спайка: > mean + 1.0*std (и не первый месяц)
        if len(vals) >= 4:
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            std = var ** 0.5
            for s in series:
                s["spike"] = s["n"] > mean + std and s["n"] > mean * 1.25
                s["pct_vs_mean"] = round(100.0 * (s["n"] - mean) / mean) if mean else 0
        return {"bank": bc, "product": product, "series": series}
    return _cached(f"tr:{bc}:{product}:{months}", _compute)


@_safe(None)
def themes(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        bclause, bp = _bank_clause(bc, product)
        # Темы для аудита = РИСКИ ПОСЛЕДНИХ 90 дн (n/доля), momentum vs пред. 90.
        # Скан ограничен 180 днями + булев-флаг темы считаем ОДИН раз в CTE
        # (иначе ILIKE по 40к длинных текстов × десятки паттернов = десятки сек).
        cte_sel, params = [], dict(bp)
        for t in THEMES:
            ts, tp = _theme_sql(t, f"t{t['key']}_")
            params.update(tp)
            cte_sel.append(f'({ts}) AS "{t["key"]}"')
        n_sel = [f'count(*) FILTER (WHERE dt >= now()-make_interval(days=>90) AND "{t["key"]}") AS "{t["key"]}_n"' for t in THEMES]
        p_sel = [f'count(*) FILTER (WHERE dt < now()-make_interval(days=>90) AND "{t["key"]}") AS "{t["key"]}_p"' for t in THEMES]
        sql = (f'WITH tagged AS MATERIALIZED ('
               f' SELECT r."datePublished" AS dt, {", ".join(cte_sel)}'
               f' FROM bankiru.reviews r WHERE {bclause}'
               f' AND r."datePublished" >= now() - make_interval(days => 180))'
               f' SELECT {", ".join(n_sel + p_sel)},'
               f' count(*) FILTER (WHERE dt >= now()-make_interval(days=>90)) AS "_total"'
               f' FROM tagged')
        with eng.connect() as c:
            row = c.execute(text(sql), params).mappings().one()
        total = int(row["_total"]) or 1
        out = []
        for t in THEMES:
            n = int(row[f'{t["key"]}_n'])
            mp = int(row[f'{t["key"]}_p'])
            d = round(100.0 * (n - mp) / mp) if mp else (None if n == 0 else 100)
            out.append({"key": t["key"], "label": t["label"], "risk": t["risk"],
                        "n": n, "pct": round(100.0 * n / total, 1), "delta_pct": d})
        out.sort(key=lambda x: x["n"], reverse=True)
        return {"bank": bc, "product": product, "days": 90, "total": total, "themes": out}
    return _cached(f"th:{bc}:{product}", _compute)


@_safe(None)
def vs_market(bank: str, product: str | None = None, days: int = 90, top: int = 8) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        with eng.connect() as c:
            rows = c.execute(text(
                'SELECT "bankName", count(*) n FROM bankiru.reviews r'
                ' WHERE r."datePublished" >= now() - make_interval(days => :d)'
                + (' AND r."product" = :product' if product else '') +
                ' GROUP BY 1 ORDER BY 2 DESC'),
                {"d": days, **({"product": product} if product else {})}).all()
        total = sum(int(r[1]) for r in rows) or 1
        ranked = [{"bank": r[0], "n": int(r[1]), "pct": round(100.0 * int(r[1]) / total, 1),
                   "is_target": r[0] == bc} for r in rows]
        top_rows = ranked[:top]
        if not any(r["is_target"] for r in top_rows):
            tgt = next((r for r in ranked if r["is_target"]), None)
            if tgt:
                top_rows = top_rows[:top - 1] + [tgt]
        return {"bank": bc, "product": product, "days": days, "rows": top_rows}
    return _cached(f"vm:{bc}:{product}:{days}:{top}", _compute)


@_safe(None)
def geo(bank: str, product: str | None = None, days: int = 365, top: int = 8) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        bclause, bp = _bank_clause(bc, product)
        with eng.connect() as c:
            rows = c.execute(text(
                f"SELECT split_part(r.location, ' (', 1) AS city, count(*) n"
                f" FROM bankiru.reviews r WHERE {bclause} AND r.location <> ''"
                f" AND r.\"datePublished\" >= now() - make_interval(days => :d)"
                f" GROUP BY 1 ORDER BY 2 DESC LIMIT 40"),
                {**bp, "d": days}).all()
        cities = []
        for city, n in rows:
            n = int(n)
            pop = _POP.get(city.strip().lower())
            per100k = round(n / (pop / 100.0), 1) if pop else None
            cities.append({"city": city, "n": n, "per_100k": per100k})
        # аномалия: per-capita сильно выше медианы городов с известным населением
        known = [c["per_100k"] for c in cities if c["per_100k"] is not None]
        if known:
            known_sorted = sorted(known)
            med = known_sorted[len(known_sorted) // 2]
            for c in cities:
                c["anomaly"] = bool(c["per_100k"] and c["per_100k"] > med * 2.2 and c["n"] >= 50)
        return {"bank": bc, "product": product, "days": days, "cities": cities[:top]}
    return _cached(f"geo:{bc}:{product}:{days}:{top}", _compute)


@_safe(None)
def products(bank: str, days: int = 365, top: int = 10) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        with eng.connect() as c:
            rows = c.execute(text(
                'SELECT "product", count(*) n FROM bankiru.reviews r'
                ' WHERE r."bankName" = :bank AND r."datePublished" >= now() - make_interval(days => :d)'
                ' GROUP BY 1 ORDER BY 2 DESC LIMIT :top'),
                {"bank": bc, "d": days, "top": top}).all()
        return {"bank": bc, "items": [{"product": r[0], "n": int(r[1])} for r in rows]}
    return _cached(f"pr:{bc}:{days}:{top}", _compute)


def list_reviews(bank: str, product: str | None = None, theme: str | None = None,
                 q: str | None = None, days: int | None = None, limit: int = 20) -> list[dict]:
    """Лента доказательной базы. q → семантика; иначе свежие, опц. фильтр по теме."""
    bc = resolve_bank(bank) if bank else None
    if q and q.strip():
        return search_reviews(q, bank=bank, product=product, since_days=days, k=limit)
    eng = _get_engine()
    if eng is None or not bc:
        return []
    bclause, bp = _bank_clause(bc, product)
    params = {**bp, "limit": limit}
    theme_clause = ""
    if theme and theme in THEME_BY_KEY:
        ts, tp = _theme_sql(THEME_BY_KEY[theme], "lt")
        theme_clause = f" AND {ts}"
        params.update(tp)
    if days:
        theme_clause += " AND r.\"datePublished\" >= now() - make_interval(days => :d)"
        params["d"] = days
    try:
        with eng.connect() as c:
            rows = c.execute(text(
                f'SELECT r."bankName" bank, r."product" product, r."datePublished" dt,'
                f' r.url, r."reviewBody" body, r.location'
                f' FROM bankiru.reviews r WHERE {bclause}{theme_clause}'
                f' AND length(r."reviewBody") >= 40'
                f' ORDER BY r."datePublished" DESC LIMIT :limit'), params).mappings().all()
    except Exception as e:
        log.warning("reviews_dash.list_reviews failed: %s", e)
        return []
    seen, out = set(), []
    for r in rows:
        body = (r["body"] or "").strip()
        key = body[:100].lower()
        if key in seen:
            continue
        seen.add(key)
        dt = r["dt"]
        out.append({"bank": r["bank"], "product": r["product"],
                    "date": dt.date().isoformat() if dt else None,
                    "city": (r["location"] or "").split(" (")[0],
                    "url": r["url"], "text": body})
    return out
