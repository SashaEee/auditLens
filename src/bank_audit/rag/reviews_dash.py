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
    {"key": "blocking", "label": "Блокировки счетов · 115/161-ФЗ", "risk": "compliance",
     "patterns": ["115-фз", "115 фз", "161-фз", "161 фз", "заблокир", "блокиров", "разблокир", "приостановил", "ограничил операц", "арест счет", "арестова", "заморозил"]},
    {"key": "escalation", "label": "Эскалация в ЦБ/суд/ФАС", "risk": "compliance",
     "patterns": ["в цб", "центробанк", "центральный банк", " в суд", "исков", "подам иск", "антимонопольн", " в фас", "прокурат", "роспотреб", "жалобу в", "регулятор"]},
    {"key": "fraud", "label": "Мошенничество / компрометация", "risk": "compliance",
     "patterns": ["мошенник", "компромет", "украли деньг", "несанкционир", "списали без", "сняли деньги без"]},
    {"key": "insurance", "label": "Навязанная страховка", "risk": "conduct",
     "patterns": ["навяз", "страховку без", "страхование без", "без моего согласия"]},
    {"key": "fees", "label": "Скрытые комиссии / рост тарифов", "risk": "conduct",
     "patterns": ["скрыт комисс", "скрыт плат", "скрыт усл", "повысили комисс", "подняли тариф", "повышение тариф", "комиссия за", "удержали комисс", "навязали комисс"]},
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
    # ── расширение покрытия (эмпирически, по кластерам «Прочего» — 2026-06) ──
    {"key": "mortgage", "label": "Ипотека · Домклик", "risk": "ops",
     "patterns": ["ипотек", "домклик", "дом клик", "обременени", "график платеж"]},
    {"key": "branch", "label": "Отделения · сотрудники", "risk": "conduct",
     "patterns": ["некомпетентн", "непрофессионал", "нахамил", "хамств", "хамят", "нагрубил", "только в отделен", "взять талон"]},
    {"key": "enforcement", "label": "Исполнительные листы · алименты", "risk": "compliance",
     "patterns": ["алимент", "пристав", "исполнительн лист", "229-фз", "229 фз", "прожиточн минимум"]},
    {"key": "bankruptcy", "label": "Банкротство · БКИ", "risk": "compliance",
     "patterns": ["банкротств", "127-фз", "213.28", "кредитн истори", "в бки ", "финансовый управляющ", "освобожден от долг"]},
    {"key": "rate", "label": "Ставка · условия кредита", "risk": "conduct",
     "patterns": ["повысили ставк", "повышение ставк", "подняли ставк", "снижение ставк", "снизить ставк", "неустойк", "изменили услови"]},
    {"key": "loyalty", "label": "Бонусы · кэшбэк · СберСпасибо", "risk": "conduct",
     "patterns": ["сберспасибо", "спасибо за покупк", "бонус спасибо", "кэшбэк", "кэшбек", "бонусн балл", "сберпрайм", "сберпремьер"]},
    {"key": "atm", "label": "Банкоматы · наличные", "risk": "ops",
     "patterns": ["банкомат", "зажева", "застрял", "купюр", "внесени наличн", "выдач наличн", "пересчит"]},
    {"key": "deposit", "label": "Вклады · накопительные · ПДС", "risk": "conduct",
     "patterns": ["вклад", "накопительн счет", "накопительный счет", "депозит", " пдс", "долгосрочные сбережен"]},
    {"key": "subscription", "label": "Подписки · автосписания", "risk": "conduct",
     "patterns": ["подписк", "сбермобайл", "сберздоров", "яндекс плюс", "автосписани", "автоплатеж"]},
    {"key": "inheritance", "label": "Наследование · счета умерших", "risk": "compliance",
     "patterns": ["наследств", "наследник", "свидетельство о смерт", "по наследству", "вступлени в наследств"]},
    {"key": "hardship", "label": "Кредитные каникулы · реструктуризация", "risk": "compliance",
     "patterns": ["кредитн каникул", "ипотечн каникул", "реструктуризац", "неплатежеспособ", "урегулирование задолж"]},
]
THEME_BY_KEY = {t["key"]: t for t in THEMES}
# Скомпилированные паттерны для пер-отзыв тегирования (Python-side, для сегментов
# drill-in и LLM-объяснений). Та же таксономия, что и в _theme_sql (SQL-агрегат).
_THEME_RE = [(t, re.compile("|".join(re.escape(p) for p in t["patterns"]), re.I)) for t in THEMES]


