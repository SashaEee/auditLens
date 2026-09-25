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
from .bankiru_reviews import resolve_bank, search_reviews
from . import review_codebook as cb

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


# ── Разметка ────────────────────────────────────────────────────────────────
# Все счётчики вкладки, сигналов и обзора считают ЖАЛОБЫ из LLM-разметки
# (rag/review_annotate): отзыв, который модель отнесла к жалобам, в т. ч.
# смешанный. Неразмеченное (kind IS NULL), похвала, вопросы, мусор и копии
# одного отзыва в счёт не входят.
_CMP = "i.kind IN ('complaint', 'mixed')"


def _ann_schema() -> str:
    from .review_annotate import SCHEMA
    return SCHEMA


def _coverage(bank_canon: str | None, days: int) -> float | None:
    """Доля отзывов окна, у которых уже есть разметка (в процентах)."""
    try:
        with db.session() as s:
            n, lab = s.execute(text(
                "SELECT count(*), count(*) FILTER (WHERE i.kind IS NOT NULL) FROM review_index i"
                " WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)"
                " AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"),
                {"bank": bank_canon, "d": days}).one()
        return round(100.0 * lab / n, 1) if n else None
    except Exception:  # noqa: BLE001
        return None


def _prev_ready(bank_canon: str | None, days: int) -> bool:
    """Размечено ли прошлое окно [2·days, days) целиком (≥97%).

    Массовая разметка идёт от свежих отзывов к старым: пока прошлый период
    размечен наполовину, в нём «не хватает» жалоб, и сравнение даёт ложный
    рост (+170% вместо реальных единиц процентов). Такое сравнение не
    показываем вовсе."""
    try:
        with db.session() as s:
            n, lab = s.execute(text(
                "SELECT count(*), count(*) FILTER (WHERE i.kind IS NOT NULL) FROM review_index i"
                " WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)"
                " AND i.dt >= now() - make_interval(days => :d2)"
                " AND i.dt <  now() - make_interval(days => :d)"),
                {"bank": bank_canon, "d": days, "d2": days * 2}).one()
        return bool(n) and lab >= 0.97 * n
    except Exception:  # noqa: BLE001
        return False


# ── Агрегаты ────────────────────────────────────────────────────────────────
@_safe([])
def banks(top: int = 120) -> list[dict]:
    """Список банков для фильтра вкладки — по жалобам ЗА ГОД, не за всё время:
    иначе в списке висели закрытые банки («Рокетбанк»). Считается по единому
    индексу, поэтому банк, которого нет во внешнем корпусе, но который собрали
    наши коллекторы, тоже попадает в список. last — дата последней жалобы:
    у банка, пропавшего с площадки («Почта Банк» с мая 2026), фронт это покажет.
    Сбер первым (даже если по объёму не №1), дальше по убыванию."""
    def _compute():
        with db.session() as s:
            rows = s.execute(text(
                "SELECT i.bank, count(*) n, max(i.dt)::date FROM review_index i"
                f" WHERE i.bank IS NOT NULL AND {_CMP}"
                " AND i.dt > now() - interval '365 days' AND i.dt <= now()"
                " GROUP BY 1 HAVING count(*) >= 20 ORDER BY 2 DESC LIMIT :top"), {"top": top}).all()
        items = [{"bank": r[0], "n": int(r[1]), "last": r[2].isoformat() if r[2] else None}
                 for r in rows]
        sber = [x for x in items if x["bank"] == "Сбербанк"]
        rest = [x for x in items if x["bank"] != "Сбербанк"]
        return sber + rest
    return _cached(f"banks:{top}", _compute, ttl=6 * 3600)


