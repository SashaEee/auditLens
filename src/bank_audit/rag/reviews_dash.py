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
import os
import re
import threading
import time

from sqlalchemy import text

from .. import db
from .bankiru_reviews import _get_engine, resolve_bank, search_reviews

log = logging.getLogger(__name__)

# Считать панель тем по сохранённой разметке, а не regex-сканом по текстам.
# Рубильник нужен на время, пока новая таксономия не подтверждена на проде:
# при выключении и при отсутствии разметки всё считается по-старому.
TOPICS_FROM_LABELS = os.getenv("REVIEW_TOPICS_AGG", "1").lower() not in ("0", "false", "no")
# Лента по единому индексу (все источники), а не только по внешней базе banki.ru.
# Рубильник на случай отката: при выключении вкладка ведёт себя как раньше.
FEED_FROM_INDEX = os.getenv("REVIEWS_FEED_INDEX", "1").lower() not in ("0", "false", "no")


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
     "patterns": ["в цб", "центробанк", "центральн банк", " в суд", "исков", "подам иск", "антимонопольн", " в фас", "прокурат", "роспотреб", "жалоб в", "регулятор"]},
    {"key": "fraud", "label": "Мошенничество / компрометация", "risk": "compliance",
     "patterns": ["мошенник", "компромет", "украли деньг", "несанкционир", "списали без", "сняли деньги без"]},
    {"key": "insurance", "label": "Навязанная страховка", "risk": "conduct",
     "patterns": ["навяз", "страховк без", "страховани без", "без моего согласия"]},
    {"key": "fees", "label": "Скрытые комиссии / рост тарифов", "risk": "conduct",
     "patterns": ["скрыт комисс", "скрыт плат", "скрыт усл", "повысили комисс", "подняли тариф", "повышени тариф", "комисси за", "удержали комисс", "навязали комисс"]},
    {"key": "missell", "label": "Навязывание / подключили без согласия", "risk": "conduct",
     "patterns": ["подключил без", "оформил без", "без моего ведома", "обманом", "ввели в заблужд", "не предупред"]},
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
     "patterns": ["банкротств", "127-фз", "213.28", "кредитн истори", "в бки", "финансов управляющ", "освобожден от долг"]},
    {"key": "rate", "label": "Ставка · условия кредита", "risk": "conduct",
     "patterns": ["повысили ставк", "повышени ставк", "подняли ставк", "снижени ставк", "снизить ставк", "неустойк", "изменил услови"]},
    {"key": "loyalty", "label": "Бонусы · кэшбэк · СберСпасибо", "risk": "conduct",
     "patterns": ["сберспасибо", "спасибо за покупк", "бонус спасибо", "кэшбэк", "кэшбек", "бонусн балл", "сберпрайм", "сберпремьер"]},
    {"key": "atm", "label": "Банкоматы · наличные", "risk": "ops",
     "patterns": ["банкомат", "зажева", "застрял", "купюр", "внесени наличн", "выдач наличн", "пересчит"]},
    {"key": "deposit", "label": "Вклады · накопительные · ПДС", "risk": "conduct",
     "patterns": ["вклад", "накопительн счет", "депозит", " пдс", "долгосрочн сбережен"]},
    {"key": "subscription", "label": "Подписки · автосписания", "risk": "conduct",
     "patterns": ["подписк", "сбермобайл", "сберздоров", "яндекс плюс", "автосписани", "автоплатеж"]},
    {"key": "inheritance", "label": "Наследование · счета умерших", "risk": "compliance",
     "patterns": ["наследств", "наследник", "свидетельств о смерт", "по наследству", "вступлени в наследств"]},
    {"key": "hardship", "label": "Кредитные каникулы · реструктуризация", "risk": "compliance",
     "patterns": ["кредитн каникул", "ипотечн каникул", "реструктуризац", "неплатежеспособ", "урегулировани задолж"]},
]
THEME_BY_KEY = {t["key"]: t for t in THEMES}


def _stem_rx(pattern: str, boundary: str) -> str:
    """Паттерн темы → regex с учётом русской морфологии.

    Словарь выше писался ОСНОВАМИ через пробел («скрыт комисс»), но сравнивался
    буквальной подстрокой — «скрытая комиссия» не совпадала, и тема находила
    пятую часть своих отзывов. Здесь основа от 4 символов получает произвольное
    окончание; короткие служебные слова («в», «за», «не») остаются буквальными,
    иначе «в» съело бы «все» и потянуло ложные срабатывания.

    Ведущий пробел в паттерне означает границу слова (« пдс» → «(ПДС)», «ПДС.»).
    Короткое последнее слово тоже закрываем границей, иначе «комисси за» ловит
    «комиссия задолженности».

    boundary — синтаксис границы слова: \\b для Python, \\y для Postgres.
    Семантика одна, движки разные, поэтому строку собираем дважды.
    """
    toks = pattern.split()
    parts: list[str] = []
    for i, t in enumerate(toks):
        parts.append(re.escape(t))
        if i < len(toks) - 1:
            parts.append(r"\w*\s+" if len(t) >= 4 else r"\s+")
    lead = boundary if pattern[:1].isspace() else ""
    tail = boundary if len(toks[-1]) < 4 else ""
    return lead + "".join(parts) + tail


def _theme_rx(theme: dict, boundary: str) -> str:
    return "(" + "|".join(_stem_rx(p, boundary) for p in theme["patterns"]) + ")"


# Скомпилированные паттерны для пер-отзыв тегирования (Python-side, для сегментов
# drill-in и LLM-объяснений). Та же таксономия, что и в _theme_sql (SQL-агрегат).
_THEME_RE = [(t, re.compile(_theme_rx(t, r"\b"), re.I)) for t in THEMES]


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
    # \y — граница слова в POSIX-регэкспах Postgres (аналог \b в Python).
    return f'r."reviewBody" ~* :{k}', {k: _theme_rx(theme, r"\y")}


