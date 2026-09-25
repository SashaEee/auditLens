"""Аналитика отзывов для вкладки «Отзывы» (риск-радар голоса клиента).

Агрегаты поверх корпуса banki.ru (БД `bankiru`, ~390к жалоб 1-2★, 2025-2026):
KPI, помесячная динамика + детект спайков, таксономия тем с трендом и
категорией риска, индекс «банк против рынка» по проблемам, география
(индекс доли банка в городе), признаки риска, лента.

Все тяжёлые агрегаты bank-scoped (подмножество ≤50к строк) → быстро.
Кэш на процесс с TTL (агрегаты считаются раз в ~час).
"""
from __future__ import annotations

import datetime as _dt
import functools
import logging
import math
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


# Срез «месяц»: «2026-05» — по дате отзыва, «ev:2026-05» — по дате события
# (режим динамики «по дате события»). Один параметр проходит через ленту,
# выгрузку, профиль среза и разбор моделью без смены их сигнатур.
_MONTH_RX = re.compile(r"^(ev:)?(\d{4}-\d{2})$")


def _month_clause(alias: str, month: str | None, p: dict) -> str:
    if not month:
        return ""
    m = _MONTH_RX.match(month)
    if not m:
        return " AND false"
    p["month"] = m.group(2)
    col = f"{alias}.ev_date" if m.group(1) else f"{alias}.dt"
    return f" AND date_trunc('month', {col}) = to_date(:month, 'YYYY-MM')"


# Признаки риска из разметки — фильтр ленты и выгрузки. Коды — белый список:
# значение попадает в SQL, поэтому всё, чего нет в списке, отвергается.
_ESC_TO = {"cbr": "ЦБ", "court": "суд", "rpn": "Роспотребнадзор", "fas": "ФАС",
           "prosecutor": "прокуратура", "finombudsman": "финомбудсмен", "police": "полиция"}
_VULN = {"pensioner": "пенсионеры", "low_income": "низкий доход", "svo": "участники СВО",
         "minor": "несовершеннолетние", "disabled": "инвалиды", "ill": "тяжелобольные"}


def _flag_sql(flag: str | None) -> str | None:
    """Условие на разметку `a` по коду признака; "" — фильтра нет, None — код неизвестен."""
    if not flag:
        return ""
    esc = "coalesce(a.esc, 'none') <> 'none'"
    if flag in ("esc:filed", "esc:threat"):
        return f"a.esc = '{flag[4:]}'"
    if flag == "filed:cbr_court":
        return "a.esc = 'filed' AND a.esc_to && ARRAY['cbr', 'court']"
    if flag.startswith("to:") and flag[3:] in _ESC_TO:
        return f"{esc} AND '{flag[3:]}' = ANY(a.esc_to)"
    if flag == "vuln:any":
        return "cardinality(a.vulnerable) > 0"
    if flag.startswith("vuln:") and flag[5:] in _VULN:
        return f"'{flag[5:]}' = ANY(a.vulnerable)"
    return {"no_consent": "a.no_consent", "misled": "a.misled",
            "amount": "a.amount IS NOT NULL", "amount:1m": "a.amount >= 1000000"}.get(flag)


# Площадка — фильтр ленты. banki.ru — это и внешний корпус, и наш сбор с неё:
# для аудитора одна площадка.
_SOURCE_KEYS = {"banki": ("bankiru", "banki_reviews"), "sravni": ("sravni_reviews",),
                "bankiros": ("bankiros_reviews",), "finuslugi": ("finuslugi_reviews",)}


def _source_clause(alias: str, source: str | None, p: dict) -> str | None:
    """"" — без фильтра, None — площадка неизвестна."""
    if not source:
        return ""
    keys = _SOURCE_KEYS.get(source)
    if not keys:
        return None
    p["srcs"] = list(keys)
    return f" AND {alias}.source = ANY(:srcs)"


def _severity_sql(alias_i: str = "i", alias_a: str = "a") -> str:
    """Серьёзность жалобы для порядка «сначала серьёзные» — из признаков
    разметки: уже обратился в ЦБ/суд/прокуратуру (3) или грозит (2), уязвимый
    клиент (2), без согласия (1), сумма от 100 тыс. ₽ (1), проблема класса
    «комплаенс» (1). Коды комплаенса — из кодификатора, не из запроса."""
    comp = ", ".join(f"'{k}'" for k, v in cb.ISSUES.items() if v[3] == "compliance")
    a, i = alias_a, alias_i
    return (f"(CASE {a}.esc WHEN 'filed' THEN 3 WHEN 'threat' THEN 2 ELSE 0 END"
            f" + CASE WHEN cardinality({a}.vulnerable) > 0 THEN 2 ELSE 0 END"
            f" + CASE WHEN {a}.no_consent THEN 1 ELSE 0 END"
            f" + CASE WHEN {a}.amount >= 100000 THEN 1 ELSE 0 END"
            f" + CASE WHEN {i}.issue IN ({comp}) THEN 1 ELSE 0 END)")