@_safe(None)
def corpus_stats(bank: str | None = None) -> dict | None:
    """Реальный состав корпуса: сколько отзывов, откуда и за какой период.

    Мусор (служебная разметка вместо текста) и копии одного отзыва, собранные
    двумя путями, в состав не входят — это не отзывы. Похвала и вопросы входят:
    это настоящие отзывы, просто не жалобы (жалобы считает overview)."""
    if not _index_ready():
        return None
    bc = resolve_bank(bank) if bank else None
    real = "coalesce(i.kind, '') NOT IN ('junk', 'dup')"
    where, p = ((f"WHERE i.bank = :bank AND (i.dt IS NULL OR i.dt <= now()) AND {real}",
                 {"bank": bc}) if bc else
                (f"WHERE (i.dt IS NULL OR i.dt <= now()) AND {real}", {}))
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.source, count(*) n, min(i.dt)::date, max(i.dt)::date,
                   count(*) FILTER (WHERE i.rating IS NOT NULL) rated,
                   round(avg(i.rating)::numeric, 2) avg_rating,
                   count(*) FILTER (WHERE {_CMP}) complaints
            FROM review_index i {where}
            GROUP BY 1 ORDER BY 2 DESC
        """), p).mappings().all()
        banks_n, all_n = s.execute(text(
            "SELECT count(DISTINCT i.bank), count(*) FROM review_index i"
            f" WHERE i.bank IS NOT NULL AND (i.dt IS NULL OR i.dt <= now()) AND {real}")).one()
    items = [{"source": _SOURCE_LABEL.get(r["source"], r["source"]),
              "key": r["source"], "n": int(r["n"]), "complaints": int(r["complaints"]),
              "from": r["min"].isoformat() if r["min"] else None,
              "to": r["max"].isoformat() if r["max"] else None,
              "avg_rating": float(r["avg_rating"]) if r["avg_rating"] is not None else None}
             for r in rows]
    merged: dict[str, dict] = {}
    for it in items:
        m = merged.setdefault(it["source"], {"source": it["source"], "n": 0, "complaints": 0,
                                             "from": it["from"], "to": it["to"]})
        m["n"] += it["n"]
        m["complaints"] += it["complaints"]
        if it["from"] and (not m["from"] or it["from"] < m["from"]):
            m["from"] = it["from"]
        if it["to"] and (not m["to"] or it["to"] > m["to"]):
            m["to"] = it["to"]
    out = sorted(merged.values(), key=lambda x: -x["n"])
    return {"bank": bc,
            "total": sum(x["n"] for x in out),
            "complaints": sum(x["complaints"] for x in out),
            "corpus_total": int(all_n or 0), "banks": int(banks_n or 0),
            "sources": out, "raw": items}


@_safe(None)
def overview(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    """KPI вкладки по жалобам из LLM-разметки.

    Жалоба — отзыв, который модель отнесла к жалобам (в т. ч. смешанный:
    претензия и похвала вместе). Похвала, вопросы, мусор и копии одного отзыва
    в счёт не идут — раньше они давали до трети «роста». Эскалация — признак
    разметки («грозит» или «уже обратился» в ЦБ, суд, прокуратуру и т. п.), а
    не регулярка по словам «в суд»: та ошибалась в половине случаев."""
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        idx, ip = _idx_clause(bc, product)
        with db.session() as s:
            cur = s.execute(text(f"""
                SELECT count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d)),
                       count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d2)
                                          AND i.dt <  now() - make_interval(days => :d)),
                       count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d) AND i.esc)
                FROM review_index i WHERE {idx} AND {_CMP}
            """), {**ip, "d": days, "d2": days * 2}).one()
            total_cur, total_prev, esc_cur = int(cur[0]), int(cur[1]), int(cur[2])
            filed = int(s.execute(text(f"""
                SELECT count(*) FROM review_index i
                JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
                WHERE {idx} AND {_CMP} AND i.dt >= now() - make_interval(days => :d)
                  AND a.esc = 'filed'
            """), {**ip, "d": days, "sv": _ann_schema()}).scalar() or 0)
            mk = s.execute(text(
                "SELECT i.bank, count(*) n FROM review_index i"
                " WHERE i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
                f" AND {_CMP}"
                + (" AND i.product = :product" if product else "") +
                " GROUP BY 1 ORDER BY 2 DESC"),
                {"d": days, **({"product": product} if product else {})}).all()
            asof = s.execute(text(
                f"SELECT max(i.dt) FROM review_index i WHERE {idx} AND {_CMP}"), ip).scalar()
            by_src = [{"source": r[0], "n": int(r[1])} for r in s.execute(text(
                f"SELECT i.source, count(*) FROM review_index i WHERE {idx} AND {_CMP}"
                f" AND i.dt >= now() - make_interval(days => :d)"
                f" GROUP BY 1 ORDER BY 2 DESC"), {**ip, "d": days}).all()]
        total_market = sum(int(r[1]) for r in mk) or 1
        ready = _prev_ready(bc, days)
        delta = (round(100.0 * (total_cur - total_prev) / total_prev, 1)
                 if total_prev and ready else None)
        return {
            "bank": bc, "product": product, "days": days,
            "total": total_cur, "prev": total_prev, "delta_pct": delta,
            "delta_low_n": bool(total_prev and min(total_cur, total_prev) < 30),
            "delta_partial": not ready,
            "market_share_pct": round(100.0 * total_cur / total_market, 1),
            "market_rank": next((i + 1 for i, r in enumerate(mk) if r[0] == bc), None),
            "market_banks": len(mk),
            "escalation_pct": round(100.0 * esc_cur / total_cur, 1) if total_cur else 0.0,
            "escalation_filed_pct": round(100.0 * filed / total_cur, 1) if total_cur else 0.0,
            "as_of": asof.date().isoformat() if asof else None,
            "by_source": by_src, "src": "annotation",
            "coverage": _coverage(bc, days * 2),
        }
    return _cached(f"ov:{bc}:{product}:{days}", _compute)


@_safe(None)
def trend(bank: str, product: str | None = None, months: int = 14) -> dict | None:
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        idx, ip = _idx_clause(bc, product)
        with db.session() as s:
            # Месяц, где разметка ещё не закончена (массовый прогон идёт от
            # свежих к старым), помечается неполным — как текущий: его столбик
            # занижен, и в базовую линию он не входит.
            rows = s.execute(text(
                f"SELECT to_char(date_trunc('month', i.dt), 'YYYY-MM') ym,"
                f"       count(*) FILTER (WHERE {_CMP}),"
                f"       count(*) FILTER (WHERE i.kind IS NOT NULL),"
                f"       count(*)"
                f" FROM review_index i WHERE {idx}"
                f" AND i.dt >= date_trunc('month', now()) - make_interval(months => :m) AND i.dt <= now()"
                f" GROUP BY 1 ORDER BY 1"), {**ip, "m": months - 1}).all()
            cur_ym = s.execute(text("SELECT to_char(now(),'YYYY-MM')")).scalar()
        series = []
        for ym, n, lab, tot in rows:
            cov = (lab / tot) if tot else 1.0
            series.append({"ym": ym, "n": int(n), "partial": ym == cur_ym or cov < 0.97,
                           **({"labeled_pct": round(100 * cov)} if cov < 0.97 else {})})
        complete = [s["n"] for s in series if not s["partial"]]
        med = None
        if len(complete) >= 4:
            med = _median(complete)
            mad = _median([abs(v - med) for v in complete]) or (
                sum(abs(v - med) for v in complete) / len(complete))
            thr = med + 2.0 * mad
            for s in series:
                s["pct_vs_median"] = round(100.0 * (s["n"] - med) / med) if med else 0
                s["spike"] = (not s["partial"]) and s["n"] > thr and s["n"] >= med * 1.4
        return {"bank": bc, "product": product, "series": series, "baseline": med}
    return _cached(f"tr:{bc}:{product}:{months}", _compute)




@_safe(None)
def themes(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    """Риск-карта: распределение жалоб по ГЛАВНОЙ проблеме из LLM-разметки.

    У каждой жалобы ровно одна главная проблема, поэтому доли в сумме дают
    100%: прежняя векторная разметка давала отзыву до двух тем, вторая была
    неверна в половине случаев, и сумма долей переваливала за сотню. Где
    проблема упомянута как дополнительная — отдельным числом «ещё в N»."""
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        p = {"bank": bc, "product": product, "d": days, "d2": days * 2}
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT i.issue,
                       count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d)) n,
                       count(*) FILTER (WHERE i.dt <  now() - make_interval(days => :d)) p
                FROM review_index i
                WHERE i.bank = :bank AND {_CMP}
                  AND i.dt >= now() - make_interval(days => :d2) AND i.dt <= now()
                  AND (CAST(:product AS text) IS NULL OR i.product = :product)
                GROUP BY 1"""), p).all()
            also = dict(s.execute(text(f"""
                SELECT x, count(*) FROM review_index i, unnest(i.issues2) x
                WHERE i.bank = :bank AND {_CMP}
                  AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()
                  AND (CAST(:product AS text) IS NULL OR i.product = :product)
                GROUP BY 1"""), p).all())
        total = sum(int(r[1]) for r in rows) or 1
        ready = _prev_ready(bc, days)
        out = []
        for code, n, prev in rows:
            o = cb.issue_obj(code)
            if not o or code == "no_issue":
                continue
            n, prev = int(n), int(prev)
            if not n and not prev:
                continue
            out.append({**o, "n": n, "pct": round(100.0 * n / total, 1),
                        "n_also": int(also.get(code, 0)),
                        "delta_pct": (None if not ready else
                                      round(100.0 * (n - prev) / prev) if prev else (None if n == 0 else 100))})
        out.sort(key=lambda x: (x["key"] == "other", -x["n"]))
        return {"bank": bc, "product": product, "days": days, "total": total,
                "themes": out, "src": "annotation", "coverage": _coverage(bc, days * 2),
                "delta_partial": not ready}
    return _cached(f"th:{bc}:{product}:{days}", _compute)