def _theme_tsquery(theme: dict) -> str:
    """Паттерны темы → tsquery для единого индекса.

    Индекс хранит tsvector, а не текст, поэтому regex по нему не пройдёт. Основы
    из словаря тем превращаются в префиксный поиск: «прокурат» → «прокурат:*».
    Многословные паттерны становятся фразой через оператор следования.

    Нужно ровно для тех метрик, что не переехали на выведенную таксономию —
    например доли эскалаций: темы «жалоба в ЦБ» в ней нет, а метрика нужна.
    """
    parts = []
    for p in theme["patterns"]:
        toks = [re.sub(r"[^0-9a-zA-Zа-яёА-ЯЁ]", "", t) for t in p.split()]
        toks = [t for t in toks if len(t) >= 2]
        if not toks:
            continue
        parts.append(" <-> ".join(f"{t}:*" for t in toks))
    return " | ".join(parts)


def _idx_clause(bc: str, product: str | None, alias: str = "i") -> tuple[str, dict]:
    """Общее условие «банк + продукт» для единого индекса."""
    cl = [f'{alias}.bank = :bank']
    p: dict = {"bank": bc}
    if product:
        cl.append(f'{alias}.product = :product')
        p["product"] = product
    # дата из будущего — дефект источника, в статистике ей делать нечего
    cl.append(f'({alias}.dt IS NULL OR {alias}.dt <= now())')
    return " AND ".join(cl), p