def flag_label(flag: str | None) -> str | None:
    """Подпись признака для ленты и имени выгрузки."""
    if not flag or _flag_sql(flag) is None:
        return None
    if flag.startswith("to:"):
        return f"обращение: {_ESC_TO[flag[3:]]}"
    if flag.startswith("vuln:"):
        return "уязвимые клиенты" if flag == "vuln:any" else _VULN[flag[5:]]
    return {"esc:filed": "уже обратились", "esc:threat": "грозят обратиться",
            "filed:cbr_court": "обратились в ЦБ или суд",
            "no_consent": "без согласия", "misled": "ввели в заблуждение",
            "amount": "указана сумма", "amount:1m": "сумма от 1 млн ₽"}[flag]


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
                       count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d) AND i.esc),
                       count(*) FILTER (WHERE i.dt >= now() - make_interval(days => :d2)
                                          AND i.dt <  now() - make_interval(days => :d) AND i.esc)
                FROM review_index i WHERE {idx} AND {_CMP}
            """), {**ip, "d": days, "d2": days * 2}).one()
            total_cur, total_prev, esc_cur = int(cur[0]), int(cur[1]), int(cur[2])
            esc_prev = int(cur[3])
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
            # эскалация у остальных банков — с чем сравнивать долю банка: порог
            # 12% у крупного банка пробит всегда, и плитка была красной постоянно
            m_n, m_esc = s.execute(text(
                f"SELECT count(*), count(*) FILTER (WHERE i.esc) FROM review_index i"
                f" WHERE i.bank <> :bank AND {_CMP}"
                f" AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
                + (" AND i.product = :product" if product else "")),
                {"bank": bc, "d": days, **({"product": product} if product else {})}).one()
            by_src = [{"source": r[0], "n": int(r[1])} for r in s.execute(text(
                f"SELECT i.source, count(*) FROM review_index i WHERE {idx} AND {_CMP}"
                f" AND i.dt >= now() - make_interval(days => :d)"
                f" GROUP BY 1 ORDER BY 2 DESC"), {**ip, "d": days}).all()]
        total_market = sum(int(r[1]) for r in mk) or 1
        m_n, m_esc = int(m_n or 0), int(m_esc or 0)
        esc_sig = False
        if total_cur >= 30 and m_n >= 100:
            p1, p0 = esc_cur / total_cur, m_esc / m_n
            pp = (esc_cur + m_esc) / (total_cur + m_n)
            se = math.sqrt(pp * (1 - pp) * (1 / total_cur + 1 / m_n)) if 0 < pp < 1 else 0
            esc_sig = bool(se and _p2((p1 - p0) / se) < 0.01 and p0 and p1 / p0 >= 1.2)
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
            "esc_n": esc_cur, "esc_prev_n": esc_prev,
            "market_escalation_pct": round(100.0 * m_esc / m_n, 1) if m_n else None,
            "escalation_sig": esc_sig,
            "as_of": asof.date().isoformat() if asof else None,
            "by_source": by_src, "src": "annotation",
            "coverage": _coverage(bc, days * 2),
        }
    return _cached(f"ov:{bc}:{product}:{days}", _compute)


def _lag_cdf() -> list[float]:
    """Доля жалоб, опубликованных не позже k дней после события (k = 0..730).

    По всем банкам за год публикаций. Нужна режиму «по дате события»: месяц
    события наполняется ещё месяцами (медиана задержки — неделя, у каждой
    десятой — больше четырёх месяцев), и без поправки последние месяцы
    выглядят спадом."""
    def _compute():
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT least(greatest(i.dt::date - i.ev_date, 0), 730), count(*)
                FROM review_index i
                WHERE {_CMP} AND i.ev_date IS NOT NULL AND i.ev_date <= i.dt::date + 1
                  AND i.dt > now() - interval '365 days' AND i.dt <= now()
                GROUP BY 1""")).all()
        hist = [0] * 731
        for k, n in rows:
            hist[int(k)] += int(n)
        tot = sum(hist) or 1
        cdf, acc = [], 0
        for n in hist:
            acc += n
            cdf.append(acc / tot)
        return cdf
    return _cached("lag_cdf", _compute, ttl=6 * 3600)


def _month_completeness(ym: str, today, cdf: list[float]) -> float:
    """Какая доля жалоб о событиях месяца ym уже опубликована к today."""
    y, m = map(int, ym.split("-"))
    d = _dt.date(y, m, 1)
    vals = []
    while d.month == m and d <= today:
        vals.append(cdf[min((today - d).days, 730)])
        d += _dt.timedelta(days=1)
    return sum(vals) / len(vals) if vals else 0.0