@_safe(None)
def vs_market(bank: str, product: str | None = None, days: int = 90, top: int = 8) -> dict | None:
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        with db.session() as s:
            rows = s.execute(text(
                "SELECT i.bank, count(*) n FROM review_index i"
                " WHERE i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
                f" AND {_CMP}"
                + (" AND i.product = :product" if product else "") +
                " GROUP BY 1 ORDER BY 2 DESC"),
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
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        idx, ip = _idx_clause(bc, product)
        with db.session() as s:
            rows = s.execute(text(
                f"SELECT i.city, count(*) n FROM review_index i"
                f" WHERE {idx} AND {_CMP} AND i.city IS NOT NULL AND i.city <> ''"
                f" AND i.dt >= now() - make_interval(days => :d)"
                f" GROUP BY 1 ORDER BY 2 DESC LIMIT 40"), {**ip, "d": days}).all()
        cities = []
        for city, n in rows:
            n = int(n)
            pop = _POP.get(city.strip().lower().replace("ё", "е"))
            per100k = round(n / (pop / 100.0), 1) if pop else None
            cities.append({"city": city, "n": n, "per_100k": per100k})
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
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        with db.session() as s:
            rows = s.execute(text(
                "SELECT i.product, count(*) n FROM review_index i"
                f" WHERE i.bank = :bank AND i.product IS NOT NULL AND {_CMP}"
                " AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
                " GROUP BY 1 ORDER BY 2 DESC LIMIT :top"),
                {"bank": bc, "d": days, "top": top}).all()
        return {"bank": bc, "items": [{"product": r[0], "n": int(r[1])} for r in rows]}
    return _cached(f"pr:{bc}:{days}:{top}", _compute)


# Как показывать источник аудитору. Ключи приходят из конфига коллекторов;
# незнакомый ключ показываем как есть — это лучше, чем прятать происхождение.
_SOURCE_LABEL = {"bankiru": "banki.ru", "banki_reviews": "banki.ru",
                 "sravni_reviews": "sravni.ru", "bankiros_reviews": "bankiros.ru",
                 "finuslugi_reviews": "finuslugi.ru"}


_ESC_RU = {"none": "", "threat": "грозит", "filed": "обратился"}


def export_rows(bank: str, product: str | None = None, theme: str | None = None,
                days: int | None = None, city: str | None = None,
                month: str | None = None, esc: bool = False,
                limit: int = 10000) -> list[dict] | None:
    """Жалобы с разметкой для выгрузки в таблицу — те же фильтры, что у ленты
    (без поиска по смыслу: он возвращает топ-300 похожих, а выгрузка — это
    полный срез). Раньше такой срез собирали вручную по запросу коллег."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    if theme and theme not in cb.ISSUES:
        return []
    p: dict = {"bank": bc, "product": product, "lim": max(1, min(limit, 20000)),
               "sv": _ann_schema()}
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
        extra += " AND i.esc"
    if theme:
        extra += " AND i.issue = :tkey"
        p["tkey"] = theme
    with db.session() as s:
        rows = [dict(r) for r in s.execute(text(f"""
            SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt, i.city, i.rating,
                   i.issue, i.issues2, a.kind, a.esc, a.esc_to, a.no_consent, a.misled,
                   a.vulnerable, a.amount, a.event_date, a.code_fit, a.new_topic,
                   a.summary, a.quote
            FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE i.bank = :bank AND {_CMP}
              AND (i.dt IS NULL OR i.dt <= now())
              AND (CAST(:product AS text) IS NULL OR i.product = :product){extra}
            ORDER BY i.dt DESC NULLS LAST
            LIMIT :lim
        """), p).mappings().all()]
    from . import bankiru_fts
    bodies = {}
    for j in range(0, len(rows), 1000):
        bodies.update(bankiru_fts.bodies_for(rows[j:j + 1000]))
    out = []
    for r in rows:
        o = cb.issue_obj(r["issue"]) or {}
        out.append({
            "дата": r["dt"].date().isoformat() if r["dt"] else "",
            "банк": r["bank"], "площадка": r["source"] or "bankiru",
            "город": r["city"] or "", "оценка": r["rating"] if r["rating"] is not None else "",
            "продукт": r["product"] or "",
            "главная проблема": o.get("label") or r["issue"] or "",
            "группа": o.get("group_label") or "",
            "доп. проблемы": "; ".join((cb.issue_obj(x) or {}).get("label") or x
                                       for x in (r["issues2"] or [])),
            "эскалация": _ESC_RU.get(r["esc"] or "none", r["esc"] or ""),
            "куда": ", ".join(r["esc_to"] or []),
            "без согласия": "да" if r["no_consent"] else "",
            "ввели в заблуждение": "да" if r["misled"] else "",
            "уязвимый клиент": ", ".join(r["vulnerable"] or []),
            "сумма": r["amount"] if r["amount"] is not None else "",
            "дата события": r["event_date"] or "",
            "вне кодификатора": (r["new_topic"] or "") if r["code_fit"] != "exact" else "",
            "суть": r["summary"] or "", "цитата": r["quote"] or "",
            "ссылка": r["url"],
            "текст": (bodies.get(r["url"]) or {}).get("text") or "",
        })
    return out


def _feed_from_index(bc: str, product: str | None, theme: str | None,
                     days: int | None, city: str | None, month: str | None,
                     limit: int, offset: int = 0,
                     esc: bool = False) -> dict:
    """Лента по ЕДИНОМУ индексу — все источники в одном списке.

    Показываются жалобы из разметки и ещё не размеченные свежие отзывы (они
    размечаются в течение часа — прятать самые свежие нельзя). Похвала,
    вопросы, мусор и копии в ленту жалоб не идут. Фильтр по теме — по главной
    проблеме, как и счётчик риск-карты: клик по строке показывает ровно те
    жалобы, что в ней посчитаны."""
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
        extra += " AND i.esc"
    if theme:
        if theme not in cb.ISSUES:
            return {"items": [], "mode": "feed", "error": "unknown_theme"}
        extra += f" AND i.issue = :tkey AND {_CMP}"
        p["tkey"] = theme
    else:
        extra += f" AND (i.kind IS NULL OR {_CMP})"
    try:
        with db.session() as s:
            rows = [dict(r) for r in s.execute(text(f"""
                SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt,
                       i.city, i.rating
                FROM review_index i
                WHERE i.bank = :bank
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
        out.append({"bank": r["bank"], "product": r["product"],
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
    """Ссылки на жалобы с этой главной проблемой. None — такого кода нет:
    аудитор должен увидеть «тема не найдена», а не пустую ленту."""
    if key not in cb.ISSUES:
        return None
    p = {"key": key, "bank": bank, "product": product, "lim": int(limit)}
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
    with db.session() as s:
        return list(s.execute(text(f"""
            SELECT f.url FROM review_index f
            WHERE f.issue = :key AND f.kind IN ('complaint', 'mixed') AND f.bank = :bank
              AND (CAST(:product AS text) IS NULL OR f.product = :product){extra}
            ORDER BY f.dt DESC LIMIT :lim
        """), p).scalars().all())


def _labels_for(urls: list[str]) -> dict[str, list[dict]]:
    """Чипы тем показанных отзывов: главная проблема первой, затем дополнительные."""
    out: dict[str, list[dict]] = {}
    for u, a in _ann_for(urls).items():
        if a["status"] not in ("agree", "arbitrated"):
            continue
        chips = []
        for code in [a["issue"]] + list(a["issues2"] or []):
            o = cb.issue_obj(code)
            if o and code != "no_issue":
                chips.append(o)
        out[u] = chips
    return out


def _attach_themes(items: list[dict]) -> None:
    """Темы, разбор и человекочитаемый источник — общий финиш ленты и поиска."""
    for r in items:
        src = r.get("source")
        if src:
            r["source"] = _SOURCE_LABEL.get(src, src)
    ann = _ann_for([i["url"] for i in items if i.get("url")])
    for r in items:
        a = ann.get(r.get("url") or "")
        if not a or a["status"] not in ("agree", "arbitrated"):
            r["themes"] = []
            r["theme_src"] = "pending"
            continue
        chips = []
        for code in [a["issue"]] + list(a["issues2"] or []):
            o = cb.issue_obj(code)
            if o and code != "no_issue":
                chips.append(o)
        r["themes"] = chips
        r["theme_src"] = "ann"
        # продукт — из разметки, а не метка площадки (у поиска по внешнему
        # корпусу она своя и неверна у большинства обращений)
        r["product"] = cb.product_label(a["product"])
        r["ann"] = {
            "kind": a["kind"], "summary": a["summary"],
            "quote": a["quote"] if a["quote_ok"] else None,
            "esc": a["esc"], "esc_to": list(a["esc_to"] or []),
            "no_consent": bool(a["no_consent"]), "misled": bool(a["misled"]),
            "vulnerable": list(a["vulnerable"] or []),
            "amount": float(a["amount"]) if a["amount"] is not None else None,
            "confidence": "согласие двух моделей" if a["status"] == "agree" else "решено арбитром",
            "new_topic": a["new_topic"] if a["code_fit"] == "approx" or a["issue"] == "other" else None,
        }


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




def list_reviews_ex(bank: str, product: str | None = None, theme: str | None = None,
                    q: str | None = None, days: int | None = None,
                    city: str | None = None, month: str | None = None,
                    limit: int = 20, offset: int = 0,
                    esc: bool = False, sort: str = "auto") -> dict:
    """Лента доказательной базы. q → поиск; иначе свежие с фильтрами
    тема/город/месяц. Дубли (массовые однотипные жалобы) не прячем, а считаем —
    массовость это аудит-сигнал → поле `similar`.

    Возвращает {items, mode, error}: упавший поиск и честное «ничего не
    нашлось» не должны выглядеть одинаково."""
    bc = resolve_bank(bank) if bank else None
    if theme and theme not in cb.ISSUES:
        return {"items": [], "mode": "search" if q else "feed", "error": "unknown_theme"}
    if q and q.strip():
        if bank and not bc:
            return {"items": [], "mode": "search", "error": "unknown_bank"}
        meta: dict = {}
        # Тема и отбор жалоб применяются к выдаче поиска по нашей разметке:
        # поиск идёт и по внешнему корпусу, где разметки нет. Поэтому берём с
        # запасом — иначе после отбора страница пустела бы.
        want = limit + offset + 1
        # при теме или продукте отбор после поиска узкий: берём широко, иначе
        # в первых десятках выдачи жалоб с нужной главной проблемой может не быть
        k = 300 if (theme or product) else min(400, want * 2)
        try:
            # Продукт тоже отбираем по нашей разметке: у внешнего корпуса своя
            # метка площадки («Обслуживание юридических лиц» у 180 тыс. строк),
            # и смысловая часть поиска с ней почти ничего не находила.
            res = search_reviews(q, bank=bc, product=None, since_days=days,
                                 theme_rx=None, city=city, month=month, k=k,
                                 strict=True, _meta=meta)
        except Exception as e:
            log.warning("reviews_dash: поиск по %r упал: %s", q, e)
            return {"items": [], "mode": "search", "error": "search_failed"}
        keep = _keep_urls([r.get("url") for r in res if r.get("url")], theme=theme, esc=esc,
                          product=product)
        res = [r for r in res if r.get("url") in keep]
        if sort == "date":
            res.sort(key=lambda r: (r.get("date") or ""), reverse=True)
        page = res[offset:offset + limit]
        _attach_themes(page)
        return {"items": page, "mode": "search", "error": None, "search": meta,
                "has_more": len(res) > offset + limit}
    if not bc:
        return {"items": [], "mode": "feed", "error": "unknown_bank"}
    return _feed_from_index(bc, product, theme, days, city, month, limit, offset, esc)


@_safe(None)
def segment_reviews(bank: str, product: str | None = None, city: str | None = None,
                    month: str | None = None, limit: int = 40) -> dict | None:
    """Сводка по срезу (город или месяц) для LLM-объяснения аномалии/пика:
    распределение главных проблем по разметке + изложения и примеры со ссылками."""
    revs = list_reviews(bank, product=product, city=city, month=month, limit=limit)
    if not revs:
        return {"n": 0, "themes": [], "samples": [], "texts": []}
    from collections import Counter
    cnt: Counter = Counter()
    risk_by: dict[str, str] = {}
    for r in revs:
        th = (r.get("themes") or [])[:1]
        for t in th:
            cnt[t["label"]] += 1
            risk_by[t["label"]] = t["risk"]
    themes_ = [{"label": lbl, "risk": risk_by[lbl], "n": n} for lbl, n in cnt.most_common(6)]
    samples = [{"date": r["date"], "city": r.get("city"), "url": r["url"],
                "text": (r["text"] or "")[:320]} for r in revs[:4]]
    texts = [(((r.get("ann") or {}).get("summary") or "") + " | " + (r["text"] or "")[:450])
             for r in revs[:25]]
    return {"n": len(revs), "themes": themes_, "samples": samples, "texts": texts}


@_safe(None)
def segment_profile(bank: str, product: str | None = None, city: str | None = None,
                    month: str | None = None, days: int = 90) -> dict | None:
    """Чем срез (город или месяц) отличается от нормы — по главной проблеме.

    Город сравнивается со всей страной за тот же период, месяц — с шестью
    предыдущими. Индекс = доля проблемы в срезе / доля в норме. flagged —
    отмечен ли срез аномалией по правилам вкладки (гео — per-capita, месяц —
    пик динамики): модель не должна называть аномалией то, что ею не отмечено,
    как было с Краснодаром 25.09."""
    bc = resolve_bank(bank)
    if not bc or not (city or month):
        return None
    p: dict = {"bank": bc, "product": product, "d": days, "city": city, "month": month}
    if city:
        seg = "i.city = :city AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
        base = "i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
        base_label = f"вся страна за те же {days} дн"
    else:
        seg = "date_trunc('month', i.dt) = to_date(:month, 'YYYY-MM')"
        base = ("i.dt >= to_date(:month, 'YYYY-MM') - interval '6 months'"
                " AND i.dt < to_date(:month, 'YYYY-MM')")
        base_label = "6 предыдущих месяцев"
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.issue, count(*) FILTER (WHERE {seg}) AS n, count(*) FILTER (WHERE {base}) AS b
            FROM review_index i
            WHERE i.bank = :bank AND {_CMP}
              AND (CAST(:product AS text) IS NULL OR i.product = :product)
              AND (({seg}) OR ({base}))
            GROUP BY 1"""), p).all()
    n_seg = sum(int(r[1]) for r in rows)
    n_base = sum(int(r[2]) for r in rows)
    if not n_seg or not n_base:
        return {"n": n_seg, "base_n": n_base, "base_label": base_label, "rows": [], "flagged": False}
    out = []
    for code, n, b in rows:
        o = cb.issue_obj(code) or {}
        n, b = int(n), int(b)
        if n < 3 or code in ("no_issue",):
            continue
        share, bshare = n / n_seg, b / n_base
        exp = bshare * n_seg
        out.append({"key": code, "label": o.get("label") or code, "risk": o.get("risk"),
                    "n": n, "pct": round(100 * share, 1), "base_pct": round(100 * bshare, 1),
                    "index": round(share / bshare, 1) if bshare else None,
                    "excess": round(n - exp, 1)})
    out.sort(key=lambda r: -r["excess"])
    flagged = False
    if city:
        g = geo(bank, product, days=days, top=40) or {}
        flagged = any(c["city"] == city and c.get("anomaly") for c in g.get("cities") or [])
    else:
        t = trend(bank, product) or {}
        flagged = any(x.get("ym") == month and x.get("spike") for x in t.get("series") or [])
    return {"n": n_seg, "base_n": n_base, "base_label": base_label, "rows": out[:8],
            "flagged": bool(flagged)}


# Конец недели сигнала — конец последнего ПОЛНОГО дня с данными, а не «сейчас».
# Корпус приходит с опозданием на сутки: неделя «от now()» содержала 6 дней
# данных против 7 в норме, и всплеск занижался на седьмую часть и опаздывал.
# Если корпус встал, неделя заканчивается на последнем дне, где он был.
_WEEK_END = ("(SELECT least(date_trunc('day', now()), date_trunc('day', max(dt)) + interval '1 day')"
             " FROM review_index WHERE source = 'bankiru' AND dt <= now())")


def week_end() -> str | None:
    """Дата последнего дня недели сигнала (для подписи «неделя по …»)."""
    try:
        with db.session() as s:
            v = s.execute(text(f"SELECT {_WEEK_END} - interval '1 day'")).scalar()
        return v.date().isoformat() if v else None
    except Exception:  # noqa: BLE001
        return None


def _topic_week_counts(bank_canon: str | None, product: str | None,
                       exclude_bank: str | None = None):
    """Понедельные счётчики жалоб по ГЛАВНОЙ проблеме — сырьё сигналов, пульса
    и слепой зоны. bank=None — рынок; exclude_bank — рынок без этого банка
    (иначе у крупного банка «рынок» наполовину состоит из него самого, и
    всплеск у банка выглядит отраслевым).

    Только жалобы, и только те, где события не старше 60 дней на момент
    отзыва: история двухлетней давности, опубликованная на этой неделе, —
    не всплеск этой недели (так в заголовок 22.09 попало событие 2025 года).

    Возвращает (topics, counts) или None:
      topics: [{key,label,short,risk,group}] — проблемы кодификатора;
      counts: <key>_w0/_w1/_b, <key>_wk (по неделям 0..8), _tw0, _tb,
              _lab_w0/_lab_b (размечено жалоб), _unc_w0/_unc_b («Прочее»)."""
    p = {"bank": bank_canon, "ex": exclude_bank, "product": product}
    try:
        with db.session() as s:
            rows = s.execute(text(f"""
                WITH dd AS (
                    SELECT i.issue, floor(extract(epoch FROM {_WEEK_END} - i.dt) / 604800)::int AS w
                    FROM review_index i
                    WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)
                      AND (CAST(:ex AS text) IS NULL OR i.bank <> :ex)
                      AND (CAST(:product AS text) IS NULL OR i.product = :product)
                      AND {_CMP}
                      AND i.dt >= {_WEEK_END} - make_interval(days => 63) AND i.dt < {_WEEK_END}
                      AND (i.ev_date IS NULL OR i.ev_date >= i.dt::date - 60))
                SELECT issue, w, count(*) FROM dd WHERE w BETWEEN 0 AND 8 GROUP BY 1, 2
            """), p).all()
    except Exception as e:  # noqa: BLE001
        log.warning("topic_week_counts: %s", e)
        return None
    topics = [t for t in cb.complaint_issues() if t["key"] != "other"]
    counts: dict = {}
    wk: dict[str, list[int]] = {}
    for code, w, n in rows:
        wk.setdefault(code, [0] * 9)[int(w)] += int(n)
    tot = [0] * 9
    for code, arr in wk.items():
        for j in range(9):
            tot[j] += arr[j]
    for t in topics + [{"key": "other"}]:
        arr = wk.get(t["key"], [0] * 9)
        counts[f'{t["key"]}_w0'] = arr[0]
        counts[f'{t["key"]}_w1'] = arr[1]
        counts[f'{t["key"]}_b'] = sum(arr[2:9])
        counts[f'{t["key"]}_wk'] = arr
    oth = wk.get("other", [0] * 9)
    counts.update({"_tw0": tot[0], "_tb": sum(tot[2:9]),
                   "_lab_w0": tot[0], "_lab_b": sum(tot[2:9]),
                   "_unc_w0": oth[0], "_unc_b": sum(oth[2:9])})
    return topics, counts


@_safe(None)
def top_topic(bank: str, product: str | None, days: int = 90) -> dict | None:
    """Ведущая проблема жалоб по продукту + динамика к прошлому окну — для
    стат-карт «Для вас»."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    with db.session() as s:
        row = s.execute(text(f"""
            SELECT i.issue,
                   count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :days)) AS n,
                   count(*) FILTER (WHERE i.dt <  now() - make_interval(days => :days)) AS p
            FROM review_index i
            WHERE i.bank = :bank AND {_CMP} AND i.issue NOT IN ('other', 'no_issue')
              AND (CAST(:product AS text) IS NULL OR i.product = :product)
              AND i.dt >= now() - make_interval(days => :days * 2) AND i.dt <= now()
            GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """), {"bank": bc, "product": product, "days": days}).first()
    if not row or not int(row[1]):
        return None
    o = cb.issue_obj(row[0]) or {}
    n, prev = int(row[1]), int(row[2])
    return {"key": row[0], "label": o.get("label"), "risk": o.get("risk"), "n": n,
            "delta_pct": (round(100.0 * (n - prev) / prev) if prev and _prev_ready(bc, days) else None)}




@_safe(None)
def week_pulse(bank: str, product: str | None = None) -> dict | None:
    """Недельный срез для «пульса дня» на главной — БЕЗ порога сигнала:
    расхождение с рынком по каждой проблеме (наша динамика против отраслевой,
    рынок — без самого банка) и общий объём недели."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    lab = _topic_week_counts(bc, product)
    if not lab:
        return None
    theme_defs, brow = lab
    mlab = _topic_week_counts(None, product, exclude_bank=bc)
    mrow = mlab[1] if mlab else None
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
        gap = (ratio / mratio) if mratio and mratio > 0 else None
        diverge.append({
            "key": k, "label": t["label"], "short": t["short"],
            "risk": t["risk"], "week": w0, "baseline_week": round(bw, 1),
            "base_count": b, "base_weeks": int(BASE_W),
            "ratio": round(ratio, 2),
            "market_ratio": round(mratio, 2) if mratio else None,
            "gap": round(gap, 2) if gap else None,
        })
    diverge.sort(key=lambda d: ((d["gap"] or d["ratio"]), d["week"]), reverse=True)
    tw0, tb = int(brow["_tw0"]), int(brow["_tb"])
    return {"diverge": diverge[:5], "week_total": tw0,
            "baseline_total": round(tb / BASE_W, 1), "src": "annotation"}


@_safe(None)
def unclassified_week(bank: str, product: str | None = None) -> dict | None:
    """Слепая зона: жалобы недели, которые модель не смогла отнести ни к одной
    проблеме кодификатора («Прочее»), и та же доля по базовому окну."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    lab = _topic_week_counts(bc, product)
    if not lab:
        return None
    _t, c_ = lab
    w_unc, w_tot, b_unc = c_["_unc_w0"], c_["_lab_w0"], c_["_unc_b"]
    base_week = round(b_unc / 7.0, 1)
    return {"week": w_unc, "week_total": w_tot,
            "pct": round(100 * w_unc / w_tot) if w_tot else 0,
            "baseline_week": base_week,
            "ratio": round(w_unc / base_week, 2) if base_week >= 1 else None,
            "src": "annotation"}


@_safe(None)
def weekly_signals(bank: str, product: str | None = None) -> dict | None:
    """Всплески жалоб за 7 дней по главной проблеме из LLM-разметки.

    Сигнал — не «выросло в ×1,8», а статистически значимый рост:
      • норма — 7 прошлых недель (окно 14–63 дня), с их собственным разбросом
        (отрицательно-биномиальное распределение);
      • поправка на множественность по всем проблемам банка (q < 0,05);
      • и практический порог: ≥ 8 жалоб, избыток ≥ 5 над нормой, рост ≥ ×1,5.
    Плюс: ускорение неделя к неделе, сравнение с рынком БЕЗ самого банка,
    географическая концентрация. Числа детерминированы; модель их только
    объясняет, и объясняет по жалобам самого сигнала (signal_evidence)."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    BASE_W = 7.0

    def _compute():
        lab = _topic_week_counts(bc, product)
        if not lab:
            return None
        theme_defs, brow = lab
        mlab = _topic_week_counts(None, product, exclude_bank=bc)
        mrow = mlab[1] if mlab else None
        cand, pv = [], {}
        for t in theme_defs:
            k = t["key"]
            w0, w1, b = int(brow[f"{k}_w0"]), int(brow[f"{k}_w1"]), int(brow[f"{k}_b"])
            weeks = list(brow[f"{k}_wk"][2:9])
            p = _nb_tail(w0, weeks)
            pv[k] = p
            cand.append((t, w0, w1, b, weeks, p))
        qv = _bh(pv)
        out = []
        for t, w0, w1, b, weeks, p in cand:
            k = t["key"]
            bw = b / BASE_W
            ratio = (w0 / bw) if bw >= 0.5 else None
            excess = w0 - bw
            new = b <= 2 and w0 >= 6
            practical = w0 >= 8 and excess >= 5 and (ratio is None or ratio >= 1.5)
            if not ((qv[k] < 0.05 and practical) or (new and qv[k] < 0.05)):
                continue
            accel = w0 > w1 and w0 >= max(8, 1.4 * w1)
            mratio, bank_specific = None, False
            if mrow is not None:
                mw0, mb = int(mrow[f"{k}_w0"]), int(mrow[f"{k}_b"])
                mbw = mb / BASE_W
                mratio = round(mw0 / mbw, 2) if mbw >= 0.5 else None
                if ratio is not None and (mratio is None or mratio < 1.4 or ratio >= 1.8 * mratio):
                    bank_specific = True
            out.append({"key": k, "label": t["label"], "short": t["short"],
                        "risk": t["risk"], "group": t["group"],
                        "week": w0, "prev_week": w1,
                        "base_count": b, "base_weeks": int(BASE_W),
                        "week_total": int(brow["_tw0"]),
                        "baseline_week": round(bw, 1),
                        "ratio": (round(ratio, 1) if ratio else None),
                        "excess": round(excess, 1),
                        "p_value": round(p, 5), "q_value": round(qv[k], 5),
                        "new": bool(new), "accel": bool(accel),
                        "market_ratio": mratio, "bank_specific": bool(bank_specific)})
        for s_ in out:
            strong = s_["q_value"] < 0.001 and s_["week"] >= 12
            s_["level"] = "high" if (strong or (s_["risk"] == "compliance" and s_["q_value"] < 0.01)
                                     or (s_["bank_specific"] and (s_["ratio"] or 0) >= 2.5)) else "medium"
        out.sort(key=lambda s_: (s_["level"] == "high", s_["excess"]), reverse=True)
        if out:
            top = out[0]
            try:
                with db.session() as s:
                    grows = s.execute(text(f"""
                        SELECT i.city, count(*) FROM review_index i
                        WHERE i.bank = :bank AND i.issue = :key AND {_CMP}
                          AND (CAST(:product AS text) IS NULL OR i.product = :product)
                          AND i.dt >= {_WEEK_END} - make_interval(days => 7) AND i.dt < {_WEEK_END}
                          AND coalesce(i.city, '') <> ''
                        GROUP BY 1 ORDER BY 2 DESC LIMIT 3
                    """), {"bank": bc, "key": top["key"], "product": product}).all()
                tot = sum(int(x[1]) for x in grows) or 1
                if grows and int(grows[0][1]) >= 4 and int(grows[0][1]) / tot >= 0.4:
                    top["geo"] = {"city": grows[0][0], "share": round(100 * int(grows[0][1]) / tot)}
            except Exception:  # noqa: BLE001
                pass
        tw0, tb = int(brow["_tw0"]), int(brow["_tb"])
        tbw = tb / BASE_W
        overall = {"week": tw0, "baseline_week": round(tbw, 1),
                   "ratio": (round(tw0 / tbw, 1) if tbw >= 0.5 else None)}
        if mrow is not None:
            mtbw = int(mrow["_tb"]) / BASE_W
            overall["market_ratio"] = round(int(mrow["_tw0"]) / mtbw, 2) if mtbw >= 0.5 else None
        return {"bank": bc, "product": product, "signals": out[:6], "overall": overall,
                "week_end": week_end(), "src": "annotation"}
    return _cached(f"wk:{bc}:{product}", _compute, ttl=1800)


def _ann_for(urls: list[str]) -> dict[str, dict]:
    """Разметка показанных отзывов одним запросом: коды, признаки, изложение,
    цитата. Нужна карточке — аудитор видит не только тему, но и то, почему."""
    if not urls:
        return {}
    try:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT a.url, a.status, a.kind, a.issue, a.issues2, a.esc, a.esc_to,
                       a.no_consent, a.misled, a.vulnerable, a.amount, a.summary, a.quote,
                       a.quote_ok, a.code_fit, a.new_topic, a.product
                FROM review_annotation a
                WHERE a.schema_version = :sv AND a.url = ANY(:u)
            """), {"sv": _ann_schema(), "u": urls}).mappings().all()
    except Exception as e:                                     # noqa: BLE001
        log.warning("reviews_dash: разметка показанных отзывов не забралась (%s)", e)
        return {}
    return {r["url"]: dict(r) for r in rows}