def _index_ready() -> bool:
    """Наполнен ли единый индекс. Пока нет — агрегаты считаются по-старому."""
    try:
        from . import bankiru_fts
        return bankiru_fts.is_ready()
    except Exception:
        return False


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
    """Список банков по объёму жалоб — для фильтра вкладки. Считается по единому
    индексу, поэтому банк, которого нет во внешнем корпусе, но который собрали
    наши коллекторы, тоже попадает в список.
    Сбер первым (даже если по объёму не №1), дальше по убыванию."""
    eng = _get_engine()

    def _compute():
        rows = None
        if _index_ready():
            try:
                with db.session() as s:
                    rows = s.execute(text(
                        "SELECT i.bank, count(*) n FROM review_index i"
                        " WHERE i.bank IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT :top"),
                        {"top": top}).all()
            except Exception as e:
                log.warning("reviews_dash.banks: индекс не сработал (%s)", e)
                rows = None
        if rows is None:
            if eng is None:
                return []
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
def corpus_stats(bank: str | None = None) -> dict | None:
    """Реальный состав корпуса: сколько отзывов, откуда и за какой период.

    Нужна подзаголовку вкладки. Раньше там стояла зашитая строка «негативные
    отзывы banki.ru (1-2 звезды)» — она перестала быть правдой, как только
    источников стало несколько, и никак не отражала объём. Числа берём из
    индекса, а не пишем руками: добавится источник — подпись обновится сама.
    """
    if not _index_ready():
        return None
    bc = resolve_bank(bank) if bank else None
    where, p = ("WHERE i.bank = :bank AND (i.dt IS NULL OR i.dt <= now())",
                {"bank": bc}) if bc else ("WHERE (i.dt IS NULL OR i.dt <= now())", {})
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.source, count(*) n, min(i.dt)::date, max(i.dt)::date,
                   count(*) FILTER (WHERE i.rating IS NOT NULL) rated,
                   round(avg(i.rating)::numeric, 2) avg_rating
            FROM review_index i {where}
            GROUP BY 1 ORDER BY 2 DESC
        """), p).mappings().all()
        # Размер всего корпуса — отдельно от чисел по выбранному банку. Одной
        # цифрой их показывать нельзя: «18 603 по 220 банкам» читается так,
        # будто 18 603 это весь корпус, хотя это только выбранный банк.
        banks_n, all_n = s.execute(text(
            "SELECT count(DISTINCT i.bank), count(*) FROM review_index i"
            " WHERE i.bank IS NOT NULL AND (i.dt IS NULL OR i.dt <= now())")).one()
    items = [{"source": _SOURCE_LABEL.get(r["source"], r["source"]),
              "key": r["source"], "n": int(r["n"]),
              "from": r["min"].isoformat() if r["min"] else None,
              "to": r["max"].isoformat() if r["max"] else None,
              "avg_rating": float(r["avg_rating"]) if r["avg_rating"] is not None else None}
             for r in rows]
    # площадки схлопываем по подписи: внешний корпус и наш коллектор banki.ru
    # для аудитора одна площадка, и показывать её двумя строками бессмысленно
    merged: dict[str, dict] = {}
    for it in items:
        m = merged.setdefault(it["source"], {"source": it["source"], "n": 0,
                                             "from": it["from"], "to": it["to"]})
        m["n"] += it["n"]
        if it["from"] and (not m["from"] or it["from"] < m["from"]):
            m["from"] = it["from"]
        if it["to"] and (not m["to"] or it["to"] > m["to"]):
            m["to"] = it["to"]
    out = sorted(merged.values(), key=lambda x: -x["n"])
    return {"bank": bc,
            # total — по ВЫБРАННОМУ банку; corpus_* — весь индекс
            "total": sum(x["n"] for x in out),
            "corpus_total": int(all_n or 0), "banks": int(banks_n or 0),
            "sources": out, "raw": items}


@_safe(None)
def overview(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute_index():
        """Обзор по ЕДИНОМУ индексу: числа включают все источники, а не только
        внешний корпус. Дедуп не нужен — url это первичный ключ индекса, поэтому
        count(*) вместо count(DISTINCT url), и это заметно дешевле."""
        idx, ip = _idx_clause(bc, product)
        with db.session() as s:
            cur = s.execute(text(f"""
                SELECT count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d)),
                       count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d2)
                                          AND i.dt <  now() - make_interval(days => :d))
                FROM review_index i WHERE {idx}
            """), {**ip, "d": days, "d2": days * 2}).one()
            total_cur, total_prev = int(cur[0]), int(cur[1])
            # признак посчитан при индексации: по tsvector его не выразить,
            # ограничителем в паттернах служат предлоги, а словарь их выбрасывает
            esc_cur = int(s.execute(text(f"""
                SELECT count(*) FROM review_index i
                WHERE {idx} AND i.dt >= now() - make_interval(days => :d) AND i.esc
            """), {**ip, "d": days}).scalar() or 0)
            mk = s.execute(text(
                "SELECT i.bank, count(*) n FROM review_index i"
                " WHERE i.dt >= now() - make_interval(days => :d)"
                " AND (i.dt IS NULL OR i.dt <= now())"
                + (" AND i.product = :product" if product else "") +
                " GROUP BY 1 ORDER BY 2 DESC"),
                {"d": days, **({"product": product} if product else {})}).all()
            asof = s.execute(text(
                f"SELECT max(i.dt) FROM review_index i WHERE {idx}"), ip).scalar()
            by_src = [{"source": r[0], "n": int(r[1])} for r in s.execute(text(
                f"SELECT i.source, count(*) FROM review_index i WHERE {idx}"
                f" GROUP BY 1 ORDER BY 2 DESC"), ip).all()]
        total_market = sum(int(r[1]) for r in mk) or 1
        delta = round(100.0 * (total_cur - total_prev) / total_prev, 1) if total_prev else None
        return {
            "bank": bc, "product": product, "days": days,
            "total": total_cur, "prev": total_prev, "delta_pct": delta,
            "delta_low_n": bool(total_prev and min(total_cur, total_prev) < 30),
            "market_share_pct": round(100.0 * total_cur / total_market, 1),
            "market_rank": next((i + 1 for i, r in enumerate(mk) if r[0] == bc), None),
            "market_banks": len(mk),
            "escalation_pct": round(100.0 * esc_cur / total_cur, 1) if total_cur else 0.0,
            "as_of": asof.date().isoformat() if asof else None,
            "by_source": by_src, "src": "index",
        }

    def _compute():
        if _index_ready():
            try:
                return _compute_index()
            except Exception as e:
                log.warning("reviews_dash.overview: индекс не сработал (%s) — считаю по корпусу", e)
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
        if _index_ready():
            try:
                idx, ip = _idx_clause(bc, product)
                with db.session() as s:
                    rows = s.execute(text(
                        f"SELECT to_char(date_trunc('month', i.dt), 'YYYY-MM') ym, count(*)"
                        f" FROM review_index i WHERE {idx}"
                        f" AND i.dt >= date_trunc('month', now()) - make_interval(months => :m)"
                        f" GROUP BY 1 ORDER BY 1"), {**ip, "m": months - 1}).all()
                    cur_ym = s.execute(text("SELECT to_char(now(),'YYYY-MM')")).scalar()
            except Exception as e:
                log.warning("reviews_dash.trend: индекс не сработал (%s)", e)
                rows = None
        else:
            rows = None
        if rows is None:
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
def _themes_from_labels(bc: str, product: str | None) -> dict | None:
    """Панель тем по СОХРАНЁННОЙ разметке вместо regex-скана по текстам.

    Разметку делает фоновый прогон (review_topics): темы выводит модель из
    корпуса, раскладывает по ним вектор. Здесь остаётся обычный GROUP BY — а
    было двадцать с лишним регэкспов по всем текстам рынка за 180 дней на каждое
    открытие вкладки.

    Всё нужное для среза (банк, продукт, дата) уже лежит в полнотекстовом
    зеркале, поэтому чужая база в агрегате не участвует вовсе.
    """
    from . import review_topics
    ver = review_topics.active_version()
    if not ver:
        return None
    params = {"bank": bc, "product": product, "ver": ver,
              "min": review_topics.MIN_Z, "rank": review_topics.RANK_CAP}
    try:
        with db.session() as s:
            rows = s.execute(text("""
                WITH dd AS (
                    SELECT f.url, f.dt FROM review_index f
                    WHERE f.bank = :bank
                      AND f.dt >= now() - make_interval(days => 180)
                      AND (CAST(:product AS text) IS NULL OR f.product = :product)
                ), lab AS (
                    SELECT l.topic_id, dd.dt
                    FROM review_topic_label l JOIN dd ON dd.url = l.url
                    WHERE l.z >= :min AND l.rn <= :rank
                )
                SELECT d.key, d.label, d.risk,
                       count(*) FILTER (WHERE lab.dt >= now() - make_interval(days => 90)) AS n,
                       count(*) FILTER (WHERE lab.dt <  now() - make_interval(days => 90)) AS p
                FROM lab JOIN review_topic_def d ON d.topic_id = lab.topic_id
                WHERE d.version = :ver
                GROUP BY d.key, d.label, d.risk
            """), params).mappings().all()
            tot, other = s.execute(text("""
                WITH dd AS (
                    SELECT f.url FROM review_index f
                    WHERE f.bank = :bank
                      AND f.dt >= now() - make_interval(days => 90)
                      AND (CAST(:product AS text) IS NULL OR f.product = :product)
                )
                SELECT count(*),
                       count(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM review_topic_label l
                           WHERE l.url = dd.url AND l.z >= :min AND l.rn <= :rank))
                FROM dd
            """), params).one()
    except Exception as e:
        log.warning("reviews_dash: агрегат по разметке не сработал (%s) — иду regex'ом", e)
        return None
    total = int(tot) or 1
    out = []
    for r in rows:
        n, p = int(r["n"]), int(r["p"])
        out.append({"key": r["key"], "label": r["label"], "risk": r["risk"], "n": n,
                    "pct": round(100.0 * n / total, 1),
                    "delta_pct": round(100.0 * (n - p) / p) if p else (None if n == 0 else 100)})
    out.sort(key=lambda x: x["n"], reverse=True)
    if other:
        out.append({"key": "other", "label": "Прочее / без темы", "risk": "other",
                    "n": int(other), "pct": round(100.0 * int(other) / total, 1),
                    "delta_pct": None})
    return {"bank": bc, "product": product, "days": 90, "total": total,
            "themes": out, "src": "labels"}


def themes(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        # Пока разметки нет (первый прогон ещё не отработал) — считаем как раньше.
        # Панель тем не должна пустеть из-за того, что фоновая задача не успела.
        if TOPICS_FROM_LABELS:
            byl = _themes_from_labels(bc, product)
            if byl and byl["themes"]:
                return byl
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
        rows = None
        if _index_ready():
            try:
                with db.session() as s:
                    rows = s.execute(text(
                        "SELECT i.bank, count(*) n FROM review_index i"
                        " WHERE i.dt >= now() - make_interval(days => :d)"
                        " AND (i.dt IS NULL OR i.dt <= now())"
                        + (" AND i.product = :product" if product else "") +
                        " GROUP BY 1 ORDER BY 2 DESC"),
                        {"d": days, **({"product": product} if product else {})}).all()
            except Exception as e:
                log.warning("reviews_dash.vs_market: индекс не сработал (%s)", e)
                rows = None
        if rows is None:
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
        rows = None
        if _index_ready():
            try:
                idx, ip = _idx_clause(bc, product)
                with db.session() as s:
                    # город заполнен не у всех источников — считаем по тем, где он
                    # есть, и не подмешиваем безгородные строки нулями
                    rows = s.execute(text(
                        f"SELECT i.city, count(*) n FROM review_index i"
                        f" WHERE {idx} AND i.city IS NOT NULL AND i.city <> ''"
                        f" AND i.dt >= now() - make_interval(days => :d)"
                        f" GROUP BY 1 ORDER BY 2 DESC LIMIT 40"), {**ip, "d": days}).all()
            except Exception as e:
                log.warning("reviews_dash.geo: индекс не сработал (%s)", e)
                rows = None
        if rows is None:
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
        rows = None
        if _index_ready():
            try:
                with db.session() as s:
                    rows = s.execute(text(
                        "SELECT i.product, count(*) n FROM review_index i"
                        " WHERE i.bank = :bank AND i.product IS NOT NULL"
                        " AND i.dt >= now() - make_interval(days => :d)"
                        " AND (i.dt IS NULL OR i.dt <= now())"
                        " GROUP BY 1 ORDER BY 2 DESC LIMIT :top"),
                        {"bank": bc, "d": days, "top": top}).all()
            except Exception as e:
                log.warning("reviews_dash.products: индекс не сработал (%s)", e)
                rows = None
        if rows is None:
            with eng.connect() as c:
                rows = c.execute(text(
                    'SELECT "product", count(DISTINCT r.url) n FROM bankiru.reviews r'
                    ' WHERE r."bankName" = :bank AND r."datePublished" >= now() - make_interval(days => :d)'
                    ' GROUP BY 1 ORDER BY 2 DESC LIMIT :top'),
                    {"bank": bc, "d": days, "top": top}).all()
        return {"bank": bc, "items": [{"product": r[0], "n": int(r[1])} for r in rows]}
    return _cached(f"pr:{bc}:{days}:{top}", _compute)


# Как показывать источник аудитору. Ключи приходят из конфига коллекторов;
# незнакомый ключ показываем как есть — это лучше, чем прятать происхождение.
_SOURCE_LABEL = {"bankiru": "banki.ru", "banki_reviews": "banki.ru",
                 "sravni_reviews": "sravni.ru", "bankiros_reviews": "bankiros.ru",
                 "finuslugi_reviews": "finuslugi.ru"}


def _feed_from_index(bc: str, product: str | None, theme: str | None,
                     days: int | None, city: str | None, month: str | None,
                     limit: int, offset: int = 0,
                     esc: bool = False) -> dict:
    """Лента по ЕДИНОМУ индексу — все источники в одном списке.

    Раньше лента читала только внешнюю базу banki.ru, поэтому отзывы, собранные
    нашими коллекторами, лежали в базе и были невидимы. Теперь источник — просто
    колонка, и аудитор видит их вперемешку по дате, как и просил: не отдельным
    блоком, а расширением того же списка.
    """
    # Берём с запасом на дедуп и на уже показанные страницы: аудитор жаловался,
    # что лента жёстко обрывается на двадцати обращениях и проверить фильтр по
    # периоду нечем.
    fetch = min(max((limit + offset) * 5, 40), 600)
    p: dict = {"bank": bc, "product": product, "lim": fetch}
    extra = ""
    if days:
        extra += " AND i.dt >= now() - make_interval(days => :d)"
        p["d"] = days
    if city:
        extra += " AND i.city = :city"
        p["city"] = city
    if month:
        extra += " AND date_trunc('month', i.dt) = to_date(:month, 'YYYY-MM')"
        p["month"] = month
    if esc:
        # Признак посчитан при индексации (миграция 031) и лежит под частичным
        # индексом — фильтр стоит один булев предикат. Аудиторы дважды просили
        # переход к самим обращениям по плашке регуляторной эскалации.
        extra += " AND i.esc"
    if theme:
        from . import review_topics
        ver = review_topics.active_version()
        if not ver:
            return {"items": [], "mode": "feed", "error": "unknown_theme"}
        extra += (" AND EXISTS (SELECT 1 FROM review_topic_label l"
                  " JOIN review_topic_def d ON d.topic_id = l.topic_id"
                  " WHERE l.url = i.url AND d.version = :ver AND d.key = :tkey"
                  " AND l.z >= :minz AND l.rn <= :rank)")
        p.update({"ver": ver, "tkey": theme, "minz": review_topics.MIN_Z,
                  "rank": review_topics.RANK_CAP})
    try:
        with db.session() as s:
            rows = [dict(r) for r in s.execute(text(f"""
                SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt,
                       i.city, i.rating
                FROM review_index i
                WHERE i.bank = :bank
                  -- дата из будущего это дефект источника, а не свежий отзыв:
                  -- такая строка навсегда встаёт первой в ленте
                  AND (i.dt IS NULL OR i.dt <= now())
                  AND (CAST(:product AS text) IS NULL OR i.product = :product){extra}
                ORDER BY i.dt DESC NULLS LAST
                LIMIT :lim
            """), p).mappings().all()]
    except Exception as e:
        log.warning("reviews_dash: лента по индексу не собралась (%s)", e)
        return {"items": [], "mode": "feed", "error": "feed_failed"}

    from . import bankiru_fts
    bodies = bankiru_fts.bodies_for(rows)
    seen: dict[str, int] = {}
    out: list[dict] = []
    for r in rows:
        b = bodies.get(r["url"]) or {}
        body = (b.get("text") or "").strip()
        if len(body) < 40:
            continue
        key = body[:100].lower()
        if key in seen:                     # массовость считаем, а не прячем
            out[seen[key]]["similar"] += 1
            continue
        seen[key] = len(out)
        dt = r["dt"]
        out.append({"bank": r["bank"], "product": r["product"] or b.get("product"),
                    "date": dt.date().isoformat() if dt else None,
                    "city": r["city"] or b.get("city"),
                    "url": r["url"], "text": body, "similar": 0,
                    "rating": float(r["rating"]) if r["rating"] is not None else None,
                    "source": r["source"],
                    "themes": []})
    page = out[offset:offset + limit]
    _attach_themes(page)
    return {"items": page, "mode": "feed", "error": None,
            "has_more": len(out) > offset + limit}


def _urls_by_topic(key: str, bank: str, product: str | None, *, days: int | None,
                   city: str | None, month: str | None, limit: int) -> list[str] | None:
    """Ссылки на отзывы темы из выведенной таксономии — по сохранённой разметке.

    Возвращает None, если такой темы в активном поколении нет: аудитор должен
    увидеть «тема не найдена», а не молча пустую ленту (её легко принять за
    «жалоб по теме нет»).
    """
    from . import review_topics
    ver = review_topics.active_version()
    if not ver:
        return None
    p = {"key": key, "ver": ver, "bank": bank, "product": product,
         "min": review_topics.MIN_Z, "rank": review_topics.RANK_CAP, "lim": int(limit)}
    extra = ""
    if days:
        extra += " AND f.dt >= now() - make_interval(days => :d)"
        p["d"] = days
    if city:
        extra += " AND f.city = :city"
        p["city"] = city
    if month:
        extra += " AND date_trunc('month', f.dt) = to_date(:month, 'YYYY-MM')"
        p["month"] = month
    try:
        with db.session() as s:
            if not s.execute(text(
                "SELECT 1 FROM review_topic_def WHERE version = :ver AND key = :key"),
                    p).first():
                return None
            return list(s.execute(text(f"""
                SELECT f.url FROM review_index f
                JOIN review_topic_label l ON l.url = f.url
                JOIN review_topic_def  d ON d.topic_id = l.topic_id
                WHERE d.version = :ver AND d.key = :key AND l.z >= :min AND l.rn <= :rank
                  AND f.bank = :bank
                  AND (CAST(:product AS text) IS NULL OR f.product = :product){extra}
                ORDER BY f.dt DESC LIMIT :lim
            """), p).scalars().all())
    except Exception as e:
        log.warning("reviews_dash: отбор по теме %r не сработал (%s)", key, e)
        return None


def _labels_for(urls: list[str]) -> dict[str, list[dict]]:
    """Темы показанных отзывов из разметки — одним запросом на всю страницу.

    Пер-отзыв regex-разметка (match_themes) остаётся фолбэком: пока фоновый
    прогон не отработал, чипы должны быть, пусть и прежние."""
    if not urls:
        return {}
    from . import review_topics  # noqa: F401 — READ_TOP используется ниже
    ver = review_topics.active_version()
    if not ver:
        return {}
    try:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT l.url, d.key, d.label, d.risk, l.score
                FROM review_topic_label l
                JOIN review_topic_def d ON d.topic_id = l.topic_id
                WHERE d.version = :ver AND l.z >= :min AND l.rn <= :rank AND l.url = ANY(:urls)
                ORDER BY l.url, l.z DESC
            """), {"ver": ver, "min": review_topics.MIN_Z, "rank": review_topics.RANK_CAP, "urls": urls}).mappings().all()
    except Exception as e:
        log.warning("reviews_dash: темы показанных отзывов не забрались (%s)", e)
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["url"], []).append(
            {"key": r["key"], "label": r["label"], "short": _short(r["label"]),
             "risk": r["risk"]})
    return out