@_safe(None)
def trend(bank: str, product: str | None = None, months: int = 14,
          basis: str = "pub") -> dict | None:
    """Помесячная динамика жалоб. basis="event" — по дате самого события, а не
    отзыва: четверть отзывов описывает события старше двух месяцев, и пик по
    дате публикации запаздывает и размазывается. Считаются только жалобы с
    датой события (её называют ~60%); месяцы, которые по опыту ещё не
    наполнились до 90%, помечены неполными — с оценкой, сколько ещё придёт."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    if basis == "event":
        return _cached(f"tre:{bc}:{product}:{months}", lambda: _trend_event(bc, product, months))

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
        return {"bank": bc, "product": product, "series": series,
                "baseline": _mark_spikes(series), "basis": "pub"}
    return _cached(f"tr:{bc}:{product}:{months}", _compute)


def _mark_spikes(series: list[dict]) -> float | None:
    """Пик — месяц выше медианы + 2·MAD завершённых месяцев и не меньше ×1,4."""
    complete = [s["n"] for s in series if not s["partial"]]
    if len(complete) < 4:
        return None
    med = _median(complete)
    mad = _median([abs(v - med) for v in complete]) or (
        sum(abs(v - med) for v in complete) / len(complete))
    thr = med + 2.0 * mad
    for s in series:
        s["pct_vs_median"] = round(100.0 * (s["n"] - med) / med) if med else 0
        s["spike"] = (not s["partial"]) and s["n"] > thr and s["n"] >= med * 1.4
    return med


def _trend_event(bc: str, product: str | None, months: int) -> dict:
    idx, ip = _idx_clause(bc, product)
    with db.session() as s:
        rows = s.execute(text(
            f"SELECT to_char(date_trunc('month', i.ev_date), 'YYYY-MM') ym, count(*)"
            f" FROM review_index i WHERE {idx} AND {_CMP}"
            f" AND i.ev_date IS NOT NULL AND i.ev_date <= i.dt::date + 1"
            f" AND i.ev_date >= date_trunc('month', now()) - make_interval(months => :m)"
            f" GROUP BY 1 ORDER BY 1"), {**ip, "m": months - 1}).all()
        with_ev, tot = s.execute(text(
            f"SELECT count(i.ev_date), count(*) FROM review_index i WHERE {idx} AND {_CMP}"
            f" AND i.dt >= now() - interval '365 days'"), ip).one()
        today = s.execute(text("SELECT current_date")).scalar()
    cdf = _lag_cdf()
    series = []
    for ym, n in rows:
        c = _month_completeness(ym, today, cdf)
        item = {"ym": ym, "n": int(n), "partial": c < 0.9}
        if c < 0.9:
            item["complete_pct"] = round(100 * c)
            if c >= 0.3:
                item["expected"] = round(int(n) / c)
        series.append(item)
    return {"bank": bc, "product": product, "series": series,
            "baseline": _mark_spikes(series), "basis": "event",
            "ev_share": round(100 * with_ev / tot) if tot else None}



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
        total_prev = sum(int(r[2]) for r in rows)
        ready = _prev_ready(bc, days)
        # Значимость изменения темы — против ОБЩЕГО потока: если жалоб в целом
        # стало на 30% больше, тема с +35% не «растёт», она идёт вместе со всеми.
        # Проверка: доля текущего окна в сумме двух окон у темы против той же
        # доли у всех жалоб (биномиальная, нормальное приближение), с поправкой
        # на число тем. Меньше 20 жалоб в двух окнах — «мало данных» (Б4).
        share_cur = total / (total + total_prev) if total_prev else None
        pv: dict[str, float] = {}
        out = []
        for code, n, prev in rows:
            o = cb.issue_obj(code)
            if not o or code == "no_issue":
                continue
            n, prev = int(n), int(prev)
            if not n and not prev:
                continue
            row = {**o, "n": n, "prev": prev, "pct": round(100.0 * n / total, 1),
                   "n_also": int(also.get(code, 0)),
                   "delta_pct": (None if not ready else
                                 round(100.0 * (n - prev) / prev) if prev else (None if n == 0 else 100))}
            if ready and share_cur is not None:
                m = n + prev
                row["delta_low"] = m < 20 or prev < 5
                if not row["delta_low"]:
                    z = (n - m * share_cur) / math.sqrt(m * share_cur * (1 - share_cur))
                    pv[code] = _p2(z)
                    ci = _rate_change(n, prev)
                    if ci:
                        row["delta_ci"] = [ci["lo"], ci["hi"]]
                    row["excess"] = round(n - prev * total / total_prev)
            out.append(row)
        qv = _bh(pv) if pv else {}
        for row in out:
            if row["key"] in qv:
                row["delta_sig"] = qv[row["key"]] < 0.05
        out.sort(key=lambda x: (x["key"] == "other", -x["n"]))
        return {"bank": bc, "product": product, "days": days, "total": total,
                "total_prev": total_prev,
                "overall_delta_pct": (round(100.0 * (total - total_prev) / total_prev)
                                      if ready and total_prev else None),
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
def issue_index(bank: str, product: str | None = None, days: int = 180) -> dict | None:
    """Где структура жалоб банка отличается от рынка — по главной проблеме.

    Индекс = доля проблемы в жалобах банка / её доля в жалобах ОСТАЛЬНЫХ
    банков. Сравнивается структура, а не объём, поэтому ни размер банка, ни
    то, насколько охотно его клиенты пишут на площадки, на индекс не влияют —
    нормировка на число клиентов здесь не нужна. «Хуже рынка» — только
    значимое отличие: 95% ДИ индекса, поправка на число проверенных проблем,
    от 10 жалоб у банка и 30 у рынка, и практический порог (от ×1,25 или до
    ×0,8). excess — сколько жалоб у банка сверх того, что было бы при
    структуре рынка. Динамика — тот же индекс по четырём кварталам (90 дней).
    Окно не короче 90 дней: на месяце почти все проблемы «мало данных»."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    days = max(int(days or 180), 90)

    def _compute():
        p = {"bank": bc, "product": product, "d": days}
        base = (f"{_CMP} AND i.bank IS NOT NULL"
                " AND (CAST(:product AS text) IS NULL OR i.product = :product) AND i.dt <= now()")
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT i.issue, (i.bank = :bank) AS own, count(*)
                FROM review_index i
                WHERE {base} AND i.dt >= now() - make_interval(days => :d)
                GROUP BY 1, 2"""), p).all()
            qrows = s.execute(text(f"""
                SELECT i.issue, (i.bank = :bank) AS own,
                       floor(extract(epoch FROM now() - i.dt) / 7776000)::int AS q, count(*)
                FROM review_index i
                WHERE {base} AND i.dt > now() - interval '360 days'
                GROUP BY 1, 2, 3"""), p).all()
            today = s.execute(text("SELECT current_date")).scalar()
        a_: dict[str, int] = {}
        c_: dict[str, int] = {}
        for code, own, n in rows:
            (a_ if own else c_)[code] = (a_ if own else c_).get(code, 0) + int(n)
        B, C = sum(a_.values()), sum(c_.values())
        if not B or not C:
            return {"bank": bc, "product": product, "days": days, "bank_total": B,
                    "market_total": C, "worse": [], "better": [], "tested": 0}
        qa = [dict() for _ in range(4)]
        qc = [dict() for _ in range(4)]
        for code, own, q, n in qrows:
            if 0 <= int(q) <= 3:
                d = (qa if own else qc)[int(q)]
                d[code] = d.get(code, 0) + int(n)
        qB = [sum(d.values()) for d in qa]
        qC = [sum(d.values()) for d in qc]
        rows_out, pv = [], {}
        for code in set(a_) | set(c_):
            o = cb.issue_obj(code)
            if not o or code in ("other", "no_issue"):
                continue
            a, c = a_.get(code, 0), c_.get(code, 0)
            if a + c < 10:
                continue
            r = _ratio_ci(a, B, c, C)
            if not r:
                continue
            pv[code] = r["p"]
            trend_q = []
            for k in (3, 2, 1, 0):                       # от старого квартала к свежему
                ak, ck = qa[k].get(code, 0), qc[k].get(code, 0)
                rk = _ratio_ci(ak, qB[k], ck, qC[k]) if ak >= 5 and ck >= 15 else None
                trend_q.append(round(rk["rr"], 2) if rk else None)
            rows_out.append({"key": code, "label": o["label"], "short": o.get("short"),
                             "risk": o.get("risk"), "n": a, "market_n": c,
                             "pct": round(100 * a / B, 1), "market_pct": round(100 * c / C, 1),
                             "index": round(r["rr"], 1), "lo": round(r["lo"], 2),
                             "hi": round(r["hi"], 2), "excess": round(a - B * c / C),
                             "low": a < 10 or c < 30, "quarters": trend_q})
        qv = _bh(pv)
        for r in rows_out:
            r["q"] = round(qv.get(r["key"], 1.0), 4)
            r["sig"] = (r["q"] < 0.05 and not r["low"]
                        and (r["index"] >= 1.25 or r["index"] <= 0.8))
        worse = sorted([r for r in rows_out if r["sig"] and r["index"] > 1],
                       key=lambda r: -r["excess"])
        better = sorted([r for r in rows_out if r["sig"] and r["index"] < 1],
                        key=lambda r: r["excess"])
        quarters = [{"from": (today - _dt.timedelta(days=90 * (k + 1))).isoformat(),
                     "to": (today - _dt.timedelta(days=90 * k)).isoformat()} for k in (3, 2, 1, 0)]
        return {"bank": bc, "product": product, "days": days, "bank_total": B,
                "market_total": C, "bank_share": round(100 * B / (B + C), 1),
                "tested": len(pv), "worse": worse[:8], "better": better[:4],
                "quarters": quarters}
    return _cached(f"ii:{bc}:{product}:{days}", _compute)


@_safe(None)
def geo(bank: str, product: str | None = None, days: int = 365, top: int = 8) -> dict | None:
    """География как индекс: доля банка в жалобах города против его доли в
    остальной стране (Б2). Население города больше не используется: на
    площадки пишет не население, и деление на него раздувало города, где
    площадкой пользуются активнее, — так «аномалией» выглядел Краснодар.
    Аномалия — значимо выше (95% ДИ, поправка на число городов), от ×1,3 и
    от 30 жалоб. focus — проблема, которой у банка в этом городе заметно
    больше, чем у него же по стране. Города сверх топа по объёму, но с
    аномалией, добавляются в конец."""
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        p = {"bank": bc, "product": product, "d": days}
        win = (f"{_CMP} AND coalesce(i.city, '') <> '' AND i.bank IS NOT NULL"
               " AND (CAST(:product AS text) IS NULL OR i.product = :product)"
               " AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()")
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT i.city, count(*) FILTER (WHERE i.bank = :bank), count(*)
                FROM review_index i WHERE {win} GROUP BY 1"""), p).all()
            mix = s.execute(text(f"""
                SELECT i.city, i.issue, count(*) FROM review_index i
                WHERE {win} AND i.bank = :bank GROUP BY 1, 2"""), p).all()
        A = sum(int(r[1]) for r in rows)
        T = sum(int(r[2]) for r in rows)
        if not A or A == T:
            return {"bank": bc, "product": product, "days": days, "cities": []}
        nat: dict[str, int] = {}
        by_city: dict[str, dict[str, int]] = {}
        for city, code, n in mix:
            nat[code] = nat.get(code, 0) + int(n)
            by_city.setdefault(city, {})[code] = int(n)
        cities, pv = [], {}
        for city, a, t in rows:
            a, t = int(a), int(t)
            if not a:
                continue
            r = _ratio_ci(a, t, A - a, T - t)
            if not r:
                continue
            if a >= 10:
                pv[city] = r["p"]
            c = {"city": city, "n": a, "share": round(100 * a / t, 1),
                 "base_share": round(100 * (A - a) / (T - t), 1),
                 "index": round(r["rr"], 1), "lo": round(r["lo"], 2), "hi": round(r["hi"], 2),
                 "low": a < 30}
            best = None
            for code, n in (by_city.get(city) or {}).items():
                o = cb.issue_obj(code)
                if not o or code in ("other", "no_issue") or n < 5:
                    continue
                rc = _ratio_ci(n, a, nat[code] - n, A - a)
                if not rc or rc["rr"] < 1.5 or rc["p"] >= 0.01:
                    continue
                ex = n - a * nat[code] / A
                if ex >= 3 and (not best or ex > best["excess"]):
                    best = {"key": code, "label": o["label"], "short": o.get("short"),
                            "n": n, "index": round(rc["rr"], 1), "excess": round(ex)}
            if best:
                c["focus"] = best
            cities.append(c)
        qv = _bh(pv) if pv else {}
        for c in cities:
            q = qv.get(c["city"], 1.0)
            c["anomaly"] = q < 0.05 and not c["low"] and c["index"] >= 1.3
            c["below"] = q < 0.05 and not c["low"] and c["index"] <= 0.77
        cities.sort(key=lambda c: -c["n"])
        shown = cities[:top]
        extra = [c for c in cities[top:] if c["anomaly"]]
        extra.sort(key=lambda c: -(c["n"] - c["n"] / c["index"]))
        return {"bank": bc, "product": product, "days": days,
                "national_share": round(100 * A / T, 1),
                "cities": shown + [dict(c, extra=True) for c in extra[:3]]}
    return _cached(f"geo:{bc}:{product}:{days}:{top}", _compute)