def _keep_urls(urls: list[str], *, theme: str | None, esc: bool,
               product: str | None = None) -> set[str]:
    """Какие из найденных отзывов показывать: жалобы (и ещё не размеченные),
    при заданной теме — с этой главной проблемой, при флажке — с эскалацией."""
    if not urls:
        return set()
    cond = [f"(i.kind IS NULL OR {_CMP})" if not theme else _CMP]
    p: dict = {"u": urls}
    if theme:
        cond.append("i.issue = :t")
        p["t"] = theme
    if esc:
        cond.append("i.esc")
    if product:
        cond.append("i.product = :pr")
        p["pr"] = product
    try:
        with db.session() as s:
            known = set(s.execute(text("SELECT i.url FROM review_index i WHERE i.url = ANY(:u)"),
                                  {"u": urls}).scalars().all())
            ok = set(s.execute(text(
                f"SELECT i.url FROM review_index i WHERE i.url = ANY(:u) AND {' AND '.join(cond)}"),
                p).scalars().all())
    except Exception as e:                                     # noqa: BLE001
        log.warning("reviews_dash: отбор выдачи по разметке не сработал (%s)", e)
        return set(urls)
    # отзыв, которого нет в индексе (вне окна зеркала), без темы не отбрасываем
    return ok | ({u for u in urls if u not in known} if not (theme or esc or product) else set())