def _attach_themes(items: list[dict]) -> None:
    """Темы и человекочитаемый источник — общий финиш для ленты и поиска.

    Обе ветки проходят здесь, поэтому и подпись источника ставится здесь: иначе
    лента показывала бы «banki.ru», а поиск по тем же данным — служебный ключ
    «bankiru», и аудитор считал бы их разными площадками."""
    for r in items:
        src = r.get("source")
        if src:
            r["source"] = _SOURCE_LABEL.get(src, src)
    from . import review_topics          # ленивый импорт: иначе кольцо модулей
    ready = review_topics.is_ready()
    by_url = _labels_for([i["url"] for i in items if i.get("url")])
    for r in items:
        lab = by_url.get(r.get("url") or "")
        if lab:
            r["themes"] = lab[:review_topics.READ_TOP]
            r["theme_src"] = "topics"
        elif ready:
            # Разметка есть, но этот отзыв ни к чему не отнесён — он и в панели
            # считается «Прочим». Показывать здесь regex-темы нельзя: в одном
            # списке оказались бы названия двух разных таксономий, а карточка
            # спорила бы с панелью. Пустой список фронт рисует как «Прочее».
            r["themes"] = []
            r["theme_src"] = "topics"
        else:
            r["themes"] = match_themes(r.get("text", ""))