_FLAG_GROUPS = [
    ("esc", "Куда обращаются", [(f"to:{k}", v[:1].upper() + v[1:], None) for k, v in _ESC_TO.items()]),
    ("vuln", "Уязвимые клиенты", [("vuln:any", "Все уязвимые", None)]
     + [(f"vuln:{k}", v[:1].upper() + v[1:], None) for k, v in _VULN.items()]),
    ("conduct", "Практики и суммы", [
        ("no_consent", "Без согласия", None),
        ("misled", "Ввели в заблуждение",
         "признак пока широкий: сюда попадают и споры об условиях, не только введение в заблуждение при продаже"),
        ("amount", "Указана сумма", "сумма бывает и ущербом, и суммой самого продукта — разделение в работе"),
        ("amount:1m", "Сумма от 1 млн ₽", "сумма бывает и ущербом, и суммой самого продукта"),
    ]),
]


@_safe(None)
def risk_flags(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    """Признаки риска из разметки: куда клиент грозит или уже обратился,
    уязвимые клиенты, «без согласия», «ввели в заблуждение», суммы. Для
    каждого — число и доля в жалобах банка против доли у остальных банков
    (значимость — с поправкой на число признаков). Код признака — фильтр ленты."""
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        codes = [f for _g, _l, items in _FLAG_GROUPS for f, _n, _c in items]
        cols = ",\n".join(f"count(*) FILTER (WHERE {_flag_sql(f)})" for f in codes)
        filed_cols = ",\n".join(f"count(*) FILTER (WHERE a.esc = 'filed' AND '{k}' = ANY(a.esc_to))"
                                for k in _ESC_TO)
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT (i.bank = :bank) AS own, count(*),
                       count(*) FILTER (WHERE a.esc = 'filed'),
                       count(*) FILTER (WHERE a.esc = 'threat'),
                       {cols},
                       {filed_cols}
                FROM review_index i
                JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
                WHERE {_CMP} AND i.bank IS NOT NULL
                  AND (CAST(:product AS text) IS NULL OR i.product = :product)
                  AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()
                GROUP BY 1"""), {"bank": bc, "product": product, "d": days,
                                 "sv": _ann_schema()}).all()
        own = next((r for r in rows if r[0]), None)
        mkt = next((r for r in rows if not r[0]), None)
        if not own or not mkt or not own[1]:
            return {"bank": bc, "product": product, "days": days, "total": 0, "groups": []}
        B, C = int(own[1]), int(mkt[1])
        vals = {f: (int(own[4 + j]), int(mkt[4 + j])) for j, f in enumerate(codes)}
        filed = {k: int(own[4 + len(codes) + j]) for j, k in enumerate(_ESC_TO)}
        pv = {}
        for f, (a, c) in vals.items():
            r = _ratio_ci(a, B, c, C)
            if r and a >= 5:
                pv[f] = r["p"]
        qv = _bh(pv) if pv else {}
        groups = []
        for g, label, items in _FLAG_GROUPS:
            out = []
            for f, name, caveat in items:
                a, c = vals[f]
                idx = (a / B) / (c / C) if c else None
                it = {"flag": f, "label": name, "n": a, "pct": round(100 * a / B, 1),
                      "market_pct": round(100 * c / C, 1),
                      "index": round(idx, 1) if idx else None,
                      "sig": qv.get(f, 1.0) < 0.05 and a >= 10}
                if f.startswith("to:"):
                    it["filed"] = filed[f[3:]]
                if caveat:
                    it["caveat"] = caveat
                out.append(it)
            groups.append({"key": g, "label": label, "items": out})
        return {"bank": bc, "product": product, "days": days, "total": B,
                "filed": int(own[2]), "threat": int(own[3]), "groups": groups}
    return _cached(f"rf:{bc}:{product}:{days}", _compute)


def _int_sp(n: int) -> str:
    return f"{n:,}".replace(",", "\u00a0")


def _pct_int(x) -> int:
    """Целый процент с округлением «половина вверх», как Math.round на фронте:
    round() в Python банковский (22,5 → 22), и шапка расходилась с «Главным»."""
    x = float(x or 0)
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


@_safe(None)
def changes(bank: str, product: str | None = None, days: int = 90) -> dict | None:
    """Шапка «что изменилось» для руководителя: только значимые изменения к
    прошлому равному окну — объём, темы, опережающие общий поток, доля
    эскалации — и всплеск текущей недели. Всё детерминировано; если значимого
    нет, так и пишем: «спокойно» — тоже вывод."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    ov = overview(bc, product, days) or {}
    if ov.get("delta_partial"):
        return {"days": days, "items": [], "partial": True}
    items = []
    n, prev = int(ov.get("total") or 0), int(ov.get("prev") or 0)
    ch = _rate_change(n, prev)
    if ch and ch["p"] < 0.05 and abs(ov.get("delta_pct") or 0) >= 10:
        up = n > prev
        items.append({"kind": "volume", "dir": "up" if up else "down",
                      "text": f"Жалоб {'больше' if up else 'меньше'} на {abs(_pct_int(ov['delta_pct']))}%",
                      "detail": f"{_int_sp(n)} против {_int_sp(prev)} за прошлые {days} дн, 95% ДИ {ch['lo']:+d}…{ch['hi']:+d}%"})
    th = themes(bc, product, days) or {}
    rows = [t for t in th.get("themes") or [] if t.get("delta_sig") and t["key"] != "other"]
    ups = sorted([t for t in rows if (t.get("excess") or 0) > 0], key=lambda t: -t["excess"])[:2]
    downs = sorted([t for t in rows if (t.get("excess") or 0) < 0], key=lambda t: t["excess"])[:1]
    for t in ups + downs:
        up = t["excess"] > 0
        items.append({"kind": "theme", "dir": "up" if up else "down", "key": t["key"],
                      "text": f"{t.get('short') or t['label']}: {t['prev']} → {t['n']}",
                      "detail": (f"{t['label']} — " + ("растёт быстрее общего потока жалоб"
                                                      if up else "снижается сильнее общего потока жалоб")
                                 + f" ({'+' if (ov.get('delta_pct') or 0) >= 0 else ''}"
                                 f"{_pct_int(ov.get('delta_pct'))}%)")})
    e1, e0 = int(ov.get("esc_n") or 0), int(ov.get("esc_prev_n") or 0)
    if n and prev and (e1 + e0) >= 20:
        p1, p0 = e1 / n, e0 / prev
        pp = (e1 + e0) / (n + prev)
        se = math.sqrt(pp * (1 - pp) * (1 / n + 1 / prev)) if 0 < pp < 1 else 0
        if se and _p2((p1 - p0) / se) < 0.05 and abs(p1 - p0) >= 0.01:
            items.append({"kind": "esc", "dir": "up" if p1 > p0 else "down",
                          "text": (f"Эскалация {str(round(100 * p0, 1)).replace('.', ',')}% → "
                                   f"{str(round(100 * p1, 1)).replace('.', ',')}%"),
                          "detail": "доля жалоб с угрозой или обращением в ЦБ, суд, прокуратуру"})
    wk = weekly_signals(bc, product) or {}
    for sgl in (wk.get("signals") or [])[:1]:
        items.append({"kind": "signal", "dir": "up", "key": sgl["key"],
                      "text": (f"Всплеск недели: {sgl.get('short') or sgl['label']} "
                               + ("— новое" if sgl.get("new") else
                                  f"×{str(sgl.get('ratio')).replace('.', ',')}")),
                      "detail": (f"{sgl['label']}: {sgl['week']} жалоб за 7 дней при норме "
                                 f"~{str(sgl['baseline_week']).replace('.', ',')} в неделю")})
    return {"days": days, "items": items, "partial": False,
            "overall_delta_pct": ov.get("delta_pct")}


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
                limit: int = 10000, flag: str | None = None,
                source: str | None = None) -> list[dict] | None:
    """Жалобы с разметкой для выгрузки в таблицу — те же фильтры, что у ленты
    (без поиска по смыслу: он возвращает топ-300 похожих, а выгрузка — это
    полный срез). Раньше такой срез собирали вручную по запросу коллег."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    if theme and theme not in cb.ISSUES:
        return []
    fcond = _flag_sql(flag)
    if fcond is None:
        return []
    p: dict = {"bank": bc, "product": product, "lim": max(1, min(limit, 20000)),
               "sv": _ann_schema()}
    extra = _source_clause("i", source, p)
    if extra is None:
        return []
    if days:
        extra += " AND i.dt >= now() - make_interval(days => :d)"
        p["d"] = days
    if city:
        extra += " AND i.city = :city"
        p["city"] = city
    extra += _month_clause("i", month, p)
    if esc:
        extra += " AND i.esc"
    if theme:
        extra += " AND i.issue = :tkey"
        p["tkey"] = theme
    if fcond:
        extra += f" AND {fcond}"
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
                     esc: bool = False, flag: str | None = None,
                     source: str | None = None, sort: str = "date") -> dict:
    """Лента по ЕДИНОМУ индексу — все источники в одном списке.

    Показываются жалобы из разметки и ещё не размеченные свежие отзывы (они
    размечаются в течение часа — прятать самые свежие нельзя). Похвала,
    вопросы, мусор и копии в ленту жалоб не идут. Фильтр по теме — по главной
    проблеме, как и счётчик риск-карты: клик по строке показывает ровно те
    жалобы, что в ней посчитаны."""
    fetch = min(max((limit + offset) * 5, 40), 600)
    p: dict = {"bank": bc, "product": product, "lim": fetch}
    extra = _source_clause("i", source, p)
    if extra is None:
        return {"items": [], "mode": "feed", "error": "unknown_source"}
    if days:
        extra += " AND i.dt >= now() - make_interval(days => :d)"
        p["d"] = days
    if city:
        extra += " AND i.city = :city"
        p["city"] = city
    extra += _month_clause("i", month, p)
    if esc:
        extra += " AND i.esc"
    fcond = _flag_sql(flag)
    if fcond is None:
        return {"items": [], "mode": "feed", "error": "unknown_flag"}
    if fcond:
        # признак есть только у размеченной жалобы — неразмеченные свежие не берём
        extra += (" AND EXISTS (SELECT 1 FROM review_annotation a WHERE a.url = i.url"
                  f" AND a.schema_version = :sv AND {fcond})")
        p["sv"] = _ann_schema()
    if theme:
        if theme not in cb.ISSUES:
            return {"items": [], "mode": "feed", "error": "unknown_theme"}
        extra += f" AND i.issue = :tkey AND {_CMP}"
        p["tkey"] = theme
    elif fcond:
        extra += f" AND {_CMP}"
    else:
        extra += f" AND (i.kind IS NULL OR {_CMP})"
    # «Сначала серьёзные» — по признакам разметки; при равенстве свежие выше
    sev = sort == "severity"
    if sev:
        p["sv"] = _ann_schema()
    join = ("LEFT JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv"
            if sev else "")
    sev_col = f", {_severity_sql()} AS sev" if sev else ""
    order = "sev DESC NULLS LAST, i.dt DESC NULLS LAST" if sev else "i.dt DESC NULLS LAST"
    try:
        with db.session() as s:
            rows = [dict(r) for r in s.execute(text(f"""
                SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt,
                       i.city, i.rating{sev_col}
                FROM review_index i {join}
                WHERE i.bank = :bank
                  AND (i.dt IS NULL OR i.dt <= now())
                  AND (CAST(:product AS text) IS NULL OR i.product = :product){extra}
                ORDER BY {order}
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
                    **({"sev": int(r["sev"] or 0)} if sev else {}),
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
    extra += _month_clause("f", month, p)
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