def _short(label: str) -> str:
    """Короткая метка темы для чипов в ленте (до разделителя · или /)."""
    return re.split(r"\s*[·/]\s*", label)[0]


def theme_obj(key: str) -> dict | None:
    """Полный объект темы по ключу — для LLM-классификации (key→{label,short,risk})."""
    t = THEME_BY_KEY.get(key)
    return {"key": t["key"], "label": t["label"], "short": _short(t["label"]), "risk": t["risk"]} if t else None


def match_themes(body: str | None) -> list[dict]:
    """Темы отзыва по regex — мультилейбл. Возвращает [{key,label,short,risk}]."""
    b = body or ""
    return [{"key": t["key"], "label": t["label"], "short": _short(t["label"]), "risk": t["risk"]}
            for t, rx in _THEME_RE if rx.search(b)]


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    m = n // 2
    return float(s[m]) if n % 2 else (s[m - 1] + s[m]) / 2.0

# Население городов (тыс.) — для per-capita аномалий географии. Покрывает все
# города РФ ~100k+ и региональные центры, чтобы per_100k и аномалии считались
# не только по горстке миллионников. Ключи в нижнем регистре, ё→е (см. lookup).
_POP = {
    # миллионники
    "москва": 13100, "санкт-петербург": 5600, "новосибирск": 1630, "екатеринбург": 1540,
    "казань": 1310, "нижний новгород": 1200, "челябинск": 1180, "красноярск": 1190,
    "самара": 1160, "уфа": 1150, "ростов-на-дону": 1140, "краснодар": 1100,
    "омск": 1110, "воронеж": 1050, "пермь": 1030, "волгоград": 1000,
    # 500k–1млн
    "саратов": 880, "тюмень": 870, "тольятти": 680, "махачкала": 700, "барнаул": 620,
    "ижевск": 650, "хабаровск": 610, "ульяновск": 620, "иркутск": 620, "владивосток": 600,
    "ярославль": 580, "томск": 570, "оренбург": 550, "кемерово": 550, "новокузнецк": 540,
    "набережные челны": 540, "рязань": 530, "ставрополь": 540, "севастополь": 510,
    "пенза": 510, "балашиха": 510, "липецк": 500,
    # 300k–500k
    "чебоксары": 490, "калининград": 490, "киров": 480, "тула": 470, "сочи": 470,
    "курск": 450, "улан-удэ": 440, "тверь": 420, "магнитогорск": 410, "иваново": 400,
    "брянск": 400, "сургут": 400, "белгород": 390, "якутск": 380, "калуга": 360,
    "владимир": 350, "архангельск": 350, "чита": 350, "симферополь": 340, "грозный": 330,
    "волжский": 320, "смоленск": 320, "саранск": 320, "череповец": 310, "вологда": 310,
    "подольск": 310, "орел": 300, "владикавказ": 300, "курган": 300,
    # 200k–300k
    "тамбов": 290, "нижневартовск": 280, "новороссийск": 280, "йошкар-ола": 280,
    "петрозаводск": 280, "мурманск": 270, "кострома": 270, "стерлитамак": 270, "мытищи": 270,
    "химки": 260, "нижнекамск": 240, "сыктывкар": 240, "нальчик": 240, "благовещенск": 240,
    "комсомольск-на-амуре": 240, "королев": 230, "шахты": 230, "дзержинск": 230, "энгельс": 230,
    "орск": 220, "ангарск": 220, "братск": 220, "великий новгород": 220, "старый оскол": 220,
    "псков": 210, "люберцы": 210, "красногорск": 210, "бийск": 200, "южно-сахалинск": 200,
    # 100k–200k
    "армавир": 190, "балаково": 190, "абакан": 190, "прокопьевск": 190, "рыбинск": 180,
    "северодвинск": 180, "норильск": 180, "петропавловск-камчатский": 180, "уссурийск": 180,
    "сызрань": 170, "новочеркасск": 170, "электросталь": 160, "златоуст": 160,
    "каменск-уральский": 160, "копейск": 150, "хасавюрт": 150, "пятигорск": 150, "керчь": 150,
    "одинцово": 140, "домодедово": 140, "майкоп": 140, "ковров": 140, "кисловодск": 130,
    "батайск": 130, "серпухов": 130, "каспийск": 130, "раменское": 130, "нефтеюганск": 130,
    "дербент": 120, "новый уренгой": 120, "назрань": 120, "кызыл": 120, "орехово-зуево": 120,
    "долгопрудный": 120, "димитровград": 110, "жуковский": 110, "реутов": 110, "пушкино": 110,
    "ноябрьск": 110, "ханты-мансийск": 110, "муром": 105, "ачинск": 105, "новокуйбышевск": 100,
    "элиста": 100, "магадан": 90, "биробиджан": 70, "горно-алтайск": 65, "салехард": 50,
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
                'SELECT "bankName", count(DISTINCT url) n FROM bankiru.reviews'
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
            # count(DISTINCT r.url): корпус banki.ru содержит точные дубли (сбой
            # дедупа краулера — один URL встречается тысячи раз), считать их как
            # разные жалобы = ложные всплески. Одна жалоба = один URL.
            cur = c.execute(text(
                f'SELECT count(DISTINCT r.url) FILTER (WHERE r."datePublished" >= now() - make_interval(days => :d)),'
                f'       count(DISTINCT r.url) FILTER (WHERE r."datePublished" >= now() - make_interval(days => :d2)'
                f'                          AND r."datePublished" < now() - make_interval(days => :d))'
                f' FROM bankiru.reviews r WHERE {bclause}'),
                {**bp, "d": days, "d2": days * 2}).one()
            total_cur, total_prev = int(cur[0]), int(cur[1])
            # эскалация (ILIKE) — ТОЛЬКО по строкам текущего периода (мало строк → быстро)
            esc_cur = int(c.execute(text(
                f'SELECT count(DISTINCT r.url) FROM bankiru.reviews r WHERE {bclause}'
                f' AND r."datePublished" >= now() - make_interval(days => :d) AND {esc}'),
                {**bp, **ep, "d": days}).scalar() or 0)
            # доля рынка + ранг за период
            mk = c.execute(text(
                'SELECT "bankName", count(DISTINCT r.url) AS n FROM bankiru.reviews r'
                ' WHERE r."datePublished" >= now() - make_interval(days => :d)'
                + (' AND r."product" = :product' if product else '') +
                ' GROUP BY 1 ORDER BY 2 DESC'),
                {"d": days, **({"product": product} if product else {})}).all()
            # свежесть данных — последняя дата отзыва по банку (индекс по datePublished)
            asof = c.execute(text(
                f'SELECT max(r."datePublished") FROM bankiru.reviews r WHERE {bclause}'),
                bp).scalar()
        total_market = sum(int(r[1]) for r in mk) or 1
        share = round(100.0 * total_cur / total_market, 1)
        rank = next((i + 1 for i, r in enumerate(mk) if r[0] == bc), None)
        delta = round(100.0 * (total_cur - total_prev) / total_prev, 1) if total_prev else None
        esc_pct = round(100.0 * esc_cur / total_cur, 1) if total_cur else 0.0
        return {
            "bank": bc, "product": product, "days": days,
            "total": total_cur, "prev": total_prev, "delta_pct": delta,
            # малые абсолютные числа делают %-дельту шумной — помечаем
            "delta_low_n": bool(total_prev and min(total_cur, total_prev) < 30),
            "market_share_pct": share, "market_rank": rank, "market_banks": len(mk),
            "escalation_pct": esc_pct,
            "as_of": asof.date().isoformat() if asof else None,
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
                f"SELECT to_char(date_trunc('month', r.\"datePublished\"), 'YYYY-MM') ym, count(DISTINCT r.url)"
                f" FROM bankiru.reviews r WHERE {bclause}"
                f" AND r.\"datePublished\" >= date_trunc('month', now()) - make_interval(months => :m)"
                f" GROUP BY 1 ORDER BY 1"),
                {**bp, "m": months - 1}).all()
            cur_ym = c.execute(text("SELECT to_char(now(),'YYYY-MM')")).scalar()
        series = [{"ym": r[0], "n": int(r[1]), "partial": r[0] == cur_ym} for r in rows]
        # baseline и детект спайка — ТОЛЬКО по завершённым месяцам (текущий неполный
        # занижен и раздувал бы «падение»/смещал среднее). Robust: медиана + MAD,
        # устойчиво к самому пику и к растущему тренду (в отличие от mean+std).
        complete = [s["n"] for s in series if not s["partial"]]
        med = None
        if len(complete) >= 4:
            med = _median(complete)
            mad = _median([abs(v - med) for v in complete]) or (
                sum(abs(v - med) for v in complete) / len(complete))
            thr = med + 2.0 * mad   # ловит явный пик (напр. +55%), не шумит на ровном ряде
            for s in series:
                s["pct_vs_median"] = round(100.0 * (s["n"] - med) / med) if med else 0
                s["spike"] = (not s["partial"]) and s["n"] > thr and s["n"] >= med * 1.4
        return {"bank": bc, "product": product, "series": series, "baseline": med}
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
        any_expr = " OR ".join(f'"{t["key"]}"' for t in THEMES)   # отзыв попал хоть в одну тему
        # дедуп источника СНАЧАЛА (DISTINCT ON url), потом regex по уникальным
        # (корпус содержит точные дубли краулера — иначе счёт и время раздуты)
        sql = (f'WITH dd AS MATERIALIZED ('
               f' SELECT DISTINCT ON (r.url) r."datePublished", r."reviewBody"'
               f' FROM bankiru.reviews r WHERE {bclause}'
               f' AND r."datePublished" >= now() - make_interval(days => 180)'
               f' ORDER BY r.url),'
               f' tagged AS MATERIALIZED ('
               f' SELECT r."datePublished" AS dt, {", ".join(cte_sel)} FROM dd r)'
               f' SELECT {", ".join(n_sel + p_sel)},'
               f' count(*) FILTER (WHERE dt >= now()-make_interval(days=>90)) AS "_total",'
               f' count(*) FILTER (WHERE dt >= now()-make_interval(days=>90) AND NOT ({any_expr})) AS "_other"'
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
        # «Прочее / без темы» — сколько жалоб не попало ни в одну тему (контекст
        # полноты риск-карты; темы мультилейбл, поэтому сумма pct ≠ 100%).
        other_n = int(row["_other"])
        if other_n:
            out.append({"key": "other", "label": "Прочее / без темы", "risk": "other",
                        "n": other_n, "pct": round(100.0 * other_n / total, 1), "delta_pct": None})
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
                'SELECT "bankName", count(DISTINCT r.url) n FROM bankiru.reviews r'
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
                f"SELECT split_part(r.location, ' (', 1) AS city, count(DISTINCT r.url) n"
                f" FROM bankiru.reviews r WHERE {bclause} AND r.location <> ''"
                f" AND r.\"datePublished\" >= now() - make_interval(days => :d)"
                f" GROUP BY 1 ORDER BY 2 DESC LIMIT 40"),
                {**bp, "d": days}).all()
        cities = []
        for city, n in rows:
            n = int(n)
            pop = _POP.get(city.strip().lower().replace("ё", "е"))
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
                'SELECT "product", count(DISTINCT r.url) n FROM bankiru.reviews r'
                ' WHERE r."bankName" = :bank AND r."datePublished" >= now() - make_interval(days => :d)'
                ' GROUP BY 1 ORDER BY 2 DESC LIMIT :top'),
                {"bank": bc, "d": days, "top": top}).all()
        return {"bank": bc, "items": [{"product": r[0], "n": int(r[1])} for r in rows]}
    return _cached(f"pr:{bc}:{days}:{top}", _compute)