def _nb_tail(x: int, weeks: list[int]) -> float:
    """P(X ≥ x) при недельной норме из истории — отрицательно-биномиальное
    распределение с разбросом, оценённым по самим неделям (жалобы идут
    волнами, и пуассоновский порог на них даёт ложные всплески). Если разброс
    не больше среднего — обычный Пуассон."""
    import math
    n = len(weeks)
    m = sum(weeks) / n if n else 0.0
    m = max(m, 0.5)                               # нулевая база — не бесконечный рост
    v = (sum((w - m) ** 2 for w in weeks) / (n - 1)) if n > 1 else m
    if x <= 0:
        return 1.0
    if v > m * 1.05:
        r = m * m / (v - m)
        q = r / (r + m)
        pk = q ** r
        cdf = pk
        for k in range(0, x - 1):
            pk *= (k + r) / (k + 1) * (1 - q)
            cdf += pk
    else:
        pk = math.exp(-m)
        cdf = pk
        for k in range(0, x - 1):
            pk *= m / (k + 1)
            cdf += pk
    return max(0.0, min(1.0, 1.0 - cdf))


def _bh(pvals: dict[str, float]) -> dict[str, float]:
    """q-значения Бенджамини — Хохберга: проблем десятки, и без поправки хотя
    бы одна «пробивает порог» каждую неделю просто по случайности."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out, prev = {}, 1.0
    for rank in range(m, 0, -1):
        k, p = items[rank - 1]
        prev = min(prev, p * m / rank)
        out[k] = prev
    return out


@_safe([])
def signal_evidence(bank: str, key: str, product: str | None = None, days: int = 7,
                    limit: int = 30) -> list[dict]:
    """Жалобы, из которых сложился сигнал: та же выборка, что в его счётчике
    (главная проблема, жалоба, события не старше 60 дней). Именно их, а не
    «последние жалобы банка» читает модель, когда объясняет сигнал: 22.09
    сводка взяла формулировку из отзыва другой темы."""
    bc = resolve_bank(bank)
    if not bc or key not in cb.ISSUES:
        return []
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.url, i.dt, i.city, a.summary, a.quote, a.quote_ok, a.esc,
                   a.no_consent, a.misled, a.issues2
            FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE i.bank = :bank AND i.issue = :key AND {_CMP}
              AND (CAST(:product AS text) IS NULL OR i.product = :product)
              AND i.dt >= {_WEEK_END} - make_interval(days => :d) AND i.dt < {_WEEK_END}
              AND (i.ev_date IS NULL OR i.ev_date >= i.dt::date - 60)
            ORDER BY i.dt DESC LIMIT :lim
        """), {"bank": bc, "key": key, "product": product, "d": days, "lim": limit,
               "sv": _ann_schema()}).mappings().all()
    return [{"url": r["url"], "date": r["dt"].date().isoformat() if r["dt"] else None,
             "city": r["city"], "summary": r["summary"],
             "quote": r["quote"] if r["quote_ok"] else None,
             "esc": r["esc"], "no_consent": bool(r["no_consent"]), "misled": bool(r["misled"])}
            for r in rows]