_BANKI_ID = re.compile(r"banki\.ru/services/responses/bank/response/(\d+)")


def _bank_reply_for(urls: list[str]) -> dict[str, dict]:
    """Ответ банка и «решено» по показанным отзывам — из сбора площадок
    (sources/review_streams): у banki.ru по номеру отзыва (корпус и наш сбор
    дают одну и ту же жалобу под разными ссылками), у sravni — по ссылке."""
    ids = {u: m.group(1) for u in urls if (m := _BANKI_ID.search(u or ""))}
    sravni = [u for u in urls if "sravni.ru" in (u or "")]
    out: dict[str, dict] = {}
    if not ids and not sravni:
        return out
    try:
        with db.session() as s:
            if ids:
                rows = s.execute(text("""
                    SELECT source_review_id, raw FROM review
                    WHERE source = 'banki_reviews' AND source_review_id = ANY(:ids)
                      AND raw ? 'seen_at'"""), {"ids": list(set(ids.values()))}).all()
                by = {r[0]: r[1] or {} for r in rows}
                for u, rid in ids.items():
                    raw = by.get(rid)
                    if raw:
                        out[u] = {"answer": raw.get("answer"), "resolved": raw.get("resolved"),
                                  "checked": raw.get("countable"), "src": "banki.ru"}
            if sravni:
                rows = s.execute(text("""
                    SELECT source_url, raw FROM review
                    WHERE source = 'sravni_reviews' AND source_url = ANY(:u)"""), {"u": sravni}).all()
                for u, raw in rows:
                    raw = raw or {}
                    out[u] = {"answer": None, "resolved": raw.get("problem_solved"),
                              "has_answer": raw.get("company_response"), "src": "sravni.ru"}
    except Exception as e:  # noqa: BLE001 — лента не должна падать из-за этого
        log.warning("reviews_dash: ответ банка не забрался (%s)", e)
    return out