def list_reviews(bank: str, product: str | None = None, theme: str | None = None,
                 q: str | None = None, days: int | None = None,
                 city: str | None = None, month: str | None = None,
                 limit: int = 20) -> list[dict]:
    """Лента доказательной базы. q → семантика; иначе свежие с фильтрами
    тема/город/месяц. Дубли (массовые однотипные жалобы) не прячем, а считаем —
    массовость это аудит-сигнал → поле `similar`."""
    bc = resolve_bank(bank) if bank else None
    if q and q.strip():
        res = search_reviews(q, bank=bank, product=product, since_days=days, k=limit)
        for r in res:
            r["themes"] = match_themes(r.get("text", ""))   # пер-отзыв темы (regex baseline)
        return res
    eng = _get_engine()
    if eng is None or not bc:
        return []
    bclause, bp = _bank_clause(bc, product)
    # тянем с запасом, чтобы счётчик «ещё N похожих» был осмысленным после дедупа
    fetch = min(max(limit * 5, 40), 120)
    params = {**bp, "lim": fetch}
    clause = ""
    if theme and theme in THEME_BY_KEY:
        ts, tp = _theme_sql(THEME_BY_KEY[theme], "lt")
        clause += f" AND {ts}"
        params.update(tp)
    if days:
        clause += " AND r.\"datePublished\" >= now() - make_interval(days => :d)"
        params["d"] = days
    if city:
        clause += " AND split_part(r.location, ' (', 1) = :city"
        params["city"] = city
    if month:
        clause += " AND date_trunc('month', r.\"datePublished\") = to_date(:month, 'YYYY-MM')"
        params["month"] = month
    try:
        with eng.connect() as c:
            # DISTINCT ON (url): в корпусе точные дубли краулера (один URL тысячи
            # раз). Без дедупа окно из :lim свежих заполнялось бы копиями одной
            # жалобы (все с датой заливки), реальные отзывы вытеснялись.
            rows = c.execute(text(
                f'SELECT bank, product, dt, url, body, location FROM ('
                f'  SELECT DISTINCT ON (r.url) r."bankName" bank, r."product" product,'
                f'  r."datePublished" dt, r.url, r."reviewBody" body, r.location'
                f'  FROM bankiru.reviews r WHERE {bclause}{clause}'
                f'  AND length(r."reviewBody") >= 40'
                f'  ORDER BY r.url, r."datePublished" DESC) s'
                f' ORDER BY dt DESC LIMIT :lim'), params).mappings().all()
    except Exception as e:
        log.warning("reviews_dash.list_reviews failed: %s", e)
        return []
    seen: dict[str, int] = {}
    out: list[dict] = []
    for r in rows:
        body = (r["body"] or "").strip()
        key = body[:100].lower()
        if key in seen:
            out[seen[key]]["similar"] += 1
            continue
        seen[key] = len(out)
        dt = r["dt"]
        out.append({"bank": r["bank"], "product": r["product"],
                    "date": dt.date().isoformat() if dt else None,
                    "city": (r["location"] or "").split(" (")[0],
                    "url": r["url"], "text": body, "similar": 0,
                    "themes": match_themes(body)})   # пер-отзыв темы (regex baseline)
    return out[:limit]