def list_reviews(bank: str, product: str | None = None, theme: str | None = None,
                 q: str | None = None, days: int | None = None,
                 city: str | None = None, month: str | None = None,
                 limit: int = 20) -> list[dict]:
    """Лента доказательной базы. Тонкая обёртка над list_reviews_ex для тех
    вызывающих, кому нужен только список (сегменты, LLM-объяснения)."""
    # Только именованные: позиционный вызов уже однажды тихо съел период,
    # подставив None пятым аргументом, и любой новый параметр в середине
    # сигнатуры сдвинул бы весь хвост.
    return list_reviews_ex(bank, product=product, theme=theme, q=q, days=days,
                           city=city, month=month, limit=limit)["items"]


def _esc_urls(urls: list[str]) -> set[str]:
    """Какие из этих обращений несут признак регуляторной эскалации."""
    if not urls:
        return set()
    try:
        with db.session() as s:
            rows = s.execute(text(
                "SELECT url FROM review_index WHERE esc AND url = ANY(:u)"),
                {"u": urls}).scalars().all()
        return set(rows)
    except Exception as e:                                     # noqa: BLE001
        log.warning("reviews_dash: признак эскалации не забрался (%s)", e)
        return set(urls)      # фильтр не молчит: лучше не сузить, чем соврать