def _attach_themes(items: list[dict]) -> None:
    """Темы, разбор и человекочитаемый источник — общий финиш ленты и поиска."""
    for r in items:
        src = r.get("source")
        if src:
            r["source"] = _SOURCE_LABEL.get(src, src)
    replies = _bank_reply_for([i["url"] for i in items if i.get("url")])
    for r in items:
        if r.get("url") in replies:
            r["bank_reply"] = replies[r["url"]]
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
            "event_date": str(a["event_date"]) if a.get("event_date") else None,
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
                    esc: bool = False, sort: str = "auto", flag: str | None = None,
                    source: str | None = None) -> dict:
    """Лента доказательной базы. q → поиск; иначе свежие с фильтрами
    тема/город/месяц. Дубли (массовые однотипные жалобы) не прячем, а считаем —
    массовость это аудит-сигнал → поле `similar`.

    Возвращает {items, mode, error}: упавший поиск и честное «ничего не
    нашлось» не должны выглядеть одинаково."""
    bc = resolve_bank(bank) if bank else None
    if theme and theme not in cb.ISSUES:
        return {"items": [], "mode": "search" if q else "feed", "error": "unknown_theme"}
    if _flag_sql(flag) is None:
        return {"items": [], "mode": "search" if q else "feed", "error": "unknown_flag"}
    if _source_clause("i", source, {}) is None:
        return {"items": [], "mode": "search" if q else "feed", "error": "unknown_source"}
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
            # месяц события поиск по корпусу не знает — его отбирает _keep_urls
            res = search_reviews(q, bank=bc, product=None, since_days=days,
                                 theme_rx=None, city=city,
                                 month=None if (month or "").startswith("ev:") else month,
                                 k=k, strict=True, _meta=meta)
        except Exception as e:
            log.warning("reviews_dash: поиск по %r упал: %s", q, e)
            return {"items": [], "mode": "search", "error": "search_failed"}
        keep = _keep_urls([r.get("url") for r in res if r.get("url")], theme=theme, esc=esc,
                          product=product, flag=flag, source=source,
                          month=month if (month or "").startswith("ev:") else None)
        res = [r for r in res if r.get("url") in keep]
        if sort == "date":
            res.sort(key=lambda r: (r.get("date") or ""), reverse=True)
        page = res[offset:offset + limit]
        _attach_themes(page)
        return {"items": page, "mode": "search", "error": None, "search": meta,
                "has_more": len(res) > offset + limit}
    if not bc:
        return {"items": [], "mode": "feed", "error": "unknown_bank"}
    return _feed_from_index(bc, product, theme, days, city, month, limit, offset, esc, flag,
                            source=source, sort="severity" if sort == "severity" else "date")


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