@_safe(None)
def segment_reviews(bank: str, product: str | None = None, city: str | None = None,
                    month: str | None = None, limit: int = 40) -> dict | None:
    """Сводка по срезу (город или месяц) для LLM-объяснения аномалии/пика:
    тексты жалоб + детерминированный regex-разбор тем + примеры со ссылками."""
    revs = list_reviews(bank, product=product, city=city, month=month, limit=limit)
    if not revs:
        return {"n": 0, "themes": [], "samples": [], "texts": []}
    from collections import Counter
    cnt: Counter = Counter()
    risk_by: dict[str, str] = {}
    for r in revs:
        for th in match_themes(r.get("text", "")):
            cnt[th["label"]] += 1
            risk_by[th["label"]] = th["risk"]
    themes = [{"label": lbl, "risk": risk_by[lbl], "n": n} for lbl, n in cnt.most_common(6)]
    samples = [{"date": r["date"], "city": r.get("city"), "url": r["url"],
                "text": (r["text"] or "")[:320]} for r in revs[:4]]
    texts = [(r["text"] or "")[:600] for r in revs[:25]]
    return {"n": len(revs), "themes": themes, "samples": samples, "texts": texts}


def _theme_week_counts(eng, where_sql: str, params_extra: dict) -> dict:
    """Помесячно→понедельно: по каждой теме счёт за окна w0[0-7д], w1[7-14д],
    base[14-63д] + общие. where_sql — bank-scoped или 'TRUE' (рынок)."""
    cte_sel, params = [], dict(params_extra)
    for t in THEMES:
        ts, tp = _theme_sql(t, f"x{t['key']}_")
        params.update(tp)
        cte_sel.append(f'({ts}) AS "{t["key"]}"')
    w0 = "dt >= now()-make_interval(days=>7)"
    w1 = "dt < now()-make_interval(days=>7) AND dt >= now()-make_interval(days=>14)"
    base = "dt < now()-make_interval(days=>14)"
    sel = []
    for t in THEMES:
        k = t["key"]
        sel += [f'count(*) FILTER (WHERE {w0} AND "{k}") AS "{k}_w0"',
                f'count(*) FILTER (WHERE {w1} AND "{k}") AS "{k}_w1"',
                f'count(*) FILTER (WHERE {base} AND "{k}") AS "{k}_b"']
    # Дедуп СНАЧАЛА (DISTINCT ON url), потом regex-скан по уникальным — иначе
    # точные дубли краулера и раздувают счёт (ложные всплески), и удваивают
    # тяжёлый market-скан. Один проход дедупа дешевле, чем count(DISTINCT)×63.
    sql = (f'WITH dd AS MATERIALIZED ('
           f' SELECT DISTINCT ON (r.url) r."datePublished", r."reviewBody"'
           f' FROM bankiru.reviews r WHERE {where_sql}'
           f' AND r."datePublished" >= now() - make_interval(days => 63)'
           f' ORDER BY r.url),'
           f' tagged AS MATERIALIZED ('
           f' SELECT r."datePublished" AS dt, {", ".join(cte_sel)} FROM dd r)'
           f' SELECT {", ".join(sel)},'
           f' count(*) FILTER (WHERE {w0}) AS "_tw0", count(*) FILTER (WHERE {base}) AS "_tb"'
           f' FROM tagged')
    with eng.connect() as c:
        return dict(c.execute(text(sql), params).mappings().one())