@_safe([])
def novel_clusters(bank: str, product: str | None = None, days: int = 7,
                   min_n: int = 3, sim: float = 0.78) -> list[dict]:
    """Новые сюжеты недели, сгруппированные кодом: формулировки проблем вне
    кодификатора, похожие по смыслу (векторы bge-m3), от min_n жалоб.

    Раньше «новую тему» решала модель по списку из 20 жалоб и склеивала
    разнородное: три разных случая («скрыли альтернативу», «не дали бонус»,
    «пенсионер») назвала одной темой. Теперь группы считает код, модель их
    только называет."""
    rows = novel_week(bank, product=product, days=days, limit=120)
    if len(rows) < min_n:
        return []
    try:
        from . import embedder
        vecs = embedder.embed_batch([(r["new_topic"] or "")[:200] for r in rows])
    except Exception as e:  # noqa: BLE001 — без векторов новых сюжетов не выводим
        log.info("novel_clusters: векторы недоступны (%s)", e)
        return []
    n = len(rows)
    near = [[j for j in range(n) if j != i and embedder.cosine_similarity(vecs[i], vecs[j]) >= sim]
            for i in range(n)]
    taken: set[int] = set()
    out = []
    for i in sorted(range(n), key=lambda k: -len(near[k])):
        if i in taken:
            continue
        members = [i] + [j for j in near[i] if j not in taken]
        if len(members) < min_n:
            continue
        taken.update(members)
        out.append({"n": len(members), "topic": rows[i]["new_topic"],
                    "items": [rows[j] for j in members]})
    return out


def novel_week(bank: str, product: str | None = None, days: int = 7, limit: int = 30) -> list[dict]:
    """Жалобы недели, для которых в кодификаторе нет точного кода: модель
    отнесла их к «Прочему» или отметила код как приблизительный и назвала
    проблему своими словами. Отсюда видно новое — и для сводки, и для
    пополнения кодификатора."""
    bc = resolve_bank(bank)
    if not bc:
        return []
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.url, i.dt, a.new_topic, a.summary, a.issue
            FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE i.bank = :bank AND {_CMP}
              AND (CAST(:product AS text) IS NULL OR i.product = :product)
              AND i.dt >= {_WEEK_END} - make_interval(days => :d) AND i.dt < {_WEEK_END}
              AND (a.issue = 'other' OR a.code_fit = 'approx') AND a.new_topic IS NOT NULL
            ORDER BY i.dt DESC LIMIT :lim
        """), {"bank": bc, "product": product, "d": days, "lim": limit,
               "sv": _ann_schema()}).mappings().all()
    return [{"url": r["url"], "date": r["dt"].date().isoformat() if r["dt"] else None,
             "new_topic": r["new_topic"], "summary": r["summary"], "issue": r["issue"]}
            for r in rows]