def list_reviews_ex(bank: str, product: str | None = None, theme: str | None = None,
                    q: str | None = None, days: int | None = None,
                    city: str | None = None, month: str | None = None,
                    limit: int = 20, offset: int = 0,
                    esc: bool = False) -> dict:
    """Лента доказательной базы. q → семантика; иначе свежие с фильтрами
    тема/город/месяц. Дубли (массовые однотипные жалобы) не прячем, а считаем —
    массовость это аудит-сигнал → поле `similar`.

    Возвращает {items, mode, error}. Признак error нужен вкладке: раньше упавший
    поиск и честное «ничего не нашлось» выглядели одинаково — пустым списком,
    и аудитор делал вывод, что жалоб по теме нет."""
    bc = resolve_bank(bank) if bank else None
    if q and q.strip():
        if bank and not bc:
            # банка нет в корпусе — это не «жалоб не нашлось», а другой ответ
            return {"items": [], "mode": "search", "error": "unknown_bank"}
        # тема/город/месяц раньше здесь терялись — теперь уезжают в сам поиск
        th = THEME_BY_KEY.get(theme or "")
        meta: dict = {}
        try:
            # Смещение отрабатываем срезом, а не SQL-OFFSET: дедуп массовых
            # жалоб идёт уже после выборки, и OFFSET по сырым строкам разъехался
            # бы со списком, который видит аудитор.
            # Просим на ОДНУ запись больше, чем покажем: поиск обрезает выдачу
            # ровно до k, и без этого запаса «есть ещё» было бы тождественно
            # ложным — кнопка догрузки не появилась бы никогда.
            res = search_reviews(q, bank=bc, product=product, since_days=days,
                                 theme_rx=_theme_rx(th, r"\y") if th else None,
                                 city=city, month=month, k=limit + offset + 1,
                                 strict=True, _meta=meta)
        except Exception as e:
            log.warning("reviews_dash: поиск по %r упал: %s", q, e)
            return {"items": [], "mode": "search", "error": "search_failed"}
        if esc:
            # Поиск идёт по внешнему корпусу, где признака эскалации нет: он
            # наш и посчитан при индексации. Поэтому отбираем после поиска по
            # нашему индексу — иначе фильтр молча пропадал бы, стоило аудитору
            # ввести запрос, ровно как раньше пропадал период.
            keep = _esc_urls([r.get("url") for r in res if r.get("url")])
            res = [r for r in res if r.get("url") in keep]
        page = res[offset:offset + limit]
        _attach_themes(page)
        return {"items": page, "mode": "search", "error": None, "search": meta,
                "has_more": len(res) > offset + limit}
    if not bc:
        return {"items": [], "mode": "feed", "error": "unknown_bank"}
    # Единый индекс — основной путь: в нём и внешний корпус, и наши коллекторы.
    # Старая ветка по внешней базе остаётся страховкой на случай, если индекс
    # ещё не наполнен (первый запуск) или его отключили рубильником.
    if FEED_FROM_INDEX:
        from . import bankiru_fts
        if bankiru_fts.is_ready():
            res = _feed_from_index(bc, product, theme, days, city, month,
                                   limit, offset, esc)
            # offset в условии обязателен: на последней странице список пуст и
            # ошибки нет, без этого запрос провалился бы в запасную ветку и
            # аудитор по кнопке «показать ещё» получил бы первую страницу снова.
            if res["items"] or res["error"] or offset:
                return res
    eng = _get_engine()
    if eng is None:
        return {"items": [], "mode": "feed", "error": None}
    bclause, bp = _bank_clause(bc, product)
    # тянем с запасом, чтобы счётчик «ещё N похожих» был осмысленным после дедупа
    fetch = min(max(limit * 5, 40), 120)
    params = {**bp, "lim": fetch}
    clause = ""
    # Тема из выведенной таксономии живёт разметкой в НАШЕЙ базе, а лента читает
    # чужую — join между ними невозможен. Поэтому сначала берём отобранные url
    # у себя, потом добираем по ним тексты: та же двухфазность, что и в
    # словесной ноге поиска. Ключи старой regex-таксономии продолжают работать
    # прежним путём, пока разметки нет.
    if theme and theme not in THEME_BY_KEY:
        urls = _urls_by_topic(theme, bc, product, days=days, city=city,
                              month=month, limit=fetch)
        if urls is None:
            return {"items": [], "mode": "feed", "error": "unknown_theme"}
        if not urls:
            return {"items": [], "mode": "feed", "error": None}
        clause += " AND r.url = ANY(:turls)"
        params["turls"] = urls
        theme = None                      # дальше по regex-ветке не идём
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
        return {"items": [], "mode": "feed", "error": "feed_failed"}
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
                    "themes": []})
    out = out[:limit]
    _attach_themes(out)
    return {"items": out, "mode": "feed", "error": None}


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