@_safe(None)
def weekly_signals(bank: str, product: str | None = None) -> dict | None:
    """Срочные аномалии за 7 дней — МНОГОСИГНАЛЬНО (не просто «выросло ×N»):
    рост к базлайну (среднее за 7 нед, окно 14–63 дн), УСКОРЕНИЕ (нед-к-нед), сравнение с РЫНКОМ (всплеск
    только у банка vs отраслевой тренд), ГЕО-концентрация (локальный сбой), новые
    темы. Числа детерминированы; LLM объясняет и приоритизирует, не меняя их."""
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None
    BASE_W = 7.0   # недель в базлайне (49 дн: окно 14–63)

    def _compute():
        bclause, bp = _bank_clause(bc, product)
        brow = _theme_week_counts(eng, bclause, bp)
        try:
            mrow = _theme_week_counts(eng, "TRUE", {})     # рынок (все банки)
        except Exception as e:
            log.warning("weekly_signals: рыночный срез не посчитан: %s", e)
            mrow = None
        out = []
        for t in THEMES:
            k = t["key"]
            w0, w1, b = int(brow[f"{k}_w0"]), int(brow[f"{k}_w1"]), int(brow[f"{k}_b"])
            bw = b / BASE_W
            ratio = (w0 / bw) if bw >= 0.5 else None
            new = b <= 2 and w0 >= 6
            accel = w0 > w1 and w0 >= max(8, 1.4 * w1)      # нарастает неделя к неделе
            surge = w0 >= 8 and bw >= 1.0 and ratio is not None and ratio >= 1.8
            if not (surge or new):
                continue
            mratio, bank_specific = None, False
            if mrow is not None:
                mw0, mb = int(mrow[f"{k}_w0"]), int(mrow[f"{k}_b"])
                mbw = mb / BASE_W
                mratio = round(mw0 / mbw, 2) if mbw >= 0.5 else None
                if ratio is not None and (mratio is None or mratio < 1.4 or ratio >= 1.8 * mratio):
                    bank_specific = True   # всплеск у банка, рынок ровный → наша регрессия
            out.append({"key": k, "label": t["label"], "short": _short(t["label"]),
                        "risk": t["risk"], "week": w0, "prev_week": w1,
                        # сырьё нормы — чтобы UI показывал аудитору саму формулу,
                        # а не только результат (b жалоб за BASE_W недель)
                        "base_count": b, "base_weeks": int(BASE_W),
                        "week_total": int(brow["_tw0"]),
                        "baseline_week": round(bw, 1), "ratio": (round(ratio, 1) if ratio else None),
                        "new": bool(new), "accel": bool(accel),
                        "market_ratio": mratio, "bank_specific": bool(bank_specific)})
        out.sort(key=lambda s: (s["ratio"] or 5) * s["week"], reverse=True)
        # гео-концентрация ведущего сигнала (локальный сбой/инцидент)
        if out:
            top = out[0]
            try:
                ts, tp = _theme_sql(THEME_BY_KEY[top["key"]], "g")
                with eng.connect() as c:
                    grows = c.execute(text(
                        f"SELECT split_part(r.location, ' (', 1) city, count(DISTINCT r.url) n"
                        f" FROM bankiru.reviews r WHERE {bclause} AND {ts} AND r.location <> ''"
                        f" AND r.\"datePublished\" >= now()-make_interval(days=>7)"
                        f" GROUP BY 1 ORDER BY 2 DESC LIMIT 3"), {**bp, **tp}).all()
                tot = sum(int(x[1]) for x in grows) or 1
                if grows and int(grows[0][1]) >= 4 and int(grows[0][1]) / tot >= 0.4:
                    top["geo"] = {"city": grows[0][0], "share": round(100 * int(grows[0][1]) / tot)}
            except Exception:
                pass
        # уровень приоритета
        for s in out:
            score = ((s["ratio"] or 4) * (1.5 if s["bank_specific"] else 1.0)
                     * (1.3 if s["accel"] else 1.0) * (1.4 if s["risk"] == "compliance" else 1.0))
            s["level"] = "high" if (score >= 5 and s["week"] >= 10) or (
                s["bank_specific"] and (s["ratio"] or 0) >= 2.5) else "medium"
        out.sort(key=lambda s: (s["level"] == "high", (s["ratio"] or 5) * s["week"]), reverse=True)
        tw0, tb = int(brow["_tw0"]), int(brow["_tb"])
        tbw = tb / BASE_W
        overall = {"week": tw0, "baseline_week": round(tbw, 1),
                   "ratio": (round(tw0 / tbw, 1) if tbw >= 0.5 else None)}
        if mrow is not None:
            mtw0, mtb = int(mrow["_tw0"]), int(mrow["_tb"])
            mtbw = mtb / BASE_W
            overall["market_ratio"] = round(mtw0 / mtbw, 2) if mtbw >= 0.5 else None
        return {"bank": bc, "product": product, "signals": out[:6], "overall": overall}
    return _cached(f"wk:{bc}:{product}", _compute, ttl=1800)