_SOURCE_RU = {"bankiru": "banki.ru — корпус", "banki_reviews": "banki.ru — наш сбор",
              "sravni_reviews": "sravni.ru", "finuslugi_reviews": "finuslugi.ru",
              "bankiros_reviews": "bankiros.ru"}


def source_health() -> dict:
    """Полнота площадок отзывов — для «Пульса».

    Неделя — последние 7 полных дней с данными, норма — медиана 8 недель до
    неё (по дате отзыва). Падение ниже 60% нормы — «просел», ноль при норме от
    5 в неделю — «встал»; площадки с нормой меньше 5 в неделю помечаются
    «малый поток» (finuslugi: около отзыва в день на всю площадку, это не
    поломка). Отдельно — банки, пропавшие из корпуса (≥20 жалоб в месяц в
    среднем за полгода до этого и ни одной за 45 дней): так в мае 2026 исчез
    «Почта Банк», и вкладка молча показывала «1 жалоба за квартал»."""
    out: dict = {"sources": [], "gone_banks": [], "week_end": week_end()}
    with db.session() as s:
        # корпус — по индексу; наши сборщики — по собранному: в индекс они
        # намеренно пишут не всё (похвалу и дубли корпуса не берём), и объём
        # индекса выглядел бы вечной «просадкой»
        rows = s.execute(text(f"""
            WITH e AS (SELECT {_WEEK_END} AS t)
            SELECT i.source, floor(extract(epoch FROM e.t - i.dt) / 604800)::int AS w, count(*)
            FROM review_index i, e
            WHERE i.source = 'bankiru' AND i.dt >= e.t - interval '63 days' AND i.dt < e.t
            GROUP BY 1, 2
            UNION ALL
            SELECT r.source, floor(extract(epoch FROM e.t - r.posted_at) / 604800)::int, count(*)
            FROM review r, e
            WHERE r.source IN ('banki_reviews', 'sravni_reviews', 'finuslugi_reviews', 'bankiros_reviews')
              AND r.posted_at >= e.t - interval '63 days' AND r.posted_at < e.t
            GROUP BY 1, 2""")).all()
        runs = {r[0]: r for r in s.execute(text("""
            SELECT DISTINCT ON (source) source, started_at, status, left(coalesce(error, ''), 160)
            FROM extraction_run WHERE source LIKE '%review%'
            ORDER BY source, started_at DESC""")).all()}
        gone = s.execute(text(f"""
            SELECT i.bank, count(*) FILTER (WHERE i.dt BETWEEN now() - interval '225 days'
                                                          AND now() - interval '45 days') / 6.0 AS per_month,
                   max(i.dt)::date AS last
            FROM review_index i
            WHERE i.source = 'bankiru' AND {_CMP} AND i.dt > now() - interval '225 days' AND i.dt <= now()
            GROUP BY 1
            HAVING count(*) FILTER (WHERE i.dt > now() - interval '45 days') = 0
               AND count(*) FILTER (WHERE i.dt BETWEEN now() - interval '225 days'
                                                  AND now() - interval '45 days') >= 120
            ORDER BY 2 DESC""")).all()
    import statistics
    by: dict[str, list[int]] = {}
    for src, w, n in rows:
        if 0 <= int(w) <= 8:
            by.setdefault(src, [0] * 9)[int(w)] += int(n)
    for src in sorted(set(by) | set(_SOURCE_RU), key=lambda k: -(by.get(k, [0])[0])):
        arr = by.get(src, [0] * 9)
        norm = statistics.median(arr[1:9])
        wk = arr[0]
        if norm < 5:
            status = "малый поток"
        elif wk == 0:
            status = "встал"
        elif wk < 0.6 * norm:
            status = "просел"
        else:
            status = "норма"
        run = runs.get(src)
        out["sources"].append({"source": src, "label": _SOURCE_RU.get(src, src), "week": wk,
                               "norm": round(norm, 1), "status": status,
                               "last_run": run[1].isoformat() if run else None,
                               "last_run_status": run[2] if run else None,
                               "last_error": (run[3] or None) if run else None})
    out["gone_banks"] = [{"bank": b, "per_month": round(float(pm)), "last": str(last)}
                         for b, pm, last in gone]
    return out