def _topic_week_counts(bank_canon: str | None, product: str | None):
    """Понедельные счётчики по темам LLM-таксономии из сохранённой разметки
    (review_index × review_topic_label) — сырьё сигналов главной. До 05.08.2026
    сигналы считались regex-темами THEMES и ТОЛЬКО по banki.ru: вкладка
    «Отзывы» уже жила на новой таксономии и всех источниках, а передовица — на
    старой. bank=None — рынок целиком. Возвращает (topics, counts) или None
    (нет таксономии/разметки → вызывающий падает на THEMES-путь):
      topics: [{key,label,risk}] активного поколения;
      counts: {<key>_w0/_w1/_b, _tw0, _tb, _lab_w0/_lab_b (размечено),
               _unc_w0/_unc_b (размечено, но ни одна тема не прошла порог)}.
    Слепая зона честная: неразмеченный ещё бэклог в неё не пишется — иначе
    отставание разметки выглядело бы как «вал жалоб вне тем»."""
    from . import review_topics
    ver = review_topics.active_version()
    if not ver:
        return None
    p = {"ver": ver, "min": review_topics.MIN_Z, "rank": review_topics.RANK_CAP,
         "bank": bank_canon, "product": product}
    try:
        with db.session() as s:
            topics = [dict(r) for r in s.execute(text(
                "SELECT key, label, risk FROM review_topic_def"
                " WHERE version = :ver ORDER BY topic_id"), p).mappings()]
            if not topics:
                return None
            rows = s.execute(text("""
                WITH dd AS (
                    SELECT i.url, i.dt FROM review_index i
                    WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)
                      AND (CAST(:product AS text) IS NULL OR i.product = :product)
                      AND i.dt >= now() - make_interval(days => 63)
                      AND i.dt <= now())
                SELECT d.key,
                       count(*) FILTER (WHERE dd.dt >= now()-make_interval(days=>7)) AS w0,
                       count(*) FILTER (WHERE dd.dt <  now()-make_interval(days=>7)
                                          AND dd.dt >= now()-make_interval(days=>14)) AS w1,
                       count(*) FILTER (WHERE dd.dt <  now()-make_interval(days=>14)) AS b
                FROM dd
                JOIN review_topic_label l ON l.url = dd.url
                     AND l.z >= :min AND l.rn <= :rank
                JOIN review_topic_def d ON d.topic_id = l.topic_id AND d.version = :ver
                GROUP BY d.key
            """), p).mappings().all()
            tot = s.execute(text("""
                WITH dd AS (
                    SELECT i.dt,
                           EXISTS (SELECT 1 FROM review_topic_label l
                                   WHERE l.url = i.url) AS lab,
                           EXISTS (SELECT 1 FROM review_topic_label l
                                   JOIN review_topic_def d ON d.topic_id = l.topic_id
                                        AND d.version = :ver
                                   WHERE l.url = i.url
                                     AND l.z >= :min AND l.rn <= :rank) AS clf
                    FROM review_index i
                    WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)
                      AND (CAST(:product AS text) IS NULL OR i.product = :product)
                      AND i.dt >= now() - make_interval(days => 63)
                      AND i.dt <= now())
                SELECT count(*) FILTER (WHERE dt >= now()-make_interval(days=>7)),
                       count(*) FILTER (WHERE dt <  now()-make_interval(days=>14)),
                       count(*) FILTER (WHERE dt >= now()-make_interval(days=>7) AND lab),
                       count(*) FILTER (WHERE dt <  now()-make_interval(days=>14) AND lab),
                       count(*) FILTER (WHERE dt >= now()-make_interval(days=>7)
                                          AND lab AND NOT clf),
                       count(*) FILTER (WHERE dt <  now()-make_interval(days=>14)
                                          AND lab AND NOT clf)
                FROM dd
            """), p).one()
    except Exception as e:  # noqa: BLE001
        log.warning("topic_week_counts: %s — фолбэк на regex-темы", e)
        return None
    counts: dict = {}
    for t in topics:
        for sfx in ("w0", "w1", "b"):
            counts[f'{t["key"]}_{sfx}'] = 0
    for r in rows:
        counts[f'{r["key"]}_w0'] = int(r["w0"])
        counts[f'{r["key"]}_w1'] = int(r["w1"])
        counts[f'{r["key"]}_b'] = int(r["b"])
    counts.update({"_tw0": int(tot[0]), "_tb": int(tot[1]),
                   "_lab_w0": int(tot[2]), "_lab_b": int(tot[3]),
                   "_unc_w0": int(tot[4]), "_unc_b": int(tot[5])})
    return topics, counts


@_safe(None)
def top_topic(bank: str, product: str | None, days: int = 90) -> dict | None:
    """Ведущая тема жалоб по продукту из label-таксономии + momentum к прошлому
    окну. Для стат-карт «Для вас»: раньше «горячая тема» искалась через
    _THEME_SLUG по СТАРЫМ regex-ключам (deposit/mortgage/transfer) — после
    перехода на LLM-таксономию лукап молча возвращал пусто (аудит 05.08.2026)."""
    from . import review_topics
    ver = review_topics.active_version()
    bc = resolve_bank(bank)
    if not ver or not bc:
        return None
    p = {"ver": ver, "min": review_topics.MIN_Z, "rank": review_topics.RANK_CAP,
         "bank": bc, "product": product, "days": days}
    with db.session() as s:
        row = s.execute(text("""
            WITH dd AS (
                SELECT i.url, i.dt FROM review_index i
                WHERE i.bank = :bank
                  AND (CAST(:product AS text) IS NULL OR i.product = :product)
                  AND i.dt >= now() - make_interval(days => :days * 2)
                  AND i.dt <= now())
            SELECT d.key, d.label, d.risk,
                   count(*) FILTER (WHERE dd.dt >= now() - make_interval(days => :days)) AS n,
                   count(*) FILTER (WHERE dd.dt <  now() - make_interval(days => :days)) AS p
            FROM dd
            JOIN review_topic_label l ON l.url = dd.url
                 AND l.z >= :min AND l.rn <= :rank
            JOIN review_topic_def d ON d.topic_id = l.topic_id AND d.version = :ver
            GROUP BY d.key, d.label, d.risk
            ORDER BY n DESC LIMIT 1
        """), p).mappings().first()
    if not row or not int(row["n"]):
        return None
    n, prev = int(row["n"]), int(row["p"])
    return {"key": row["key"], "label": row["label"], "risk": row["risk"], "n": n,
            "delta_pct": (round(100.0 * (n - prev) / prev) if prev else None)}


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
def week_pulse(bank: str, product: str | None = None) -> dict | None:
    """Недельный срез для «пульса дня» на главной — БЕЗ порога сигнала.

    Сигналы (weekly_signals) показывают только пробившие порог ×1.8, и в
    спокойный день их ноль — полоса пустеет. Здесь считаем то, что есть всегда:
      • расхождение с рынком по каждой теме (наша динамика против отраслевой) —
        главный вопрос аудитора «это мы или рынок»;
      • слепая зона: жалобы, не попавшие ни в одну из тем классификатора.
    Оба числа берутся из уже выполняемых запросов, лишней нагрузки нет.
    """
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None
    lab = _topic_week_counts(bc, product)
    if lab:
        theme_defs, brow = lab
        mlab = _topic_week_counts(None, product)     # рынок = все банки индекса
        mrow = mlab[1] if mlab else None
    else:                       # разметки ещё нет — старый regex-путь по banki.ru
        theme_defs = THEMES
        bclause, bp = _bank_clause(bc, product)
        brow = _theme_week_counts(eng, bclause, bp)
        try:
            mrow = _theme_week_counts(eng, "TRUE", {})
        except Exception as e:  # noqa: BLE001
            log.warning("week_pulse: рыночный срез не посчитан: %s", e)
            mrow = None

    BASE_W = 7.0
    diverge = []
    for t in theme_defs:
        k = t["key"]
        w0, b = int(brow[f"{k}_w0"]), int(brow[f"{k}_b"])
        bw = b / BASE_W
        if w0 < 5 or bw < 1.0:          # малая база — коэффициент неустойчив
            continue
        ratio = w0 / bw
        mratio = None
        if mrow is not None:
            mw0, mb = int(mrow[f"{k}_w0"]), int(mrow[f"{k}_b"])
            mbw = mb / BASE_W
            mratio = (mw0 / mbw) if mbw >= 0.5 else None
        # расхождение = во сколько раз наш рост обгоняет рыночный
        gap = (ratio / mratio) if mratio and mratio > 0 else None
        diverge.append({
            "key": k, "label": t["label"], "short": _short(t["label"]),
            "risk": t["risk"], "week": w0, "baseline_week": round(bw, 1),
            "base_count": b, "base_weeks": int(BASE_W),
            "ratio": round(ratio, 2),
            "market_ratio": round(mratio, 2) if mratio else None,
            "gap": round(gap, 2) if gap else None,
        })
    # ведущая тема: сначала по расхождению с рынком, при равенстве — по объёму
    diverge.sort(key=lambda d: ((d["gap"] or d["ratio"]), d["week"]), reverse=True)

    tw0, tb = int(brow["_tw0"]), int(brow["_tb"])
    return {
        "diverge": diverge[:5],
        "week_total": tw0,
        "baseline_total": round(tb / BASE_W, 1),
        **({"src": "labels"} if lab else {}),
    }


@_safe(None)
def unclassified_week(bank: str, product: str | None = None) -> dict | None:
    """Слепая зона: жалобы недели, не попавшие ни в одну тему классификатора,
    и та же доля по базовому окну — чтобы отличить «свежий инцидент вне
    таксономии» от обычного уровня непокрытия."""
    eng = _get_engine()
    if eng is None:
        return None
    bc = resolve_bank(bank)
    if not bc:
        return None
    lab = _topic_week_counts(bc, product)
    if lab:
        # слепая зона НОВОГО классификатора: отзыв размечен, но ни одна тема не
        # прошла порог z/rn; неразмеченный бэклог в знаменатель не входит
        _t, c_ = lab
        w_unc, w_tot, b_unc = c_["_unc_w0"], c_["_lab_w0"], c_["_unc_b"]
        base_week = round(b_unc / 7.0, 1)
        return {"week": w_unc, "week_total": w_tot,
                "pct": round(100 * w_unc / w_tot) if w_tot else 0,
                "baseline_week": base_week,
                "ratio": round(w_unc / base_week, 2) if base_week >= 1 else None,
                "src": "labels"}
    bclause, bp = _bank_clause(bc, product)
    with eng.connect() as c:
        rows = c.execute(text(
            f'SELECT DISTINCT ON (r.url) r."reviewBody" AS body, r."datePublished" AS dt'
            f' FROM bankiru.reviews r WHERE {bclause}'
            f' AND r."datePublished" >= now() - make_interval(days => 63)'
            f' ORDER BY r.url'), bp).all()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    w_edge, b_edge = now - timedelta(days=7), now - timedelta(days=14)
    w_tot = w_unc = b_tot = b_unc = 0
    for body, dt in rows:
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        unmatched = not match_themes(body)
        if dt >= w_edge:
            w_tot += 1
            w_unc += unmatched
        elif dt < b_edge:
            b_tot += 1
            b_unc += unmatched
    base_week = round(b_unc / 7.0, 1)
    return {
        "week": w_unc, "week_total": w_tot,
        "pct": round(100 * w_unc / w_tot) if w_tot else 0,
        "baseline_week": base_week,
        "ratio": round(w_unc / base_week, 2) if base_week >= 1 else None,
    }


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
        lab = _topic_week_counts(bc, product)
        if lab:
            theme_defs, brow = lab
            mlab = _topic_week_counts(None, product)   # рынок = все банки индекса
            mrow = mlab[1] if mlab else None
        else:                   # разметки ещё нет — старый regex-путь по banki.ru
            theme_defs = THEMES
            bclause, bp = _bank_clause(bc, product)
            brow = _theme_week_counts(eng, bclause, bp)
            try:
                mrow = _theme_week_counts(eng, "TRUE", {})     # рынок (все банки)
            except Exception as e:
                log.warning("weekly_signals: рыночный срез не посчитан: %s", e)
                mrow = None
        out = []
        for t in theme_defs:
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
                if lab:
                    from . import review_topics as _rt
                    with db.session() as s:
                        grows = s.execute(text("""
                            SELECT split_part(i.city, ' (', 1) AS city,
                                   count(DISTINCT i.url) AS n
                            FROM review_index i
                            JOIN review_topic_label l ON l.url = i.url
                                 AND l.z >= :min AND l.rn <= :rank
                            JOIN review_topic_def d ON d.topic_id = l.topic_id
                                 AND d.version = :ver AND d.key = :key
                            WHERE i.bank = :bank
                              AND (CAST(:product AS text) IS NULL OR i.product = :product)
                              AND i.dt >= now() - make_interval(days => 7)
                              AND i.dt <= now() AND coalesce(i.city, '') <> ''
                            GROUP BY 1 ORDER BY 2 DESC LIMIT 3
                        """), {"min": _rt.MIN_Z, "rank": _rt.RANK_CAP,
                               "ver": _rt.active_version(), "key": top["key"],
                               "bank": bc, "product": product}).all()
                else:
                    bclause, bp = _bank_clause(bc, product)
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
        return {"bank": bc, "product": product, "signals": out[:6], "overall": overall,
                **({"src": "labels"} if lab else {})}
    return _cached(f"wk:{bc}:{product}", _compute, ttl=1800)