@_safe(None)
def segment_profile(bank: str, product: str | None = None, city: str | None = None,
                    month: str | None = None, days: int = 90) -> dict | None:
    """Чем срез (город или месяц) отличается от нормы — по главной проблеме.

    Город сравнивается со всей страной за тот же период, месяц — с шестью
    предыдущими (месяц события «ev:YYYY-MM» — с шестью предыдущими месяцами
    событий). Индекс = доля проблемы в срезе / доля в норме. flagged —
    отмечен ли срез аномалией по правилам вкладки (гео — индекс доли банка в
    городе, месяц — пик динамики): модель не должна называть аномалией то, что
    ею не отмечено, как было с Краснодаром 25.09."""
    bc = resolve_bank(bank)
    if not bc or not (city or month):
        return None
    p: dict = {"bank": bc, "product": product, "d": days, "city": city}
    ev = bool(month and month.startswith("ev:"))
    if city:
        seg = "i.city = :city AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
        base = "i.dt >= now() - make_interval(days => :d) AND i.dt <= now()"
        base_label = f"вся страна за те же {days} дн"
    else:
        m = _MONTH_RX.match(month or "")
        if not m:
            return None
        p["month"] = m.group(2)
        col = "i.ev_date" if ev else "i.dt"
        seg = f"date_trunc('month', {col}) = to_date(:month, 'YYYY-MM')"
        base = (f"{col} >= to_date(:month, 'YYYY-MM') - interval '6 months'"
                f" AND {col} < to_date(:month, 'YYYY-MM')")
        base_label = "6 предыдущих месяцев" + (" событий" if ev else "")
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
        t = trend(bank, product, basis="event" if ev else "pub") or {}
        flagged = any(x.get("ym") == p["month"] and x.get("spike") for x in t.get("series") or [])
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
                       a.quote_ok, a.code_fit, a.new_topic, a.product, a.event_date
                FROM review_annotation a
                WHERE a.schema_version = :sv AND a.url = ANY(:u)
            """), {"sv": _ann_schema(), "u": urls}).mappings().all()
    except Exception as e:                                     # noqa: BLE001
        log.warning("reviews_dash: разметка показанных отзывов не забралась (%s)", e)
        return {}
    return {r["url"]: dict(r) for r in rows}


def _keep_urls(urls: list[str], *, theme: str | None, esc: bool,
               product: str | None = None, flag: str | None = None,
               month: str | None = None, source: str | None = None) -> set[str]:
    """Какие из найденных отзывов показывать: жалобы (и ещё не размеченные),
    при заданной теме — с этой главной проблемой, при флажке — с эскалацией,
    при признаке — с этим признаком разметки."""
    if not urls:
        return set()
    fcond = _flag_sql(flag) or ""
    cond = [f"(i.kind IS NULL OR {_CMP})" if not (theme or fcond) else _CMP]
    p: dict = {"u": urls}
    if fcond:
        cond.append("EXISTS (SELECT 1 FROM review_annotation a WHERE a.url = i.url"
                    f" AND a.schema_version = :sv AND {fcond})")
        p["sv"] = _ann_schema()
    mc = _month_clause("i", month, p)
    if mc:
        cond.append(mc[len(" AND "):])
    sc = _source_clause("i", source, p) or ""
    if sc:
        cond.append(sc[len(" AND "):])
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
    return ok | ({u for u in urls if u not in known}
                 if not (theme or esc or product or fcond or month or sc) else set())


def _nb_tail(x: int, weeks: list[int]) -> float:
    """P(X ≥ x) при недельной норме из истории — отрицательно-биномиальное
    распределение с разбросом, оценённым по самим неделям (жалобы идут
    волнами, и пуассоновский порог на них даёт ложные всплески). Если разброс
    не больше среднего — обычный Пуассон."""
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


def _p2(z: float) -> float:
    """Двустороннее p-значение для нормальной z-статистики."""
    return math.erfc(abs(z) / math.sqrt(2))


def _ratio_ci(a: int, n1: int, c: int, n2: int) -> dict | None:
    """Отношение долей (a/n1) / (c/n2) с 95% ДИ и p-значением.

    Лог-нормальное приближение; ноль заменяется на 0,5, иначе интервал
    бесконечен. Выборки должны быть независимы: банк против ОСТАЛЬНОГО рынка,
    город против остальной страны — не против целого, в который входит сам."""
    if n1 <= 0 or n2 <= 0:
        return None
    a_, c_ = (a if a > 0 else 0.5), (c if c > 0 else 0.5)
    rr = (a_ / n1) / (c_ / n2)
    var = 1 / a_ - 1 / n1 + 1 / c_ - 1 / n2
    se = math.sqrt(var) if var > 0 else 0.0
    return {"rr": rr, "lo": rr * math.exp(-1.96 * se), "hi": rr * math.exp(1.96 * se),
            "p": _p2(math.log(rr) / se) if se else 1.0}


def _rate_change(n: int, prev: int) -> dict | None:
    """Изменение счётчика к прошлому равному окну: 95% ДИ в процентах и p
    (пуассоновское отношение интенсивностей)."""
    if n <= 0 or prev <= 0:
        return None
    se = math.sqrt(1 / n + 1 / prev)
    lr = math.log(n / prev)
    return {"lo": round(100 * (math.exp(lr - 1.96 * se) - 1)),
            "hi": round(100 * (math.exp(lr + 1.96 * se) - 1)), "p": _p2(lr / se)}


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
