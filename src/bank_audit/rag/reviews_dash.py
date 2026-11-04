"""Аналитика отзывов для вкладки «Отзывы» (риск-радар голоса клиента).

Агрегаты поверх единого индекса отзывов всех площадок (review_index) с
LLM-разметкой: KPI, помесячная динамика + детект спайков, таксономия тем с
трендом и категорией риска, индекс «банк против рынка» по проблемам, география
(индекс доли банка в городе), признаки риска, лента. Что считается жалобой —
_CMP ниже; слова для людей и ИИ — rag/review_glossary.

Все тяжёлые агрегаты bank-scoped (подмножество ≤50к строк) → быстро.
Кэш на процесс с TTL (агрегаты считаются раз в ~час).
"""
from __future__ import annotations

import datetime as _dt
import functools
import logging
import dataclasses
import math
import os
import re
import threading
import time

from sqlalchemy import text

from .. import db
from .bankiru_reviews import resolve_bank, search_reviews
from . import review_codebook as cb
from . import review_period as _rp

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


_TTL_PARTIAL = 300.0


def _cached(key: str, fn, ttl: float = _TTL):
    """Кэш с TTL. Ответ «прошлый период ещё размечается» (delta_partial) живёт
    5 минут, а не час: это бывает и на минуту — пока переиндексация вставляет
    строки без разметки, — и час без сравнения из-за такой минуты слишком дорог."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < (_TTL_PARTIAL if _partial(hit[1]) else ttl):
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def _partial(val) -> bool:
    return isinstance(val, dict) and bool(val.get("delta_partial") or val.get("partial"))


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
        cl.append(f'al_product_is({alias}.product, {alias}.slices, :product)')
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
# Жалоба = отзыв, который разметка отнесла к претензиям, и не копия другого отзыва:
# автор пишет одну историю на нескольких площадках — это одна жалоба (rag/review_dedup,
# review_index.dup_of; с 04.11.2026, до того копии между площадками считались дважды)
# Копия выпадает, только если её каноническая сама считается жалобой: каноническую
# могли разметить как мусор (сбой чтения текста), вопрос или ещё не разметить — тогда
# жалобу несёт копия (ревью 04.11.2026: иначе такая жалоба пропадала из всех чисел).
_CANON_CMP = ("EXISTS (SELECT 1 FROM review_index c WHERE c.url = {a}.dup_of"
              " AND c.kind IN ('complaint', 'mixed'))")
_CMP = ("(i.kind IN ('complaint', 'mixed') AND (i.dup_of IS NULL OR NOT "
        + _CANON_CMP.format(a="i") + "))")
_CANON_CMP_F = _CANON_CMP.format(a="f")


def _ann_schema() -> str:
    from .review_annotate import SCHEMA
    return SCHEMA


# Срез «месяц»: «2026-05» — по дате отзыва, «ev:2026-05» — по дате события
# (режим динамики «по дате события»). Один параметр проходит через ленту,
# выгрузку, профиль среза и разбор моделью без смены их сигнатур.
_MONTH_RX = re.compile(r"^(ev:)?(\d{4}-\d{2})$")


# Неделя всплеска: sig:<последний день недели> — ровно те жалобы, из которых сложилось
# число сигнала (7 дней по этот день, события не старше 60 дней — как в
# _topic_day_counts). До 04.11.2026 клик по всплеску «×2,1 — 40 жалоб» открывал
# тему за весь период вкладки — сотни жалоб, и «40» в них было не найти.
_SIG_RX = re.compile(r"^sig:(\d{4}-\d{2}-\d{2})$")


def _sig_window(alias: str, d: str, p: dict) -> str:
    p["sig_d"] = d
    return (f"{alias}.dt >= CAST(:sig_d AS date) - 6 AND {alias}.dt < CAST(:sig_d AS date) + 1"
            f" AND ({alias}.ev_date IS NULL OR {alias}.ev_date >= {alias}.dt::date - 60)")


def _month_clause(alias: str, month: str | None, p: dict) -> str:
    if not month:
        return ""
    sg = _SIG_RX.match(month)
    if sg:
        return " AND " + _sig_window(alias, sg.group(1), p)
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


def _per(period, days, product: str | None = None) -> "_rp.Period":
    """Период вызова: явный (вкладка — даты «с — по») или «последние N дней»
    (выпуск, ИИ-инструменты, оповещения и старые ссылки зовут с числом дней)."""
    if isinstance(period, _rp.Period):
        return period
    return _rp.rolling(int(days or 90), data_start_for(product))


def data_start_for(product: str | None) -> "_dt.date":
    """С какого дня есть данные: корпус — с начала 2025, срезы («Зарплатные
    проекты», «УС», «Выездной сервис») — с начала своей разметки. Иначе прошлый
    период среза был бы почти пуст, и «12 мес» показывали бы рост в разы."""
    from . import review_slices as rs
    from ..directions import by_product_param
    d = by_product_param(product)
    if (product and product in {lbl for lbl, _d in rs.SLICES.values()}) or (d and d.review_slices):
        return max(_rp.DATA_START, _dt.date.fromisoformat(rs.SINCE))
    return _rp.DATA_START


def _coverage(bank_canon: str | None, per) -> float | None:
    """Доля отзывов окна (период вместе с прошлым), у которых уже есть разметка, %."""
    per = _per(per, per)
    try:
        with db.session() as s:
            n, lab = s.execute(text(
                "SELECT count(*), count(*) FILTER (WHERE i.kind IS NOT NULL) FROM review_index i"
                " WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)"
                " AND i.dt >= :lo AND i.dt < :t1 AND i.dt <= now()"),
                {"bank": bank_canon, **per.binds()}).one()
        return round(100.0 * lab / n, 1) if n else None
    except Exception:  # noqa: BLE001
        return None


def _prev_ready(bank_canon: str | None, per) -> bool:
    """Размечен ли прошлый период целиком (≥97%).

    Массовая разметка идёт от свежих отзывов к старым: пока прошлый период
    размечен наполовину, в нём «не хватает» жалоб, и сравнение даёт ложный
    рост (+170% вместо реальных единиц процентов). Такое сравнение не
    показываем вовсе. Нет сравнения — нечего и проверять."""
    per = _per(per, per)
    if not per.has_cmp:
        return False
    try:
        with db.session() as s:
            n, lab = s.execute(text(
                "SELECT count(*), count(*) FILTER (WHERE i.kind IS NOT NULL) FROM review_index i"
                " WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)"
                " AND i.dt >= :p0 AND i.dt < :p1"),
                {"bank": bank_canon, **per.binds()}).one()
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
def overview(bank: str, product: str | None = None, days: int = 90,
             period: "_rp.Period | None" = None) -> dict | None:
    """KPI вкладки по жалобам из LLM-разметки.

    Жалоба — отзыв, который модель отнесла к жалобам (в т. ч. смешанный:
    претензия и похвала вместе). Похвала, вопросы, мусор и копии одного отзыва
    в счёт не идут — раньше они давали до трети «роста». Эскалация — признак
    разметки («грозит» или «уже обратился» в ЦБ, суд, прокуратуру и т. п.), а
    не регулярка по словам «в суд»: та ошибалась в половине случаев.

    Изменение к прошлому периоду — по сравниваемой части (дни с полными
    данными) и в пересчёте на день, если прошлый период другой длины или
    частично раньше начала данных (review_period)."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    per = _per(period, days, product)

    def _compute():
        idx, ip = _idx_clause(bc, product)
        B = per.binds()
        cur_w = "i.dt >= :t0 AND i.dt < :t1"
        cmp_w = "i.dt >= :t0 AND i.dt < :c1"
        prev_w = "i.dt >= :p0 AND i.dt < :p1"
        pq = " AND al_product_is(i.product, i.slices, :product)" if product else ""
        pp = {"product": product} if product else {}
        with db.session() as s:
            cur = s.execute(text(f"""
                SELECT count(*) FILTER (WHERE {cur_w}),
                       count(*) FILTER (WHERE {prev_w}),
                       count(*) FILTER (WHERE {cur_w} AND i.esc),
                       count(*) FILTER (WHERE {prev_w} AND i.esc),
                       count(*) FILTER (WHERE {cmp_w}),
                       count(*) FILTER (WHERE {cmp_w} AND coalesce(i.source, 'bankiru') = :csrc),
                       count(*) FILTER (WHERE {prev_w} AND coalesce(i.source, 'bankiru') = :csrc)
                FROM review_index i WHERE {idx} AND {_CMP} AND i.dt >= :lo AND i.dt < :t1
            """), {**ip, **B, "csrc": CMP_SOURCE}).one()
            total_cur, total_prev, esc_cur = int(cur[0]), int(cur[1]), int(cur[2])
            esc_prev, total_cmp = int(cur[3]), int(cur[4])
            c_cmp, c_prev = int(cur[5]), int(cur[6])          # сопоставимый корпус — для «к рынку»
            filed = int(s.execute(text(f"""
                SELECT count(*) FROM review_index i
                JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
                WHERE {idx} AND {_CMP} AND {cur_w}
                  AND a.esc = 'filed'
            """), {**ip, **B, "sv": _ann_schema()}).scalar() or 0)
            mk = s.execute(text(
                "SELECT i.bank, count(*) n FROM review_index i"
                f" WHERE {cur_w} AND i.dt <= now() AND {_CMP}" + pq +
                " GROUP BY 1 ORDER BY 2 DESC"), {**B, **pp}).all()
            asof = s.execute(text(
                f"SELECT max(i.dt) FROM review_index i WHERE {idx} AND {_CMP}"), ip).scalar()
            # эскалация у остальных банков — с чем сравнивать долю банка: порог
            # 12% у крупного банка пробит всегда, и плитка была красной постоянно
            # прошлое окно рынка — чтобы рост жалоб банка сравнивать с ростом площадок:
            # «+20%» при росте рынка на 12% — это не «стало хуже на 20%» (аудит 03.10)
            m_n, m_esc, m_prev, m_cmp = s.execute(text(
                f"SELECT count(*) FILTER (WHERE {cur_w}),"
                f" count(*) FILTER (WHERE {cur_w} AND i.esc),"
                f" count(*) FILTER (WHERE {prev_w} AND coalesce(i.source, 'bankiru') = :csrc),"
                f" count(*) FILTER (WHERE {cmp_w} AND coalesce(i.source, 'bankiru') = :csrc)"
                f" FROM review_index i WHERE i.bank <> :bank AND {_CMP}"
                f" AND i.dt >= :lo AND i.dt < :t1 AND i.dt <= now()" + pq),
                {"bank": bc, **B, **pp, "csrc": CMP_SOURCE}).one()
            by_src = [{"source": r[0], "n": int(r[1])} for r in s.execute(text(
                f"SELECT i.source, count(*) FROM review_index i WHERE {idx} AND {_CMP}"
                f" AND {cur_w} GROUP BY 1 ORDER BY 2 DESC"), {**ip, **B}).all()]
        total_market = sum(int(r[1]) for r in mk) or 1
        m_n, m_esc = int(m_n or 0), int(m_esc or 0)
        m_prev, m_cmp = int(m_prev or 0), int(m_cmp or 0)
        esc_sig = False
        if total_cur >= 30 and m_n >= 100:
            p1, p0 = esc_cur / total_cur, m_esc / m_n
            pp_ = (esc_cur + m_esc) / (total_cur + m_n)
            se = math.sqrt(pp_ * (1 - pp_) * (1 / total_cur + 1 / m_n)) if 0 < pp_ < 1 else 0
            esc_sig = bool(se and _p2((p1 - p0) / se) < 0.01 and p0 and p1 / p0 >= 1.2)
        ready = _prev_ready(bc, per)
        delta = per.delta_pct(total_cmp, total_prev) if ready else None
        # рост к рынку — по сопоставимому корпусу с обеих сторон (CMP_SOURCE): новый свой
        # источник жалоб на банк иначе выглядел как «банк растёт быстрее рынка»
        vm = _vs_market(c_cmp, c_prev, m_cmp, m_prev, per.k) if ready else {}
        return {
            "bank": bc, "product": product, "days": per.days, "period": per.info(),
            "total": total_cur, "prev": total_prev, "cmp_n": total_cmp,
            "prev_expected": round(per.expected(total_prev)) if per.has_cmp else None,
            "delta_pct": delta,
            "market_delta_pct": vm.get("market_delta_pct"), "vs_market": vm.get("vs_market"),
            "delta_vs_market_pct": vm.get("rel_pct"),
            "delta_low_n": bool(total_prev and min(total_cmp, total_prev) < 30),
            # прошлый период ещё размечается (сравнения нет, но будет)
            "delta_partial": per.has_cmp and not ready,
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
            "coverage": _coverage(bc, per),
        }
    return _cached(f"ov:{bc}:{product}:{per.key}", _compute)


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
def themes(bank: str, product: str | None = None, days: int = 90,
           period: "_rp.Period | None" = None) -> dict | None:
    """Риск-карта: распределение жалоб по ГЛАВНОЙ проблеме из LLM-разметки.

    У каждой жалобы ровно одна главная проблема, поэтому доли в сумме дают
    100%: прежняя векторная разметка давала отзыву до двух тем, вторая была
    неверна в половине случаев, и сумма долей переваливала за сотню. Где
    проблема упомянута как дополнительная — отдельным числом «ещё в N».

    n — жалобы за весь период (доли считаются по ним), cmp — за сравниваемую
    часть периода (дни с полными данными): изменение к прошлому — по ней."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    per = _per(period, days, product)

    def _compute():
        p = {"bank": bc, "product": product, **per.binds()}
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT i.issue,
                       count(*) FILTER (WHERE i.dt >= :t0 AND i.dt < :t1) n,
                       count(*) FILTER (WHERE i.dt >= :p0 AND i.dt < :p1) p,
                       count(*) FILTER (WHERE i.dt >= :t0 AND i.dt < :c1) c
                FROM review_index i
                WHERE i.bank = :bank AND {_CMP}
                  AND i.dt >= :lo AND i.dt < :t1 AND i.dt <= now()
                  AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
                GROUP BY 1"""), p).all()
            also = dict(s.execute(text(f"""
                SELECT x, count(*) FROM review_index i, unnest(i.issues2) x
                WHERE i.bank = :bank AND {_CMP}
                  AND i.dt >= :t0 AND i.dt < :t1 AND i.dt <= now()
                  AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
                GROUP BY 1"""), p).all())
        total = sum(int(r[1]) for r in rows) or 1
        total_prev = sum(int(r[2]) for r in rows)
        total_cmp = sum(int(r[3]) for r in rows)
        ready = _prev_ready(bc, per)
        # Значимость изменения темы — против ОБЩЕГО потока: если жалоб в целом
        # стало на 30% больше, тема с +35% не «растёт», она идёт вместе со всеми.
        # Проверка: доля текущего окна в сумме двух окон у темы против той же
        # доли у всех жалоб (биномиальная, нормальное приближение), с поправкой
        # на число тем. Меньше 20 жалоб в двух окнах — «мало данных» (Б4).
        # Длина окон в этой проверке сокращается, поэтому она верна и для
        # месяцев разной длины, и для прошлого периода, обрезанного началом данных.
        share_cur = total_cmp / (total_cmp + total_prev) if total_prev and total_cmp else None
        pv: dict[str, float] = {}
        out = []
        for code, n, prev, c in rows:
            o = cb.issue_obj(code)
            if not o or code == "no_issue":
                continue
            n, prev, c = int(n), int(prev), int(c)
            if not n and not prev:
                continue
            row = {**o, "n": n, "prev": prev, "cmp_n": c, "pct": round(100.0 * n / total, 1),
                   "n_also": int(also.get(code, 0)),
                   "delta_pct": (None if not ready else
                                 round(100.0 * (c / per.expected(prev) - 1)) if prev
                                 else (None if c == 0 else 100))}
            if ready and share_cur is not None:
                m = c + prev
                row["delta_low"] = m < 20 or prev < 5
                if not row["delta_low"]:
                    z = (c - m * share_cur) / math.sqrt(m * share_cur * (1 - share_cur))
                    pv[code] = _p2(z)
                    ci = _rate_change(c, prev, per.k)
                    if ci:
                        row["delta_ci"] = [ci["lo"], ci["hi"]]
                    row["excess"] = round(c - prev * total_cmp / total_prev)
            out.append(row)
        qv = _bh(pv) if pv else {}
        for row in out:
            if row["key"] in qv:
                row["delta_sig"] = qv[row["key"]] < 0.05
        out.sort(key=lambda x: (x["key"] == "other", -x["n"]))
        return {"bank": bc, "product": product, "days": per.days, "period": per.info(),
                "total": total, "total_prev": total_prev, "total_cmp": total_cmp,
                "overall_delta_pct": (round(100.0 * (total_cmp / per.expected(total_prev) - 1))
                                      if ready and total_prev and per.has_cmp else None),
                "themes": out, "src": "annotation", "coverage": _coverage(bc, per),
                "delta_partial": per.has_cmp and not ready}
    return _cached(f"th:{bc}:{product}:{per.key}", _compute)


@_safe(None)
def vs_market(bank: str, product: str | None = None, days: int = 90, top: int = 8,
              period: "_rp.Period | None" = None) -> dict | None:
    bc = resolve_bank(bank)
    if not bc:
        return None
    per = _per(period, days, product)

    def _compute():
        with db.session() as s:
            rows = s.execute(text(
                "SELECT i.bank, count(*) n FROM review_index i"
                " WHERE i.dt >= :t0 AND i.dt < :t1 AND i.dt <= now()"
                f" AND {_CMP}"
                + (" AND al_product_is(i.product, i.slices, :product)" if product else "") +
                " GROUP BY 1 ORDER BY 2 DESC"),
                {**per.binds(), **({"product": product} if product else {})}).all()
        total = sum(int(r[1]) for r in rows) or 1
        ranked = [{"bank": r[0], "n": int(r[1]), "pct": round(100.0 * int(r[1]) / total, 1),
                   "is_target": r[0] == bc} for r in rows]
        top_rows = ranked[:top]
        if not any(r["is_target"] for r in top_rows):
            tgt = next((r for r in ranked if r["is_target"]), None)
            if tgt:
                top_rows = top_rows[:top - 1] + [tgt]
        return {"bank": bc, "product": product, "days": per.days, "period": per.info(),
                "rows": top_rows}
    return _cached(f"vm:{bc}:{product}:{per.key}:{top}", _compute)


@_safe(None)
def issue_index(bank: str, product: str | None = None, days: int = 180,
                period: "_rp.Period | None" = None) -> dict | None:
    """Где структура жалоб банка отличается от рынка — по главной проблеме.

    Индекс = доля проблемы в жалобах банка / её доля в жалобах ОСТАЛЬНЫХ
    банков. Сравнивается структура, а не объём, поэтому ни размер банка, ни
    то, насколько охотно его клиенты пишут на площадки, на индекс не влияют —
    нормировка на число клиентов здесь не нужна. «Хуже рынка» — только
    значимое отличие: 95% ДИ индекса, поправка на число проверенных проблем,
    от 10 жалоб у банка и 30 у рынка, и практический порог (от ×1,25 или до
    ×0,8). excess — сколько жалоб у банка сверх того, что было бы при
    структуре рынка. Динамика — тот же индекс по четырём кварталам (90 дней),
    которые кончаются вместе с периодом. Окно не короче 90 дней: на месяце
    почти все проблемы «мало данных», — короткий период расширяется назад от
    своего конца, и ответ об этом говорит (window_from/window_to)."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    per = _per(period, days, product)
    t1 = per.t1
    t0 = min(per.t0, t1 - _dt.timedelta(days=90))
    ext = t0 < per.t0

    def _compute():
        nv = lambda t: t.replace(tzinfo=None)  # noqa: E731
        p = {"bank": bc, "product": product, "w0": nv(t0), "w1": nv(t1),
             "q0": nv(t1 - _dt.timedelta(days=360))}
        base = (f"{_CMP} AND i.bank IS NOT NULL"
                " AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product)) AND i.dt <= now()")
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT i.issue, (i.bank = :bank) AS own, count(*)
                FROM review_index i
                WHERE {base} AND i.dt >= :w0 AND i.dt < :w1
                GROUP BY 1, 2"""), p).all()
            qrows = s.execute(text(f"""
                SELECT i.issue, (i.bank = :bank) AS own,
                       floor(extract(epoch FROM CAST(:w1 AS timestamp) - i.dt) / 7776000)::int AS q,
                       count(*)
                FROM review_index i
                WHERE {base} AND i.dt >= :q0 AND i.dt < :w1
                GROUP BY 1, 2, 3"""), p).all()
        today = (t1 - _rp.DAY).date()
        win = {"days": max(per.days, 90), "period": per.info(),
               "window_from": t0.date().isoformat(), "window_to": today.isoformat(),
               "window_extended": ext}
        a_: dict[str, int] = {}
        c_: dict[str, int] = {}
        for code, own, n in rows:
            (a_ if own else c_)[code] = (a_ if own else c_).get(code, 0) + int(n)
        B, C = sum(a_.values()), sum(c_.values())
        if not B or not C:
            return {"bank": bc, "product": product, **win, "bank_total": B,
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
        return {"bank": bc, "product": product, **win, "bank_total": B,
                "market_total": C, "bank_share": round(100 * B / (B + C), 1),
                "tested": len(pv), "worse": worse[:8], "better": better[:4],
                "quarters": quarters}
    return _cached(f"ii:{bc}:{product}:{t0:%Y%m%d}-{t1:%Y%m%d}", _compute)


@_safe(None)
def geo(bank: str, product: str | None = None, days: int = 365, top: int = 8,
        period: "_rp.Period | None" = None) -> dict | None:
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
    per = _per(period, days, product)

    def _compute():
        p = {"bank": bc, "product": product, **per.binds()}
        win = (f"{_CMP} AND coalesce(i.city, '') <> '' AND i.bank IS NOT NULL"
               " AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))"
               " AND i.dt >= :t0 AND i.dt < :t1 AND i.dt <= now()")
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
            return {"bank": bc, "product": product, "days": per.days, "period": per.info(),
                    "cities": []}
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
        # more — сколько ещё городов с осмысленной базой (от 10 жалоб) не
        # показано: для «ещё N городов» на вкладке
        more = sum(1 for c in cities[top:] if c["n"] >= 10 and not c["anomaly"])
        return {"bank": bc, "product": product, "days": per.days, "period": per.info(),
                "national_share": round(100 * A / T, 1), "more": more,
                "cities": shown + [dict(c, extra=True) for c in extra[:3]]}
    return _cached(f"geo:{bc}:{product}:{per.key}:{top}", _compute)


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
def risk_flags(bank: str, product: str | None = None, days: int = 90,
               period: "_rp.Period | None" = None) -> dict | None:
    """Признаки риска из разметки: куда клиент грозит или уже обратился,
    уязвимые клиенты, «без согласия», «ввели в заблуждение», суммы. Для
    каждого — число и доля в жалобах банка против доли у остальных банков
    (значимость — с поправкой на число признаков). Код признака — фильтр ленты."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    per = _per(period, days, product)

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
                  AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
                  AND i.dt >= :t0 AND i.dt < :t1 AND i.dt <= now()
                GROUP BY 1"""), {"bank": bc, "product": product, **per.binds(),
                                 "sv": _ann_schema()}).all()
        own = next((r for r in rows if r[0]), None)
        mkt = next((r for r in rows if not r[0]), None)
        if not own or not mkt or not own[1]:
            return {"bank": bc, "product": product, "days": per.days, "period": per.info(),
                    "total": 0, "groups": []}
        B, C = int(own[1]), int(mkt[1])
        vals = {f: (int(own[4 + j]), int(mkt[4 + j])) for j, f in enumerate(codes)}
        filed = {k: int(own[4 + len(codes) + j]) for j, k in enumerate(_ESC_TO)}
        adj = _vuln_by_product(bc, product, per)
        pv = {}
        for f, (a, c) in vals.items():
            if f in adj and a >= 5:
                pv[f] = adj[f]["p"]        # уязвимые — против ожидания по продуктам
                continue
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
                if f in adj:
                    # Доля уязвимых зависит от продуктов: детские и пенсионные
                    # карты дают их сами по себе. Сравниваем с тем, сколько
                    # было бы при рыночной доле ВНУТРИ каждого продукта
                    # (косвенная стандартизация), а не со всем потоком
                    e = adj[f]["expected"]
                    it["expected_pct"] = round(100 * e / B, 1) if B else None
                    it["index_adj"] = round(a / e, 2) if e else None
                    it["sig"] = it["sig"] and bool(it["index_adj"]) and a >= 10
                if f.startswith("to:"):
                    it["filed"] = filed[f[3:]]
                if caveat:
                    it["caveat"] = caveat
                out.append(it)
            groups.append({"key": g, "label": label, "items": out})
        return {"bank": bc, "product": product, "days": per.days, "period": per.info(),
                "total": B, "filed": int(own[2]), "threat": int(own[3]), "groups": groups}
    return _cached(f"rf:{bc}:{product}:{per.key}", _compute)


def _vuln_by_product(bc: str, product: str | None, per) -> dict:
    """Уязвимые клиенты против ожидания по продуктовому составу.

    expected = Σ по продуктам (жалоб банка на продукт × доля признака у
    остальных банков в этом продукте). «Все уязвимые 9,4% против 5,9%» у Сбера
    могло быть составом (детские карты, пенсионные продукты), а не обращением с
    уязвимыми (аудит 03.10, ОТЗ-10). p — двусторонний тест Пуассона."""
    per = _per(per, per, product)
    vcodes = [f for _g, _l, items in _FLAG_GROUPS if _g == "vuln" for f, _n, _c in items]
    cols = ",\n".join(f"count(*) FILTER (WHERE {_flag_sql(f)})" for f in vcodes)
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT (i.bank = :bank) AS own, coalesce(i.product, '_') AS p, count(*), {cols}
            FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE {_CMP} AND i.bank IS NOT NULL
              AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
              AND i.dt >= :t0 AND i.dt < :t1 AND i.dt <= now()
            GROUP BY 1, 2"""), {"bank": bc, "product": product, **per.binds(),
                                "sv": _ann_schema()}).all()
    own = {r[1]: r for r in rows if r[0]}
    mkt = {r[1]: r for r in rows if not r[0]}
    m_tot = sum(int(r[2]) for r in mkt.values())
    out = {}
    for j, f in enumerate(vcodes):
        m_all = sum(int(r[3 + j]) for r in mkt.values())
        exp, obs = 0.0, 0
        for p_, r in own.items():
            n_p = int(r[2])
            obs += int(r[3 + j])
            mr = mkt.get(p_)
            rate = (int(mr[3 + j]) / int(mr[2])) if mr is not None and int(mr[2]) >= 30 \
                else (m_all / m_tot if m_tot else 0.0)
            exp += n_p * rate
        if exp <= 0:
            continue
        pv = (poisson_sf(obs, exp) if obs >= exp
              else 1.0 - poisson_sf(obs + 1, exp))
        out[f] = {"expected": exp, "observed": obs, "p": min(1.0, 2 * pv)}
    return out


def _int_sp(n: int) -> str:
    return f"{n:,}".replace(",", "\u00a0")


def _pct_int(x) -> int:
    """Целый процент с округлением «половина вверх», как Math.round на фронте:
    round() в Python банковский (22,5 → 22), и шапка расходилась с «Главным»."""
    x = float(x or 0)
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


@_safe(None)
def _vs_market(n: int, prev: int, m_n: int, m_prev: int, k: float = 1.0) -> dict:
    """Рост жалоб банка против роста остальных банков за те же окна.

    Сравниваем долю банка в общем потоке (двухвыборочный z-тест долей): растёт
    она — банк обгоняет рынок. rel_pct — на сколько процентов рост банка больше
    роста рынка: (n/prev) / (m_n/m_prev) − 1. vs_market: above / below / same.
    k — пересчёт прошлого окна на длину текущего (review_period): на доли и на
    rel он не влияет, рост рынка без него врёт, если окна разной длины."""
    if not (n and prev and m_n and m_prev):
        return {}
    rel = (n / prev) / (m_n / m_prev) - 1
    p1, p0 = n / (n + m_n), prev / (prev + m_prev)
    pp = (n + prev) / (n + m_n + prev + m_prev)
    se = math.sqrt(pp * (1 - pp) * (1 / (n + m_n) + 1 / (prev + m_prev))) if 0 < pp < 1 else 0
    sig = bool(se) and _p2((p1 - p0) / se) < 0.05 and abs(rel) >= 0.05
    return {"market_delta_pct": round(100.0 * (m_n / (m_prev * k) - 1), 1),
            "rel_pct": round(100.0 * rel, 1),
            "vs_market": ("above" if rel > 0 else "below") if sig else "same"}


def _dm_range(a: str | None, b: str | None) -> str:
    """«2026-04-19», «2026-07-16» → «19.04–16.07.2026» (год — один раз, если общий)."""
    if not a or not b:
        return ""
    (y0, m0, d0), (y1, m1, d1) = a.split("-"), b.split("-")
    return f"{d0}.{m0}{'' if y0 == y1 else '.' + y0}–{d1}.{m1}.{y1}"


def changes(bank: str, product: str | None = None, days: int = 90,
            period: "_rp.Period | None" = None) -> dict | None:
    """Шапка «что изменилось» для руководителя: только значимые изменения к
    прошлому периоду — объём, темы, опережающие общий поток, доля
    эскалации — и всплеск текущей недели. Всё детерминировано; если значимого
    нет, так и пишем: «спокойно» — тоже вывод. Сравнивать не с чем (период до
    начала данных или сравнение выключено) — no_cmp, и «спокойно» не пишем."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    per = _per(period, days, product)
    head = {"days": per.days, "period": per.info()}
    ov = overview(bc, product, days, period=per) or {}
    if ov.get("delta_partial"):
        return {**head, "items": [], "partial": True}
    items = []
    if not per.has_cmp:
        items += _week_signal_items(bc, product)
        return {**head, "items": items, "partial": False, "no_cmp": True,
                "overall_delta_pct": None}
    # объём — по сравниваемой части периода (дни с полными данными)
    n = int(ov["cmp_n"] if ov.get("cmp_n") is not None else ov.get("total") or 0)
    prev = int(ov.get("prev") or 0)
    pi = per.info()
    when = _dm_range(pi.get("cmp_from"), pi.get("cmp_to"))
    ch = _rate_change(n, prev, per.k)
    if ch and ch["p"] < 0.05 and abs(ov.get("delta_pct") or 0) >= 10:
        up = n > per.expected(prev)
        mk, vs = ov.get("market_delta_pct"), ov.get("vs_market")
        # Цвет — по сравнению с рынком, а не с собой: рост вместе с площадками
        # (новые сборщики, рост banki.ru) — не «стало хуже». dir: up — обгоняет
        # рынок (плохо), down — отстаёт, flat — вместе с рынком.
        dir_ = {"above": "up", "below": "down", "same": "flat"}.get(vs or "", "up" if up else "down")
        mtxt = f" (рынок {'+' if mk >= 0 else '−'}{abs(_pct_int(mk))}%)" if mk is not None else ""
        items.append({"kind": "volume", "dir": dir_, "rise": up, "vs_market": vs,
                      "market_delta_pct": mk, "rel_pct": ov.get("delta_vs_market_pct"),
                      "text": f"Жалоб {'больше' if up else 'меньше'} на {abs(_pct_int(ov['delta_pct']))}%{mtxt}",
                      "detail": f"{_int_sp(n)} против {_int_sp(prev)} за {when}"
                                + (" (в пересчёте на день)" if pi.get("per_day") else "")
                                + f", 95% ДИ {ch['lo']:+d}…{ch['hi']:+d}%"
                                + (f"; у остальных банков {'+' if mk >= 0 else '−'}{abs(_pct_int(mk))}%"
                                   if mk is not None else "")})
    th = themes(bc, product, days, period=per) or {}
    rows = [t for t in th.get("themes") or [] if t.get("delta_sig") and t["key"] != "other"]
    ups = sorted([t for t in rows if (t.get("excess") or 0) > 0], key=lambda t: -t["excess"])[:2]
    downs = sorted([t for t in rows if (t.get("excess") or 0) < 0], key=lambda t: t["excess"])[:1]
    for t in ups + downs:
        up = t["excess"] > 0
        items.append({"kind": "theme", "dir": "up" if up else "down", "key": t["key"],
                      "text": f"{t.get('short') or t['label']}: {t['prev']} → {t['cmp_n']}",
                      "detail": (f"{t['label']} — " + ("растёт быстрее общего потока жалоб"
                                                      if up else "снижается сильнее общего потока жалоб")
                                 + f" ({'+' if (ov.get('delta_pct') or 0) >= 0 else ''}"
                                 f"{_pct_int(ov.get('delta_pct'))}%)")})
    e1, e0 = int(ov.get("esc_n") or 0), int(ov.get("esc_prev_n") or 0)
    n = int(ov.get("total") or 0)          # доля эскалации — по всему периоду
    if n and prev and (e1 + e0) >= 20:
        p1, p0 = e1 / n, e0 / prev
        pp = (e1 + e0) / (n + prev)
        se = math.sqrt(pp * (1 - pp) * (1 / n + 1 / prev)) if 0 < pp < 1 else 0
        if se and _p2((p1 - p0) / se) < 0.05 and abs(p1 - p0) >= 0.01:
            items.append({"kind": "esc", "dir": "up" if p1 > p0 else "down",
                          "text": (f"Эскалация {str(round(100 * p0, 1)).replace('.', ',')}% → "
                                   f"{str(round(100 * p1, 1)).replace('.', ',')}%"),
                          "detail": "доля жалоб с угрозой или обращением в ЦБ, суд, прокуратуру"})
    items += _week_signal_items(bc, product)
    return {**head, "items": items, "partial": False,
            "overall_delta_pct": ov.get("delta_pct")}


def _week_signal_items(bc: str, product: str | None) -> list[dict]:
    """Всплеск текущей недели — от периода не зависит, поэтому и без сравнения."""
    out = []
    wk = weekly_signals(bc, product) or {}
    for sgl in (wk.get("signals") or [])[:1]:
        out.append({"kind": "signal", "dir": "up", "key": sgl["key"],
                    "text": ((f"Держится с {_dm_iso(sgl.get('since'))}: "
                              if sgl.get("status") == "continuing" else "Всплеск недели: ")
                             + f"{sgl.get('short') or sgl['label']} "
                             + ("— новое" if sgl.get("new") else
                                f"×{str(sgl.get('ratio')).replace('.', ',')}")),
                    "detail": (f"{sgl['label']}: {sgl['week']} жалоб за 7 дней при норме "
                               f"~{str(sgl['baseline_week']).replace('.', ',')} в неделю"
                               + (f"; {signal_note(sgl)}" if signal_note(sgl) else ""))})
    return out


@_safe(None)
def daily(bank: str, product: str | None = None) -> dict | None:
    """Жалобы по дням с начала данных — для календаря периода: тон дня по числу
    жалоб, точка у дня со всплеском и живой счётчик «в периоде N жалоб» до
    применения. ~650 чисел массивом от data_from, кэш на полчаса.

    Всплеск дня — больше 1,8 медианы того же дня недели за 8 предыдущих недель
    и на 8 жалоб больше неё: будни и выходные на площадках разные, поэтому
    сравнение с соседними днями красило бы каждый понедельник."""
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        start = data_start_for(product)
        idx, ip = _idx_clause(bc, product)
        with db.session() as s:
            rows = s.execute(text(
                f"SELECT i.dt::date, count(*) FROM review_index i WHERE {idx} AND {_CMP}"
                " AND i.dt >= :s GROUP BY 1 ORDER BY 1"),
                {**ip, "s": _dt.datetime.combine(start, _dt.time())}).all()
        today = _dt.datetime.now(_rp.MSK).date()
        n_days = (today - start).days + 1
        counts = [0] * n_days
        for d, n in rows:
            k = (d - start).days
            if 0 <= k < n_days:
                counts[k] = int(n)
        spikes = []
        for k in range(56, n_days):
            same = sorted(counts[k - 7 * j] for j in range(1, 9))
            med = (same[3] + same[4]) / 2
            if counts[k] >= 1.8 * med and counts[k] - med >= 8:
                spikes.append(k)
        h = _rp.data_horizon()
        return {"bank": bc, "product": product, "from": start.isoformat(),
                "to": today.isoformat(), "counts": counts, "spikes": spikes,
                "full_to": (h - _rp.DAY).date().isoformat() if h else None,
                "data_start": _rp.DATA_START.isoformat()}
    return _cached(f"daily:{bc}:{product}", _compute, ttl=1800)


# Срезы, которые аудиторы ищут в списке продуктов, хотя это не продукты договора
# (обращения 08–14.10): «Зарплатные проекты», «УС — банкоматы и терминалы»,
# «Выездной сервис». Флаги — отдельная LLM-разметка (rag/review_slices.py,
# точность и полнота замерены слепым судьёй), в индексе — review_index.slices;
# условие во всех запросах — SQL-функция al_product_is (migrations/099).
from . import review_slices as _rs  # noqa: E402
from . import review_stats as _RS  # noqa: E402

PSEUDO_PRODUCTS = {label: f"'{key}' = ANY(coalesce(i.slices, '{{}}'))"
                   for key, (label, _definition) in _rs.SLICES.items()}


@_safe(None)
def products(bank: str, days: int = 365, top: int = 10) -> dict | None:
    bc = resolve_bank(bank)
    if not bc:
        return None

    def _compute():
        p = {"bank": bc, "d": days, "top": top}
        win = (f" WHERE i.bank = :bank AND {_CMP}"
               " AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()")
        with db.session() as s:
            rows = s.execute(text(
                "SELECT i.product, count(*) n FROM review_index i" + win +
                " AND i.product IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT :top"), p).all()
            extra = [(lbl, int(s.execute(text("SELECT count(*) FROM review_index i" + win +
                                              " AND " + cond), p).scalar() or 0))
                     for lbl, cond in PSEUDO_PRODUCTS.items()]
        items = [{"product": r[0], "n": int(r[1])} for r in rows]
        # срез — только когда разметка ЭТОГО среза догнала (иначе числа занижены):
        # новый срез не прячет готовые, а пересчёт одного не гасит остальные
        try:
            ready = {k: v["ready"] for k, v in _rs.coverage(days)["keys"].items()}
        except Exception:  # noqa: BLE001
            ready = {}
        key_of = {lbl: k for k, (lbl, _d) in _rs.SLICES.items()}
        items += [{"product": lbl, "n": n} for lbl, n in extra if n and ready.get(key_of.get(lbl))]
        # направления аудита (directions.py): продукты или срезы направления одним
        # фильтром 'dir:<код>'; направление на срезе — только когда его разметка готова
        from .. import directions as _dirs
        dirs = []
        with db.session() as s:
            for d in _dirs.DIRECTIONS:
                if any(not ready.get(k) for k in d.review_slices):
                    continue
                n = int(s.execute(text("SELECT count(*) FROM review_index i" + win
                                       + " AND al_product_is(i.product, i.slices, :dir)"),
                                  {**p, "dir": _dirs.DIR_PREFIX + d.code}).scalar() or 0)
                dirs.append({"product": _dirs.DIR_PREFIX + d.code, "label": d.name, "n": n})
        # что поменялось в срезе за последние 45 дней (переход на новую версию, новый срез) —
        # строка под фильтром: иначе аудитор видит, что жалоб в срезе вдруг стало меньше
        notes: dict[str, str] = {}
        try:
            with db.session() as s:
                for key, note in s.execute(text("""SELECT key, note FROM review_slice_active
                        WHERE note LIKE 'Срез%' AND switched_at > now() - interval '45 days'""")).all():
                    if key in _rs.LABELS:
                        notes[_rs.LABELS[key]] = note
        except Exception:  # noqa: BLE001 — без таблицы реестра заметок нет
            pass
        return {"bank": bc, "items": items, "slice_notes": notes, "directions": dirs}
    return _cached(f"pr:{bc}:{days}:{top}", _compute)


# Как показывать источник аудитору. Ключи приходят из конфига коллекторов;
# незнакомый ключ показываем как есть — это лучше, чем прятать происхождение.
_SOURCE_LABEL = {"bankiru": "banki.ru", "banki_reviews": "banki.ru",
                 "sravni_reviews": "sravni.ru", "bankiros_reviews": "bankiros.ru",
                 "finuslugi_reviews": "finuslugi.ru"}


_ESC_RU = {"none": "", "threat": "грозит", "filed": "обратился"}


def _win_clause(alias: str, days: int | None, period, p: dict) -> str:
    """Окно ленты, выгрузки и групп: период «с — по» или «последние N дней»;
    ни того ни другого — без окна (весь корпус)."""
    if isinstance(period, _rp.Period):
        b = period.binds()
        p["w0"], p["w1"] = b["t0"], b["t1"]
        return f" AND {alias}.dt >= :w0 AND {alias}.dt < :w1"
    if days:
        p["d"] = days
        return f" AND {alias}.dt >= now() - make_interval(days => :d)"
    return ""


@dataclasses.dataclass(frozen=True)
class ReviewQuery:
    """Срез «Аудита отзывов» одним объектом (план 04.11, К1): лента, счётчик ленты,
    выгрузка Excel/CSV, отбор поиска и группы похожих строят условие отсюда, ИИ —
    ссылку на вкладку (to_url). До этого каждый собирал условие сам, и числа на
    экране, в Excel и у ИИ расходились при любой новой мелочи фильтра.

    bank — каноническое имя; month — 'YYYY-MM', 'ev:YYYY-MM' (месяц событий) или
    'sig:YYYY-MM-DD' (неделя всплеска: своё окно, период не применяется)."""
    bank: str | None
    product: str | None = None
    theme: str | None = None
    flag: str | None = None
    esc: bool = False
    city: str | None = None
    source: str | None = None
    month: str | None = None
    period: "_rp.Period | None" = None
    days: int | None = None
    q: str | None = None
    date_from: str | None = None        # «с — по» строками — для ссылки, когда Period нет
    date_to: str | None = None

    def error(self) -> str | None:
        """Код ошибки фильтра (неизвестные тема, признак, площадка) или None."""
        if self.theme and self.theme not in cb.ISSUES:
            return "unknown_theme"
        if _flag_sql(self.flag) is None:
            return "unknown_flag"
        if _source_clause("i", self.source, {}) is None:
            return "unknown_source"
        return None

    def where_sql(self, p: dict, *, pending: bool = True, window: bool = True,
                  city: bool = True) -> str:
        """Условие на строки индекса (алиас i) — то, что идёт после WHERE; параметры — в p.
        pending — без темы и признака в набор входят и свежие неразмеченные (как в ленте);
        window=False — без окна периода (поиск сам ищет в окне и сверяет даты);
        city=False — город уже учтён (поиск ищет с городом)."""
        cond = ["(i.dt IS NULL OR i.dt <= now())"]
        if self.bank:                       # поиск ИИ бывает и по всем банкам
            cond.insert(0, "i.bank = :bank")
            p["bank"] = self.bank
        if self.product:
            cond.append("al_product_is(i.product, i.slices, :product)")
            p["product"] = self.product
        cond.append((_source_clause("i", self.source, p) or "")[len(" AND "):] or "true")
        sig = bool(_SIG_RX.match(self.month or ""))
        if window and not sig:
            w = _win_clause("i", self.days, self.period, p)
            if w:
                cond.append(w[len(" AND "):])
        if self.month and (window or sig or self.month.startswith("ev:")):
            mc = _month_clause("i", self.month, p)
            if mc:
                cond.append(mc[len(" AND "):])
        if city and self.city:
            cond.append("i.city = :city")
            p["city"] = self.city
        if self.esc:
            cond.append("i.esc")
        fcond = _flag_sql(self.flag) or ""
        if fcond:
            # признак есть только у размеченной жалобы — неразмеченные свежие не берём
            cond.append("EXISTS (SELECT 1 FROM review_annotation a WHERE a.url = i.url"
                        f" AND a.schema_version = :sv AND {fcond})")
            p["sv"] = _ann_schema()
        if self.theme:
            cond.append(f"i.issue = :tkey AND {_CMP}")
            p["tkey"] = self.theme
        elif fcond or not pending:
            cond.append(_CMP)
        else:
            cond.append(f"(i.kind IS NULL OR {_CMP})")
        return " AND ".join(c for c in cond if c and c != "true")

    def to_url(self, tab: str | None = None) -> str:
        """Ссылка на вкладку с этим срезом — те же параметры, что читает ReviewsPage."""
        from urllib.parse import urlencode
        per = self.period
        pp, d0, d1 = None, None, None
        sig = bool(_SIG_RX.match(self.month or ""))
        if per is not None and not sig:
            if per.kind == "rolling" and per.n:
                pp = f"{per.n}d"
            elif per.kind == "all":
                pp = "all"
            else:
                info = per.info()
                d0, d1 = info["from"], info["to"]
        elif self.date_from and not sig:
            d0, d1 = self.date_from, self.date_to
        elif self.days and not sig:
            pp = f"{max(1, int(self.days))}d"
        t = tab or ("complaints" if (self.theme or self.flag or self.esc or self.city
                                     or self.source or self.q or self.month) else None)
        params = {"tab": t, "bank": self.bank if self.bank and self.bank != "Сбербанк" else None,
                  "product": self.product, "p": pp, "from": d0, "to": d1 if d0 else None,
                  "theme": self.theme, "flag": self.flag, "esc": "1" if self.esc else None,
                  "city": self.city, "source": self.source, "month": self.month, "q": self.q}
        qs = urlencode({k: v for k, v in params.items() if v})
        return "#reviews" + ("?" + qs if qs else "")


def export_rows(bank: str, product: str | None = None, theme: str | None = None,
                days: int | None = None, city: str | None = None,
                month: str | None = None, esc: bool = False,
                limit: int = 10000, flag: str | None = None,
                source: str | None = None,
                period: "_rp.Period | None" = None,
                q: str | None = None) -> list[dict] | None:
    """Жалобы с разметкой для выгрузки в таблицу — те же фильтры, что у ленты.
    С поиском (q) — ровно тот набор, что во вкладке (search_set): все словесные
    попадания и до SENSE_CAP похожих по смыслу, в том же порядке. До 03.11.2026
    выгрузка поиск игнорировала и отдавала весь срез."""
    bc = resolve_bank(bank)
    if not bc:
        return None
    # тот же срез, что у ленты (ReviewQuery): без темы и признака в него входят и свежие,
    # ещё не размеченные жалобы — до 04.11.2026 выгрузка брала только размеченные
    rq = ReviewQuery(bank=bc, product=product, theme=theme, flag=flag, esc=esc, city=city,
                     source=source, month=month, period=period, days=days)
    if rq.error():
        return []
    p: dict = {"lim": max(1, min(limit, 20000))}
    extra = " AND " + rq.where_sql(p)
    p["sv"] = _ann_schema()
    order = None
    if q and q.strip():
        got = search_set(q, bc, theme=theme, esc=esc, product=product, flag=flag, source=source,
                         city=city, month=month, days=days, period=period)
        if got["meta"].get("failed") or got["meta"].get("words_failed"):
            # пустой или неполный файл выглядел бы как «жалоб нет» (ревью 04.11.2026)
            raise SearchFailed("поиск не отработал")
        order = [r["url"] for r in got["rows"]][:p["lim"]]
        extra += " AND i.url = ANY(:urls)"
        p["urls"] = order or ["-"]
    with db.session() as s:
        rows = [dict(r) for r in s.execute(text(f"""
            SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt, i.city, i.rating,
                   i.issue, i.issues2, a.kind, a.esc, a.esc_to, a.no_consent, a.misled,
                   a.vulnerable, a.amount, a.event_date, a.code_fit, a.new_topic,
                   a.summary, a.quote, i.kind AS ikind
            FROM review_index i
            LEFT JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE true{extra}
            ORDER BY i.dt DESC NULLS LAST
            LIMIT :lim
        """), p).mappings().all()]
    if order is not None:
        pos = {u: k for k, u in enumerate(order)}
        rows.sort(key=lambda r: pos.get(r["url"], len(pos)))
    from . import bankiru_fts
    bodies, sl = {}, {}
    for j in range(0, len(rows), 1000):
        bodies.update(bankiru_fts.bodies_for(rows[j:j + 1000]))
        sl.update(_rs.evidence_for([r["url"] for r in rows[j:j + 1000]]))
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
            "срез и основание": "; ".join(
                x["label"] + (f" · п. {x['clause']}" if (x.get("clause") or "").isdigit() else "")
                + (f" — «{x['quote']}»" if x.get("quote") else "") for x in sl.get(r["url"], [])),
            # как в ленте: неразмеченная — без вида текста в индексе (в т.ч. разметка не удалась)
            "разметка": "" if r["ikind"] else "на разметке",
            "ссылка": r["url"],
            "текст": (bodies.get(r["url"]) or {}).get("text") or "",
        })
    return out


def _feed_from_index(bc: str, product: str | None, theme: str | None,
                     days: int | None, city: str | None, month: str | None,
                     limit: int, offset: int = 0,
                     esc: bool = False, flag: str | None = None,
                     source: str | None = None, sort: str = "date",
                     cursor: int | None = None, period=None) -> dict:
    """Лента по ЕДИНОМУ индексу — все источники в одном списке.

    Показываются жалобы из разметки и ещё не размеченные свежие отзывы (они
    размечаются в течение часа — прятать самые свежие нельзя). Похвала,
    вопросы, мусор и копии в ленту жалоб не идут. Фильтр по теме — по главной
    проблеме, как и счётчик риск-карты: клик по строке показывает ровно те
    жалобы, что в ней посчитаны.

    cursor — номер строки выборки, с которой продолжать (его отдаёт прошлая
    страница в «next»). Раньше страница считалась от склеенного списка, который
    каждый раз выбирался заново с потолком 600 строк: на ~600-й карточке «Показать
    ещё» пропадала при 2 451 жалобе на счётчике (аудит 03.10, ОТЗ-01)."""
    fetch = min(max((limit + offset) * 5, 40), 600)
    p: dict = {"lim": fetch}
    rq = ReviewQuery(bank=bc, product=product, theme=theme, flag=flag, esc=esc, city=city,
                     source=source, month=month, period=period, days=days)
    if rq.error():
        return {"items": [], "mode": "feed", "error": rq.error()}
    # «Сначала серьёзные» — по признакам разметки; при равенстве свежие выше
    sev = sort == "severity"
    if sev:
        p["sv"] = _ann_schema()
    join = ("LEFT JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv"
            if sev else "")
    sev_col = f", {_severity_sql()} AS sev" if sev else ""
    order = "sev DESC NULLS LAST, i.dt DESC NULLS LAST" if sev else "i.dt DESC NULLS LAST"
    where = rq.where_sql(p)
    if cursor is not None:
        return _feed_page(where, join, sev_col, order, p, limit, max(0, int(cursor)), sev)
    try:
        with db.session() as s:
            # Сколько всего по этому фильтру — шапка ленты раньше считала только
            # загруженную страницу («21 жалоба» рядом с «2 426» на вкладке).
            # pending — свежие, ещё не размеченные: в счётчики вкладки они не входят
            tot = None
            if offset == 0:
                n_all, n_pend = s.execute(text(
                    f"SELECT count(*), count(*) FILTER (WHERE i.kind IS NULL)"
                    f" FROM review_index i WHERE {where}"), p).one()
                tot = {"total": int(n_all or 0) - int(n_pend or 0), "pending": int(n_pend or 0)}
            rows = [dict(r) for r in s.execute(text(f"""
                SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt,
                       i.city, i.rating{sev_col}
                FROM review_index i {join}
                WHERE {where}
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
            "has_more": len(out) > offset + limit, **(tot or {})}


def _feed_card(r: dict, b: dict, body: str, sev: bool) -> dict:
    dt = r["dt"]
    return {"bank": r["bank"], "product": r["product"],
            "date": dt.date().isoformat() if dt else None,
            "city": r["city"] or b.get("city"),
            "url": r["url"], "text": body, "similar": 0,
            "rating": float(r["rating"]) if r["rating"] is not None else None,
            "source": r["source"],
            **({"sev": int(r["sev"] or 0)} if sev else {}),
            "themes": []}


def _feed_page(where: str, join: str, sev_col: str, order: str, p: dict,
               limit: int, cursor: int, sev: bool) -> dict:
    """Страница ленты по курсору: выбираем порциями с места, где остановилась прошлая
    страница, пока не наберём limit карточек. Короткие тексты и копии пропускаем,
    копии внутри страницы считаем в «similar». next — откуда продолжать."""
    chunk = max(limit * 3, 60)
    out: list[dict] = []
    seen: dict[str, int] = {}
    pos, more, tot = cursor, True, None
    from . import bankiru_fts
    try:
        with db.session() as s:
            if cursor == 0:
                n_all, n_pend = s.execute(text(
                    f"SELECT count(*), count(*) FILTER (WHERE i.kind IS NULL)"
                    f" FROM review_index i WHERE {where}"), p).one()
                tot = {"total": int(n_all or 0) - int(n_pend or 0), "pending": int(n_pend or 0)}
            for _ in range(8):                       # не больше 8 порций на страницу
                rows = [dict(r) for r in s.execute(text(f"""
                    SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt,
                           i.city, i.rating{sev_col}
                    FROM review_index i {join}
                    WHERE {where}
                    ORDER BY {order}, i.url
                    LIMIT :chunk OFFSET :pos
                """), {**p, "chunk": chunk, "pos": pos}).mappings().all()]
                bodies = bankiru_fts.bodies_for(rows) if rows else {}
                stop = None
                for k, r in enumerate(rows):
                    b = bodies.get(r["url"]) or {}
                    body = (b.get("text") or "").strip()
                    if len(body) < 40:
                        continue
                    key = body[:100].lower()
                    if key in seen:
                        out[seen[key]]["similar"] += 1
                        continue
                    if len(out) == limit:            # страница полна: продолжим с этой строки
                        stop = k
                        break
                    seen[key] = len(out)
                    out.append(_feed_card(r, b, body, sev))
                if stop is not None:
                    pos += stop
                    break
                pos += len(rows)
                if len(rows) < chunk:                # выборка кончилась
                    more = False
                    break
    except Exception as e:
        log.warning("reviews_dash: лента по индексу не собралась (%s)", e)
        return {"items": [], "mode": "feed", "error": "feed_failed"}
    _attach_themes(out)
    return {"items": out, "mode": "feed", "error": None, "has_more": more,
            "next": pos, **(tot or {})}


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
            WHERE f.issue = :key AND f.kind IN ('complaint', 'mixed')
              AND (f.dup_of IS NULL OR NOT {_CANON_CMP_F}) AND f.bank = :bank
              AND (CAST(:product AS text) IS NULL OR al_product_is(f.product, f.slices, :product)){extra}
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
    # срезы с основанием: «Выездной сервис · п. 1 — „курьер не привёз карту“»
    sl = _rs.evidence_for([i["url"] for i in items if i.get("url")])
    for r in items:
        r["slices"] = sl.get(r.get("url") or "", [])
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
            "confidence": ("две модели согласны" if a["status"] == "agree"
                           else "модели разошлись — решила третья"),
            "event_date": str(a["event_date"]) if a.get("event_date") else None,
            "new_topic": a["new_topic"] if a["code_fit"] == "approx" or a["issue"] == "other" else None,
        }


def list_reviews(bank: str, product: str | None = None, theme: str | None = None,
                 q: str | None = None, days: int | None = None,
                 city: str | None = None, month: str | None = None,
                 limit: int = 20, period=None) -> list[dict]:
    """Лента доказательной базы. Тонкая обёртка над list_reviews_ex для тех
    вызывающих, кому нужен только список (сегменты, LLM-объяснения)."""
    # Только именованные: позиционный вызов уже однажды тихо съел период,
    # подставив None пятым аргументом, и любой новый параметр в середине
    # сигнатуры сдвинул бы весь хвост.
    return list_reviews_ex(bank, product=product, theme=theme, q=q, days=days,
                           city=city, month=month, limit=limit, period=period)["items"]



# ── Поиск по жалобам: один набор результатов для ленты, счётчика и выгрузки ────
# До 03.11.2026 выдача была «топ-k похожих»: k росло с каждой страницей
# (min(400, 2·(offset+limit+1))), счётчик «дословных N, по смыслу M» считался по
# первой порции из 42 строк, смысловая часть конца не имела, а выгрузка поиск не
# учитывала вовсе. Аудитор видел «38 + 4» и листал до 140+. Теперь набор
# определён: ВСЕ отзывы, где значимые слова запроса стоят рядом (или вариант
# раскрытия — сокращение, синоним; bankiru_fts.search_near), с теми же фильтрами,
# что во вкладке, плюс не больше SENSE_CAP ближайших по смыслу без этих слов.
# «Любое из слов» как определение не годится: «переводы самому себе» — 6 523
# жалобы Сбера. Порог по косинусу тоже (мусор бывает «ближе» верного), поэтому
# смысловая часть ограничена числом, а не расстоянием.
class SearchFailed(RuntimeError):
    """Поиск не отработал (сбой базы): выгрузка не должна молча отдать пустой список."""


SENSE_CAP = int(os.getenv("REVIEWS_SEARCH_SENSE_CAP", "20"))
_WORD_RX = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{2,}")
_SET_TTL = 600
_SET_CACHE: dict[tuple, tuple[float, dict]] = {}


_PHRASE_RX = re.compile(r'"([^"]{2,})"')
_EXCL_RX = re.compile(r'(?:^|\s)[-−–]([0-9A-Za-zА-Яа-яЁё]{2,})')
_SENSE_POOL = int(os.getenv("REVIEWS_SEARCH_SENSE_POOL", "40"))
_THIN_WORDS = int(os.getenv("REVIEWS_SEARCH_THIN", "5"))
_SENSE_OK: dict[tuple[str, str], bool] = {}     # (запрос, url) → относится ли (проверка моделью)


def parse_query(q: str) -> dict:
    """Синтаксис строки поиска: "точная фраза" в прямых кавычках, -слово — исключить.
    Ёлочки «…» — просто текст: их копируют из подсказок, точную фразу они не значат."""
    phrases = [p.strip() for p in _PHRASE_RX.findall(q or "") if p.strip()]
    excl = [w.lower() for w in _EXCL_RX.findall(q or "")]
    plain = _EXCL_RX.sub(" ", _PHRASE_RX.sub(" ", q or ""))
    plain = " ".join(plain.replace("«", " ").replace("»", " ").split())
    text_ = " ".join([plain] + phrases).strip() or (q or "").strip()
    return {"phrases": phrases, "excl": excl, "plain": plain, "text": text_}


def _excluded(urls: list[str], words: list[str]) -> set[str]:
    """Какие из urls содержат слово-исключение (по началу основы, как и поиск)."""
    if not urls or not words:
        return set()
    from . import bankiru_fts
    with db.session() as s:
        lex = s.execute(text("SELECT tsvector_to_array(to_tsvector(CAST('russian' AS regconfig), :t))"),
                        {"t": " ".join(words)}).scalar() or []
        if not lex:
            return set()
        tq = " | ".join("'" + bankiru_fts.stem_key(x).replace("'", "''") + "'"
                        + (":*" if bankiru_fts.is_prefix(x) else "") for x in lex)
        return set(s.execute(text("""SELECT url FROM review_index WHERE url = ANY(:u)
                                      AND tsv @@ to_tsquery(CAST('simple' AS regconfig), :tq)"""),
                             {"u": urls, "tq": tq}).scalars().all())


# Проверка «похожих по смыслу» (04.11.2026). Раньше — одна быстрая модель на все 40
# кандидатов с таймаутом 20 с и повтором: DeepSeek-V4-Flash сначала рассуждает и в 20 с
# не укладывался, поиск ждал 40 с и показывал кандидатов без проверки (замер на проде:
# 40,5 с на любом запросе). Теперь в два шага:
#   1) реранкер Qwen3 (облако, /rerank, ~0,3 с на 40 жалоб) упорядочивает кандидатов и
#      отсекает явно чужие — порядок «по смыслу» теперь от самого близкого;
#   2) лёгкая модель (gemini-3.1-flash-lite: 1,4 с на 10 жалоб) отвечает «та ли это
#      ситуация» — пакетами по 10 параллельно, с жёстким таймаутом без повторов.
# Реранкер один различает тему, а не ситуацию (на «не зачисляют выручку по эквайрингу»
# поднимает и сломанные терминалы), поэтому решает модель; не ответила — решает порог
# реранкера; не ответили оба — кандидаты как есть, с пометкой «не проверено».
_RERANK_MODEL = os.getenv("REVIEWS_RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B")
_SENSE_MODEL = os.getenv("REVIEWS_SENSE_MODEL", "google/gemini-3.1-flash-lite")
_SENSE_RR_MIN = float(os.getenv("REVIEWS_SENSE_RR_MIN", "0.05"))     # ниже — точно чужая
_SENSE_RR_KEEP = float(os.getenv("REVIEWS_SENSE_RR_KEEP", "0.5"))    # порог, если модель молчит
_SENSE_BATCH = 10
_RR_PRE = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and '
           'the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
_RR_INS = "Given an auditor's search query, judge whether the bank customer complaint describes the same situation"
_RR_SUF = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def _rerank(query: str, texts: list[str]) -> list[float] | None:
    """Оценки реранкера 0..1 по порядку texts (шаблон Qwen3-Reranker: без него оценки
    плоские). None — реранкер недоступен."""
    from ..ai.analyst import LLM_API_KEY, LLM_BASE_URL
    if not LLM_API_KEY or not LLM_BASE_URL or not texts:
        return None
    try:
        import httpx
        r = httpx.post(LLM_BASE_URL.rstrip("/") + "/rerank", timeout=float(os.getenv("REVIEWS_RERANK_TIMEOUT_S", "6")),
                       headers={"Authorization": "Bearer " + LLM_API_KEY},
                       json={"model": _RERANK_MODEL,
                             "query": _RR_PRE + f"<Instruct>: {_RR_INS}\n<Query>: {query}\n<Document>: ",
                             "documents": [t[:1500] + _RR_SUF for t in texts]})
        r.raise_for_status()
        sc = {int(x["index"]): float(x["relevance_score"]) for x in r.json().get("results") or []}
        if len(sc) < len(texts):
            return None
        return [sc[k] for k in range(len(texts))]
    except Exception as e:  # noqa: BLE001
        log.info("reviews_dash: реранкер недоступен (%s)", type(e).__name__)
        return None


def _pfx(w: str) -> str:
    return w.lower().replace("ё", "е")[:5]


def _variant_ok(variant: str, kept: list[str]) -> bool:
    """Вариант запроса — отдельная группа «слова рядом», только если он сохраняет смысл.
    Не годятся: одно слово при запросе из нескольких (вариант «эквайринг» к «не
    зачисляют выручку по эквайрингу» находил любую жалобу на эквайринг — 218 вместо
    нужных, 04.11.2026) и вариант из тех же слов запроса, только меньшим их числом."""
    words = [w for w in variant.split() if w.strip()]
    if not words:
        return False
    if len(kept) >= 2 and len(words) < 2:
        return False
    base = {_pfx(w) for w in kept}
    return not ({_pfx(w) for w in words} <= base and len(words) < len(kept))


_RERANK_TOP = int(os.getenv("REVIEWS_RERANK_TOP", "50"))


def _rerank_head(query: str, rows: list[dict]) -> tuple[list[dict], bool]:
    """Первые _RERANK_TOP жалоб со словами запроса — по близости к ситуации, которую ищет
    аудитор (реранкер, ~0,3 с), остальные — в прежнем порядке. Первую страницу читают
    все, и раньше её порядок задавало слияние векторов и ключевых слов. Реранкер не
    ответил — порядок прежний."""
    head = rows[:_RERANK_TOP]
    if len(head) < 3:
        return rows, False
    try:
        with db.session() as s:
            summ = dict(s.execute(text("""SELECT url, summary FROM review_annotation
                                           WHERE schema_version = :sv AND url = ANY(:u)"""),
                                  {"sv": _ann_schema(), "u": [r["url"] for r in head]}).all())
    except Exception:  # noqa: BLE001
        return rows, False
    sc = _rerank(query, [(summ.get(r["url"]) or (r.get("text") or "")[:500] or "").strip() for r in head])
    if sc is None:
        return rows, False
    order = sorted(range(len(head)), key=lambda k: (-sc[k], k))
    return [head[k] for k in order] + rows[_RERANK_TOP:], True


def _verify_sense(query: str, rows: list[dict]) -> tuple[list[dict], bool]:
    """Смысловые кандидаты → только относящиеся к запросу, от самых близких (сначала
    реранкер, затем лёгкая модель по сути жалобы из разметки). Без проверки «похожие
    по смыслу» на «переводы самому себе» давали «перевод из ВТБ не дошёл»."""
    if not rows:
        return [], True
    with db.session() as s:
        summ = dict(s.execute(text("""SELECT url, summary FROM review_annotation
                                       WHERE schema_version = :sv AND url = ANY(:u)"""),
                              {"sv": _ann_schema(), "u": [r["url"] for r in rows]}).all())
    txt = {r["url"]: (summ.get(r["url"]) or (r.get("text") or "")[:500]).strip() for r in rows}
    sc = _rerank(query, [txt[r["url"]] for r in rows])
    if sc is not None:
        order = sorted(range(len(rows)), key=lambda k: -sc[k])
        rows = [dict(rows[k], sense_score=round(sc[k], 3)) for k in order if sc[k] >= _SENSE_RR_MIN]
    if len(_SENSE_OK) > 20000:
        _SENSE_OK.clear()          # до записи: очистка между записью и чтением теряла вердикты
    # вердикты из кэша — сразу в свой словарь: общий кэш может очистить параллельный запрос
    verdicts: dict[str, bool] = {r["url"]: v for r in rows
                                 if (v := _SENSE_OK.get((query, r["url"]))) is not None}
    todo = [r for r in rows if r["url"] not in verdicts]
    ok_model = True
    if todo:
        parts = [todo[k:k + _SENSE_BATCH] for k in range(0, len(todo), _SENSE_BATCH)]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(parts)) as ex:
            got = list(ex.map(lambda part: _ask_relevance(query, [txt[r["url"]] for r in part]), parts))
        for part, verdict in zip(parts, got):
            if verdict is None:
                ok_model = False
                continue
            for r, v in zip(part, verdict):
                _SENSE_OK[(query, r["url"])] = v
                verdicts[r["url"]] = v
    if ok_model:
        # свои вердикты — из локального словаря: общий кэш мог очистить параллельный запрос
        return [r for r in rows if verdicts.get(r["url"])], True
    if sc is not None:                     # модель не ответила — решает порог реранкера
        return [r for r in rows if r.get("sense_score", 0) >= _SENSE_RR_KEEP], True
    return rows, False


def _ask_relevance(query: str, texts: list[str]) -> list[bool] | None:
    from ..ai.analyst import LLM_API_KEY, LLM_BASE_URL
    from ..ai.llm_utils import _loose_json_loads
    if not LLM_API_KEY or not texts:
        return None
    try:
        from openai import OpenAI
        c = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, max_retries=0,
                   timeout=float(os.getenv("REVIEWS_SENSE_TIMEOUT_S", "12")))
        user = (f"Запрос аудитора: «{query}»\n\n"
                + "\n".join(f"[{k}] {t[:500]}" for k, t in enumerate(texts, 1)))
        r = c.chat.completions.create(
            model=_SENSE_MODEL, temperature=0, max_tokens=800,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": (
                "Ты отбираешь жалобы клиентов банка к запросу аудитора. Жалоба относится к запросу, "
                "если её суть — та ситуация, которую ищет аудитор, пусть и другими словами. Не относится, "
                "если совпадает лишь продукт или общие слова, а ситуация другая. Сомневаешься — не "
                'относится. Ответ строго JSON: {"items":[{"n":1,"rel":true}, ...]} по всем n.')},
                {"role": "user", "content": user}])
        j = _loose_json_loads(r.choices[0].message.content or "") or {}
        got = {int(x["n"]): bool(x.get("rel")) for x in (j.get("items") or [])
               if isinstance(x, dict) and str(x.get("n", "")).isdigit()}
        if len(got) < len(texts) * 0.9:
            return None
        return [got.get(k, False) for k in range(1, len(texts) + 1)]
    except Exception as e:  # noqa: BLE001
        log.info("reviews_dash: проверка смысловых совпадений недоступна (%s)", type(e).__name__)
        return None


def search_set(q: str, bc: str | None, *, theme: str | None = None, esc: bool = False,
               product: str | None = None, flag: str | None = None, source: str | None = None,
               city: str | None = None, month: str | None = None, days: int | None = None,
               period=None, sort: str = "auto") -> dict:
    """{rows, meta}: упорядоченный конечный набор — сначала все жалобы со словами
    запроса, затем не больше SENSE_CAP проверенных моделью похожих по смыслу. У строк
    из хвоста текста ещё нет — его подставляет _fill_page для показанной страницы."""
    import time as _time
    pkey = period.key if isinstance(period, _rp.Period) else None
    key = (q.strip().lower(), bc, theme, esc, product, flag, source, city, month, days, pkey, sort)
    hit = _SET_CACHE.get(key)
    if hit and _time.time() - hit[0] < _SET_TTL:
        return hit[1]
    pq = parse_query(q)
    meta: dict = {}
    since = days
    lo_d = hi_d = None
    if isinstance(period, _rp.Period):
        since = max(1, math.ceil((_dt.datetime.now(_rp.MSK) - period.t0) / _rp.DAY))
        lo_d = period.t0.date().isoformat()
        hi_d = (period.t1 - _rp.DAY).date().isoformat()
    # месяц событий и неделя всплеска — фильтр по индексу (_keep_urls), не окно поиска
    ev_month = month if (month or "").startswith(("ev:", "sig:")) else None
    plain_month = None if ev_month else month
    sg = _SIG_RX.match(month or "")
    if sg:
        # неделя всплеска — и окно поиска: иначе слова искались по всей истории, верхние
        # NEAR_CAP по рангу отрезали жалобы самой недели ещё до фильтра (ревью 04.11.2026)
        d0 = _dt.date.fromisoformat(sg.group(1)) - _dt.timedelta(days=6)
        since = max(1, (_dt.date.today() - d0).days + 1)
    # верхушка слияния слов и смысла (RRF) — порядок и смысловые кандидаты
    head = search_reviews(pq["text"], bank=bc, product=None, since_days=since, theme_rx=None,
                          city=city, month=plain_month, k=60, strict=True, _meta=meta)
    from . import bankiru_fts
    from . import reviews_query as _rq
    since_ts = (_dt.datetime.now() - _dt.timedelta(days=since)) if since else None
    wmeta: dict = {}
    # «рядом» в базе (миграция 106, с 04.11.2026): фильтры вкладки — до отбора, без
    # потолка NEAR_CAP. Прежде частый запрос («кредит» по банку) обрезался на 6000
    # строк по рангу ещё до темы и продукта — нужные жалобы могли не дойти до фильтра.
    # Без функции в базе — прежний путь через Python.
    fq = ReviewQuery(bank=None, product=product, theme=theme, flag=flag, esc=esc,
                     source=source, month=ev_month)
    use_sql = bankiru_fts.near_sql_ready() and not fq.error()

    def near(groups, spans=None):
        if not use_sql:
            return bankiru_fts.search_near(groups, bank=bc, city=city, month=plain_month,
                                           since_ts=since_ts, spans=spans, _meta=wmeta)
        fp: dict = {}
        cond = fq.where_sql(fp, window=False, city=False)
        try:
            return bankiru_fts.search_near_sql(
                groups, bank=bc, city=city, month=plain_month, since_ts=since_ts, spans=spans,
                extra_sql=f"EXISTS (SELECT 1 FROM review_index i WHERE i.url = f.url AND {cond})",
                params=fp), False
        except Exception:  # noqa: BLE001 — «слов нет» и «поиск не отработал» различаем
            wmeta["failed"] = True
            return [], False
    words: list[dict] = []
    capped = False
    groups: list[list[str]] = []
    if not bankiru_fts.is_ready():
        wmeta["failed"] = True
    else:
        if pq["phrases"]:
            # точные фразы: жалоба должна содержать каждую (как в поисковиках)
            words, capped = bankiru_fts.search_phrases(pq["phrases"], bank=bc, city=city,
                                                       month=plain_month, since_ts=since_ts,
                                                       also=" ".join(_rq.base_terms(pq["plain"]))
                                                       if pq["plain"].strip() else None, _meta=wmeta)
            groups = [p.split() for p in pq["phrases"]]
        else:
            base = _rq.base_terms(pq["plain"])
            common = {c.lower() for c in (meta.get("common") or [])}
            kept = [t for t in base if t.lower() not in common] or base
            low = {t.lower() for t in base}
            groups = [kept] + [v.split() for v in (meta.get("terms") or [])
                               if v.lower() not in low and _variant_ok(v, kept)]
            # окно первой группы — по всем словам запроса: служебные («не», «по») в
            # тексте тоже занимают позиции
            words, capped = near(groups, spans=[len(_WORD_RX.findall(pq["plain"]))])
            if len(words) < _THIN_WORDS:
                # слова аудитора не легли на язык жалоб — варианты от модели
                # («не зачисляют выручку» ↔ «не поступают деньги по эквайрингу»)
                x = _rq.expand(pq["plain"], use_llm=True)
                extra = [v.split() for v in (x.get("terms") or [])
                         if v.lower() not in low and v.split() not in groups and _variant_ok(v, kept)]
                if extra:
                    more, c2 = near(extra)
                    have = {w["url"] for w in words}
                    words += [w for w in more if w["url"] not in have]
                    groups += extra
                    capped = capped or c2
    wset = {w["url"]: w for w in words}
    urls = list(dict.fromkeys([r.get("url") for r in head if r.get("url")] + list(wset)))
    keep = _keep_urls(urls, theme=theme, esc=esc, product=product, flag=flag, source=source,
                      month=ev_month, bank=bc)
    if keep is None:
        # фильтры вкладки не применились — показать всё найденное значило бы показать
        # не тот набор (до 04.11.2026 так и было); честнее сказать, что поиск не отработал
        meta.update({"n_words": 0, "n_sense": 0, "total": 0, "failed": "filters",
                     "phrases": pq["phrases"], "excluded": pq["excl"]})
        return {"rows": [], "meta": meta}
    drop = _excluded(urls, pq["excl"])

    def ok(r) -> bool:
        d = str(r.get("date") or "")[:10]
        u = r.get("url")
        return u in keep and u not in drop and (not lo_d or not d or lo_d <= d <= hi_d)
    head_pos = {r.get("url"): k for k, r in enumerate(head)}
    head_by = {r.get("url"): r for r in head}
    # 1) со словами запроса — сначала то, что выше в слиянии, затем по рангу слов
    word_rows = []
    for u, w in wset.items():
        if not ok(w):
            continue
        h = head_by.get(u)
        if h:
            row = dict(h)
            row["via"] = "слова и смысл" if h.get("via") in ("смысл", "слова и смысл") else "слова"
        else:
            row = {"bank": w.get("bank"), "product": w.get("product"), "date": w.get("date"),
                   "city": w.get("city"), "url": u, "review_id": w.get("review_id"),
                   "source": w.get("source") or "bankiru", "similar": 0, "text": None,
                   "via": "слова", "terms": w.get("terms") or []}
        row["_k"] = (head_pos.get(u, 10**6), -float(w.get("rank") or 0))
        word_rows.append(row)
    if sort == "date":
        word_rows.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    else:
        word_rows.sort(key=lambda r: r["_k"])
        word_rows, meta["words_reranked"] = _rerank_head(pq["text"], word_rows)
    # 2) похожие по смыслу: ближайшие без слов запроса, проверенные моделью
    cands = []
    for r in head:
        u = r.get("url")
        if not u or u in wset or not ok(r):
            continue
        cands.append({**r, "via": "смысл"})
        if len(cands) >= _SENSE_POOL:
            break
    sense, checked = _verify_sense(pq["text"], cands) if cands else ([], True)
    sense = sense[:SENSE_CAP]
    rows = word_rows + sense
    for r in rows:
        r.pop("_k", None)
    meta.update({"n_words": len(word_rows), "n_sense": len(sense), "total": len(rows),
                 "words_capped": capped, "sense_cap": SENSE_CAP, "sense_checked": checked,
                 "words_failed": bool(wmeta.get("failed")),
                 "stop_phrases": wmeta.get("stop_phrases") or [],
                 "groups": [" ".join(g) for g in groups], "phrases": pq["phrases"],
                 "excluded": pq["excl"]})
    res = {"rows": rows, "meta": meta}
    if meta["words_failed"] or not checked:
        return res                         # неполный ответ не кэшируем: следующий запрос — заново
    if len(_SET_CACHE) > 200:
        _SET_CACHE.clear()
    _SET_CACHE[key] = (_time.time(), res)
    return res


def _fill_page(rows: list[dict], tsquery: str | None) -> list[dict]:
    """Тексты и подсветка — только для показанной страницы."""
    from . import bankiru_fts
    need = [r for r in rows if not r.get("text")]
    if need:
        bodies = bankiru_fts.bodies_for(need)
        for r in need:
            b = bodies.get(r["url"]) or {}
            r["text"] = (b.get("text") or "").strip()
            r["product"] = r.get("product") or b.get("product")
            r["city"] = r.get("city") or b.get("city")
    page = [dict(r) for r in rows if len((r.get("text") or "").strip()) >= 40]
    if tsquery and page and any(not r.get("marked") for r in page):
        try:
            for r, m in zip(page, bankiru_fts.headline([r["text"] for r in page], tsquery)):
                r.setdefault("marked", m)
        except Exception:  # noqa: BLE001 — без подсветки выдача остаётся выдачей
            pass
    return page


def list_reviews_ex(bank: str, product: str | None = None, theme: str | None = None,
                    q: str | None = None, days: int | None = None,
                    city: str | None = None, month: str | None = None,
                    limit: int = 20, offset: int = 0,
                    esc: bool = False, sort: str = "auto", flag: str | None = None,
                    source: str | None = None, cursor: int | None = None,
                    period=None) -> dict:
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
        try:
            got = search_set(q, bc, theme=theme, esc=esc, product=product, flag=flag,
                             source=source, city=city, month=month, days=days, period=period,
                             sort=sort)
        except Exception as e:
            log.warning("reviews_dash: поиск по %r упал: %s", q, e)
            return {"items": [], "mode": "search", "error": "search_failed"}
        page = _fill_page(got["rows"][offset:offset + limit], got["meta"].get("tsquery"))
        _attach_themes(page)
        return {"items": page, "mode": "search", "error": None, "search": got["meta"],
                "total": got["meta"]["total"], "has_more": len(got["rows"]) > offset + limit}
    if not bc:
        return {"items": [], "mode": "feed", "error": "unknown_bank"}
    return _feed_from_index(bc, product, theme, days, city, month, limit, offset, esc, flag,
                            source=source, sort="severity" if sort == "severity" else "date",
                            cursor=cursor, period=period)


@_safe(None)
def segment_reviews(bank: str, product: str | None = None, city: str | None = None,
                    month: str | None = None, limit: int = 40, period=None) -> dict | None:
    """Сводка по срезу (город или месяц) для LLM-объяснения аномалии/пика:
    распределение главных проблем по разметке + изложения и примеры со ссылками.
    Город — за период вкладки (у месяца окно задаёт сам месяц)."""
    revs = list_reviews(bank, product=product, city=city, month=month, limit=limit,
                        period=period if city and not month else None)
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
    «Почта Банк», и вкладка молча показывала «1 жалоба за квартал».

    Выключенная в config/sources.yaml площадка — «выключен», а не «встал»
    (bankiros выключили намеренно 30.09, а «Пульс» горел просадкой). Неделя
    больше трёх норм — «всплеск»: так выглядит и наплыв жалоб, и догрузка
    после починки сборщика (sravni 30.09: ×9 к норме под видом «нормы»)."""
    out: dict = {"sources": [], "gone_banks": [], "week_end": week_end()}
    try:
        from ..config import load_sources
        cfg = load_sources()
    except Exception:  # noqa: BLE001 — без конфига просто не знаем, кто выключен
        cfg = {}
    # banki_reviews и sravni_reviews в конфиге тоже enabled: false — их собирает
    # review_streams дважды в сутки, это не выключение
    streams = {"banki_reviews", "sravni_reviews"}
    disabled = {k for k, v in cfg.items()
                if not (v or {}).get("enabled", True) and k not in streams}
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
        # самый свежий отзыв площадки: у корпуса banki.ru своих прогонов нет
        last_item = {r[0]: r[1] for r in s.execute(text("""
            SELECT 'bankiru', max(dt)::date FROM review_index
             WHERE source = 'bankiru' AND dt <= now()
            UNION ALL
            SELECT source, max(posted_at)::date FROM review
             WHERE source IN ('banki_reviews', 'sravni_reviews', 'finuslugi_reviews', 'bankiros_reviews')
               AND posted_at <= now()
             GROUP BY 1""")).all()}
        # Пропавший банк: поток жалоб за полгода до последнего отзыва против дней
        # тишины. Раньше ждали 45 дней тишины при ≥20 жалобах в месяц — Почта
        # Банк выплыл бы через полтора месяца (ПЛТ-14). «Сегодня» — свежесть
        # всего корпуса: встал краулер — молчат все, и это не пропажа банка.
        gone = s.execute(text(f"""
            WITH l AS (SELECT i.bank, max(i.dt) AS last FROM review_index i
                        WHERE i.source = 'bankiru' AND {_CMP}
                          AND i.dt > now() - interval '225 days' AND i.dt <= now()
                        GROUP BY 1)
            SELECT l.bank, count(*) / 180.0 AS per_day, l.last::date AS last
              FROM l JOIN review_index i ON i.bank = l.bank AND i.source = 'bankiru'
                    AND {_CMP} AND i.dt BETWEEN l.last - interval '180 days' AND l.last
             GROUP BY 1, 3""")).all()
        # новичок — от пяти жалоб: банк с одной жалобой иначе получал «1 в день»
        firsts = s.execute(text(f"""
            SELECT i.bank, min(i.dt)::date AS first, count(*) AS n
              FROM review_index i
             WHERE i.source = 'bankiru' AND {_CMP} AND i.dt <= now()
             GROUP BY 1 HAVING min(i.dt) > now() - interval '260 days'
                          AND count(*) >= 5""")).all()
        # «сегодня» — по размеченным жалобам, как и тишина банка: встала
        # разметка — молчат все банки, и это не пропажа
        cmp_last = s.execute(text(f"""
            SELECT max(i.dt)::date FROM review_index i
             WHERE i.source = 'bankiru' AND {_CMP} AND i.dt <= now()""")).scalar()
    import statistics
    by: dict[str, list[int]] = {}
    for src, w, n in rows:
        if 0 <= int(w) <= 8:
            by.setdefault(src, [0] * 9)[int(w)] += int(n)
    for src in sorted(set(by) | set(_SOURCE_RU), key=lambda k: -(by.get(k, [0])[0])):
        arr = by.get(src, [0] * 9)
        norm = statistics.median(arr[1:9])
        wk = arr[0]
        if src in disabled:
            status = "выключен"
        elif norm < 5:
            status = "малый поток"
        elif wk == 0:
            status = "встал"
        elif wk < 0.6 * norm:
            status = "просел"
        elif wk > 3 * norm:
            status = "всплеск"
        else:
            status = "норма"
        run = runs.get(src)
        li = last_item.get(src)
        out["sources"].append({"source": src, "label": _SOURCE_RU.get(src, src), "week": wk,
                               "norm": round(norm), "status": status,
                               "external": src == "bankiru",
                               "last_item": str(li) if li else None,
                               "last_run": run[1].isoformat() if run else None,
                               "last_run_status": run[2] if run else None,
                               "last_error": (run[3] or None) if run else None})
    corpus_last = min(x for x in (last_item.get("bankiru"), cmp_last) if x) \
        if (last_item.get("bankiru") or cmp_last) else None
    out["gone_banks"] = classify_gone(
        [(b, float(pd), last) for b, pd, last in gone],
        # поток новичка — от первой жалобы до свежести корпуса
        {b: (f, float(n) / max((corpus_last - f).days, 1)) for b, f, n in firsts},
        corpus_last) if corpus_last else []
    return out


# Банки, ушедшие с площадки по известной причине: не тревога, а пояснение.
# Почта Банк — слияние: площадка ведёт его отзывы под ВТБ (bankiru_fts, коммент
# к BANK_RENAMES), склеивать его с ВТБ как переименование нельзя.
KNOWN_BANK_EXITS = {
    "Почта Банк": {"into": "ВТБ",
                   "note": "площадка ведёт отзывы Почта Банка под ВТБ (слияние, май 2026)"},
}


def classify_gone(stats, firsts, today, *, min_silent: int = 7, min_expected: float = 10.0):
    """Пропавшие из корпуса банки и кандидаты в переименование.

    stats: [(bank, жалоб в день за полгода до последнего отзыва, последний отзыв)];
    firsts: {bank: (первый отзыв, жалоб в день)} — банки, появившиеся недавно;
    today: свежесть корпуса. Пропал, если тишина дольше min_silent дней и при его
    потоке за это время ждали бы ≥ min_expected жалоб (вероятность нуля при живом
    потоке < 5·10⁻⁵). Кандидат в новое имя — банк, впервые появившийся в пределах
    30 дней от последнего отзыва пропавшего, с потоком не меньше 30 % от его."""
    out = []
    for bank, per_day, last in stats:
        silent = (today - last).days
        lam = per_day * silent
        if silent <= min_silent or lam < min_expected:
            continue
        cands = [b for b, (f, pd) in firsts.items()
                 if b != bank and abs((f - last).days) <= 30 and pd >= 0.3 * per_day]
        known = KNOWN_BANK_EXITS.get(bank)
        out.append({"bank": bank, "per_month": round(per_day * 30), "last": str(last),
                    "silent_days": silent, "expected": round(lam, 1),
                    "rename_candidates": sorted(cands),
                    "known": (known or {}).get("note")})
    out.sort(key=lambda g: (g["known"] is not None, -g["expected"]))
    return out


@_safe(None)
def segment_profile(bank: str, product: str | None = None, city: str | None = None,
                    month: str | None = None, days: int = 90,
                    period: "_rp.Period | None" = None) -> dict | None:
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
    per = _per(period, days, product)
    p: dict = {"bank": bc, "product": product, "city": city, **per.binds()}
    ev = bool(month and month.startswith("ev:"))
    if city:
        seg = "i.city = :city AND i.dt >= :t0 AND i.dt < :t1 AND i.dt <= now()"
        base = "i.dt >= :t0 AND i.dt < :t1 AND i.dt <= now()"
        pi = per.info()
        base_label = f"вся страна за тот же период ({_dm_range(pi['from'], pi['to'])})"
    elif _SIG_RX.match(month or ""):
        seg = _sig_window("i", _SIG_RX.match(month).group(1), p)
        base = ("i.dt >= CAST(:sig_d AS date) - 55 AND i.dt < CAST(:sig_d AS date) - 6"
                " AND (i.ev_date IS NULL OR i.ev_date >= i.dt::date - 60)")
        base_label = "7 предыдущих недель"
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
              AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
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
        g = geo(bank, product, days=days, top=40, period=per) or {}
        flagged = any(c["city"] == city and c.get("anomaly") for c in g.get("cities") or [])
    else:
        t = trend(bank, product, basis="event" if ev else "pub") or {}
        flagged = any(x.get("ym") == p.get("month") and x.get("spike") for x in t.get("series") or [])
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


# Сравнение с рынком — только по сопоставимому корпусу: внешний корпус banki.ru ровно
# покрывает все банки, а наши сборщики (sravni, finuslugi, своя лента banki.ru, bankiros)
# собирают в основном Сбер — новый источник давал «рост только у Сбера» (план 04.11, К5).
# Рост к собственной норме считается по всем источникам, «банк против рынка» — по этому.
CMP_SOURCE = "bankiru"


def _topic_week_counts(bank_canon: str | None, product: str | None,
                       exclude_bank: str | None = None, source: str | None = None):
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
    p = {"bank": bank_canon, "ex": exclude_bank, "product": product, "src": source}
    try:
        with db.session() as s:
            rows = s.execute(text(f"""
                WITH dd AS (
                    SELECT i.issue, floor(extract(epoch FROM {_WEEK_END} - i.dt) / 604800)::int AS w
                    FROM review_index i
                    WHERE (CAST(:bank AS text) IS NULL OR i.bank = :bank)
                      AND (CAST(:ex AS text) IS NULL OR i.bank <> :ex)
                      AND (CAST(:src AS text) IS NULL OR coalesce(i.source, 'bankiru') = :src)
                      AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
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
              AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
              AND i.dt >= now() - make_interval(days => :days * 2) AND i.dt <= now()
            GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """), {"bank": bc, "product": product, "days": days}).first()
    if not row or not int(row[1]):
        return None
    o = cb.issue_obj(row[0]) or {}
    n, prev = int(row[1]), int(row[2])
    return {"key": row[0], "label": o.get("label"), "risk": o.get("risk"), "n": n,
            "delta_pct": (round(100.0 * (n - prev) / prev) if prev and _prev_ready(bc, days) else None)}



def _ru_num(x: float) -> str:
    """1 знак после запятой по-русски, без «,0»: 2,1 · 4 · 1,9."""
    t = f"{round(float(x), 1):.1f}".replace(".", ",")
    return t[:-2] if t.endswith(",0") else t


def _raz(k: float) -> str:
    """«в 2,1 раза», «в 3 раза», «в 5 раз» — согласование с числом."""
    r = round(float(k), 1)
    if r != int(r):
        return "раза"
    n = int(r)
    return "раза" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "раз"


_FLAT = 1.15   # рынок по теме «ровный»: рост к своей норме меньше ×1,15


def poisson_sf(k: int, lam: float) -> float:
    """P(X ≥ k) для пуассоновского X со средним lam — вероятность увидеть
    столько жалоб или больше, если тема живёт в своей норме."""
    if k <= 0:
        return 1.0
    if not lam or lam <= 0:
        return 0.0
    cdf = sum(math.exp(i * math.log(lam) - lam - math.lgamma(i + 1)) for i in range(int(k)))
    return max(0.0, min(1.0, 1.0 - cdf))


# Порог «рост подтверждён» для расхождений «пульса»: поправка Бонферрони на
# число проверенных проблем. Без неё на 40 проблемах одна-две «вне нормы»
# выпадают случайно каждую неделю — и плитка «Проверить сегодня» горела
# красным по теме, которую тест сигналов отбросил (аудит 03.10: 9 жалоб при
# норме 4,0, P ≈ 0,02 на 39 проблем).
PULSE_ALPHA = 0.05


def market_flat(market_ratio) -> bool:
    return market_ratio is None or float(market_ratio) < _FLAT


def _is_flat(s: dict) -> bool:
    """Рынок по теме ровный: по выводу теста доли (market_flat в сигнале, с 04.11.2026 —
    при нехватке данных НЕ ровный), а в старых снимках — по отношению ростов."""
    return bool(s["market_flat"]) if "market_flat" in s else market_flat(s.get("market_ratio"))


def market_phrase(ratio, market_ratio, who: str = "банка") -> str | None:
    """Как честно сказать о рынке. «Только у …» — лишь когда рынок по теме
    ровный: 25.09 заголовок написал «всплеск только у Сбера» при росте рынка
    ×1,93 (флаг bank_specific значит «сильно обгоняет рынок», а не «рынок
    стоит»). Иначе — во сколько раз наш рост сильнее рыночного."""
    if not ratio:
        return None
    if market_flat(market_ratio):
        return f"только у {who}: по рынку тема ровная"
    k = float(ratio) / float(market_ratio)
    if k >= 1.3:
        return f"в {_ru_num(k)} {_raz(k)} сильнее рынка (у рынка ×{_ru_num(market_ratio)})"
    return f"рынок растёт так же (×{_ru_num(market_ratio)})"


def _dm_iso(iso: str | None) -> str:
    """'2026-09-25' → '25.09'."""
    s = str(iso or "")[:10]
    return f"{s[8:10]}.{s[5:7]}" if len(s) == 10 else s


def signal_note(s: dict) -> str | None:
    """Сколько сигнал держится и чем — для текстов модели и заголовка:
    «держится с 25.09 (6-й день); две недели подряд выше нормы: 10 и 12».
    Без этого продолжение выглядело новым всплеском, а сигнал, поднятый
    неделю назад, — сегодняшней новостью."""
    bits = []
    if s.get("status") == "continuing" and s.get("since"):
        t = f"держится с {_dm_iso(s['since'])} ({s.get('days_on')}-й день)"
        bb = s.get("baseline_before")
        if bb is not None and abs(float(bb) - float(s.get("baseline_week") or 0)) >= 0.3:
            t += f", до всплеска норма была ~{_ru_num(bb)}/нед"
        bits.append(t)
    if s.get("sustained"):
        bits.append(f"две недели подряд выше нормы: {s.get('prev_week')} и {s.get('week')}")
    return "; ".join(bits) or None


_ONLY_RX = re.compile(r"только\s+у\s+(Сбера|Сбербанка|банка|нас)(?![а-яё])", re.I)


# «рынок не повторяет», «в отличие от рынка», «рынок не растёт» — утверждение,
# что тема растёт только у банка
_NOT_MARKET_RX = re.compile(
    r"(,\s*)?(котор\w+|что)\s+рынок\s+не\s+(повтор\w+|видит|показыва\w+|разделя\w+)"
    r"|(,\s*)?в\s+отличие\s+от\s+рынка"
    r"|(,\s*)?(а\s+|при\s+этом\s+)?рынок\s+(же\s+)?(не\s+раст[её]т|стоит\s+на\s+месте|ровный)",
    re.I)
# ускорение: модель пишет «нарастают» и «*Ускоряется.*» сама, без признака в данных
_ACCEL_WORDS = {"ускоряется": "сохраняется", "ускоряются": "сохраняются",
                "нарастает": "сохраняется", "нарастают": "сохраняются",
                "усиливается": "сохраняется", "усиливаются": "сохраняются"}
_ACCEL_RX = re.compile(r"(?<![а-яё])(" + "|".join(_ACCEL_WORDS) + r")(?![а-яё])", re.I)
_ACCEL_MARK_RX = re.compile(r"\s*\*?\s*(ускоряется|нарастает|усиливается)\s*\.?\s*\*?\s*(?=$|[А-ЯЁA-Z])",
                            re.I)
_SENT_RX = re.compile(r"(?<=[.!?…])\s+(?=[*«\"(]?[А-ЯЁA-Z])")


def _sig_stems(s: dict) -> set[str]:
    """Как тема сигнала упоминается в тексте: основы слов короткого названия и
    первого слова полного («Чарджбэк» → «чардж», «Аресты и списания…» → «арест»)."""
    words = re.findall(r"[а-яёa-z]{4,}", f"{s.get('short') or ''} "
                       f"{(s.get('label') or '').split(' ')[0]}".lower())
    return {w[:5] for w in words}


def _mentioned(chunk: str, signals: list[dict]) -> list[dict]:
    low = chunk.lower()
    out = []
    for s in signals:
        stems = _sig_stems(s)
        if stems and any(re.search(r"(?<![а-яёa-z])" + re.escape(st), low) for st in stems):
            out.append(s)
    return out


def _accelerates(s: dict):
    """True/False — растёт ли тема быстрее прошлой недели; None — данных нет."""
    if s.get("accel"):
        return True
    if s.get("prev_week") is not None and s.get("week") is not None:
        return float(s["week"]) > float(s["prev_week"])
    return False if "accel" in s else None


def _fix_chunk(chunk: str, topics: list[dict]) -> str:
    if not topics:
        return chunk
    acc = [_accelerates(t) for t in topics]
    if _ACCEL_RX.search(chunk) and acc and all(a is False for a in acc):
        chunk = _ACCEL_MARK_RX.sub(" ", chunk)
        chunk = _ACCEL_RX.sub(lambda m: _match_case(m.group(1),
                                                    _ACCEL_WORDS[m.group(1).lower()]), chunk)
    loud = [t for t in topics
            if t.get("market_ratio") is not None and not _is_flat(t)]
    if loud and _NOT_MARKET_RX.search(chunk):
        chunk = _NOT_MARKET_RX.sub("", chunk, count=1)
        notes = "; ".join(f"«{(t.get('short') or t.get('label') or '').strip()}» растёт и у "
                          f"рынка (×{_ru_num(t['market_ratio'])})" for t in loud[:2])
        chunk = re.sub(r"\s*([.!?…]*)\s*$", lambda m: f" ({notes}){m.group(1)}", chunk, count=1)
    if loud:
        chunk = _ONLY_RX.sub(lambda m: f"у {m.group(1)} сильнее, чем по рынку", chunk)
    return re.sub(r"[ \t]{2,}", " ", chunk).replace(" .", ".").replace(" ,", ",")


def _match_case(src: str, repl: str) -> str:
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl


def fix_market_claims(text: str | None, signals: list[dict] | None) -> str | None:
    """Страховка на выходе модели — сверка слов с числами того же сигнала.

    Раньше правилась только фраза «только у …», и только если ни один сигнал
    не был ровным по рынку: в «Для вас» ровные есть всегда, и страховка там не
    работала, а «рынок не повторяет» под неё не попадало вовсе (аудит 03.10).
    Теперь по каждой теме, упомянутой в пункте или предложении:
    • «ускоряется/нарастают» — только при признаке ускорения или росте к
      прошлой неделе (было «*Ускоряется.*» при 14 → 9);
    • «рынок не повторяет/в отличие от рынка/только у банка» — только если
      по этой теме рынок ровный; иначе оговорка снимается и дописывается
      рыночный коэффициент.
    Текст без упоминания тем — прежнее глобальное правило для «только у …»."""
    if not text or not signals:
        return text
    out_lines = []
    for ln in text.split("\n"):
        if ln.strip().startswith(("- ", "* ")):
            out_lines.append(_fix_chunk(ln, _mentioned(ln, signals)))
            continue
        parts = _SENT_RX.split(ln)
        out_lines.append(" ".join(_fix_chunk(p, _mentioned(p, signals)) for p in parts))
    text = "\n".join(out_lines)
    if any(s.get("ratio") and _is_flat(s) for s in signals):
        return text
    return _ONLY_RX.sub(lambda m: f"у {m.group(1)} сильнее, чем по рынку", text)


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
    # банк против рынка — по сопоставимому корпусу с обеих сторон (CMP_SOURCE)
    mlab = _topic_week_counts(None, product, exclude_bank=bc, source=CMP_SOURCE)
    mrow = mlab[1] if mlab else None
    clab = _topic_week_counts(bc, product, source=CMP_SOURCE)
    crow = clab[1] if clab else None
    BASE_W = 7.0
    diverge = []
    # разброс недель (перерассеяние) — общий по всем темам банка и рынка: жалобы идут
    # волнами, и тест доли с φ = 1 на волнах давал «быстрее рынка» почти каждую неделю
    # (ревью 04.11.2026: на нулевой модели с φ = 2 — в 47% недель)
    def _phi(row) -> float:
        if row is None:
            return 1.0
        ser = [list(row.get(f'{t["key"]}_wk') or [])[2:9] for t in theme_defs]
        ser = [x for x in ser if len(x) == 7]
        return max(1.0, _RS.pooled_overdispersion(ser)) if ser else 1.0
    phi_b, phi_m = _phi(crow), _phi(mrow)
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
        p = poisson_sf(w0, bw)
        # «быстрее рынка» — точный условный тест доли банка (банк/рынок × неделя/база),
        # а не отношение двух отношений: прежнее «×1,3 быстрее рынка» на малых числах
        # было шумом. «По рынку мало данных» — отдельный вывод, не «рынок ровный».
        v = (_RS.market_verdict(int(crow[f"{k}_w0"]), int(mrow[f"{k}_w0"]), int(crow[f"{k}_b"]),
                                int(mrow[f"{k}_b"]), k=1 / BASE_W, phi=phi_b, phi_market=phi_m)
             if mrow is not None and crow is not None else None)
        diverge.append({
            "key": k, "label": t["label"], "short": t["short"],
            "risk": t["risk"], "week": w0, "baseline_week": round(bw, 1),
            "base_count": b, "base_weeks": int(BASE_W),
            # рост против своей нормы подтверждён только с поправкой на число
            # проверенных проблем — иначе это ожидаемое случайное отклонение
            "p": round(p, 5), "sig": p < PULSE_ALPHA / max(len(theme_defs), 1),
            "ratio": round(ratio, 2),
            "market_ratio": round(mratio, 2) if mratio else None,
            # без данных по рынку отношения нет: иначе потребители с порогом gap ≥ 1,15
            # показывали «быстрее рынка ×15» по теме, где у рынка ноль недель
            "gap": (round(v["gap"], 2) if v and v.get("gap") and v["claim"] != "market_unknown"
                    else (None if v else (round(gap, 2) if gap else None))),
            "gap_lo": round(v["gap_lo"], 2) if v and v.get("gap_lo") else None,
            "market_claim": v["claim"] if v else "market_unknown",
            "market_p": v["p"] if v else None,
            "market_flat": bool(v and v["market_flat"]),
            "market_note": _RS.market_note(v) if v else "по рынку данных мало для сравнения",
        })
    # поправка на число проверенных проблем: «быстрее рынка» — только при q < 0,05
    qv, _rej = _RS.bh({d["key"]: d["market_p"] for d in diverge if d["market_p"] is not None}, 0.05)
    for d in diverge:
        q = qv.get(d["key"]) if isinstance(qv, dict) else None
        d["market_q"] = round(q, 5) if q is not None else None
        d["faster_sig"] = bool(d["market_claim"] in ("only_bank", "faster") and q is not None and q <= 0.05)
    diverge.sort(key=lambda d: (d["faster_sig"], (d["gap"] or d["ratio"]), d["week"]), reverse=True)
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
    p = poisson_sf(int(w_unc), b_unc / 7.0) if b_unc else None
    return {"week": w_unc, "week_total": w_tot,
            "pct": round(100 * w_unc / w_tot) if w_tot else 0,
            "baseline_week": base_week,
            "ratio": round(w_unc / base_week, 2) if base_week >= 1 else None,
            # «возможен новый инцидент» — только если рост не случаен:
            # 13 при норме 9,9 (P ≈ 0,2) раньше подсвечивалось как тревога
            "p": round(p, 5) if p is not None else None,
            "sig": bool(p is not None and p < PULSE_ALPHA),
            "src": "annotation"}


# ── Сигналы недели: вход, удержание, две недели ──────────────────────────────
# 29–30.09 всплеск жалоб на чарджбэк (14 при норме 3,4, вторую неделю подряд)
# пропал из «Обзора»: поправку на множественность делили на все 39 проблем, и
# q = 0,071 не прошло порог. Сигнал мигал при том, что жалоб не стало меньше.
# Теперь:
#   • поправка — отдельно для частых проблем (норма от _SIG_MIN_BASE в неделю)
#     и для редких. Группа выбирается по норме, не по текущей неделе: отбор по
#     самому всплеску сделал бы поправку нечестной. У редкой проблемы вход и
#     так решает практический порог (8 жалоб при норме меньше двух — рост в
#     четыре с лишним раза), а в общей группе она размывала поправку для частых.
#     Бюджет ошибок прежний — 5% на банк: 4% частым, 1% редким;
#   • проблема проверяется за неделю и за две недели подряд — затяжной рост
#     виден, даже когда ни одна неделя по отдельности порог не пробивает. Две
#     проверки одной проблемы сводятся в одну (взвешенный Симс, неделе — 3/4),
#     лишних гипотез нет, и недельный порог не строже прежнего;
#   • поднятый сигнал держится, пока неделя заметно выше нормы ДО всплеска:
#     иначе через неделю-две всплеск входит в норму и растворяется в ней.
# Состояние не хранится — история проигрывается по дням из тех же жалоб,
# поэтому сигнал любого дня воспроизводится. Проверка на истории 45 дней:
# у Сбера новых эпизодов нет, чарджбэк держится 24–30.09 без провала.
_SIG_Q = 0.05
_SIG_ALPHA = {"main": 0.04, "rare": 0.01}
_SIG_MIN_BASE = 2.0        # норма в неделю, с которой проблема — «частая»
_SIG_LOOKBACK = 21         # сколько дней истории проигрывается ради удержания


def _topic_day_counts(bank_canon: str, product: str | None,
                      days: int) -> dict[str, list[int]] | None:
    """Жалобы по главной проблеме по дням: [0] — последний полный день перед
    концом недели сигнала, [1] — день до него и т.д. Отбор тот же, что у
    _topic_week_counts, поэтому недели, сложенные из дней, совпадают с ней."""
    try:
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT i.issue, floor(extract(epoch FROM {_WEEK_END} - i.dt) / 86400)::int AS d,
                       count(*)
                FROM review_index i
                WHERE i.bank = :bank
                  AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
                  AND {_CMP}
                  AND i.dt >= {_WEEK_END} - make_interval(days => :days) AND i.dt < {_WEEK_END}
                  AND (i.ev_date IS NULL OR i.ev_date >= i.dt::date - 60)
                GROUP BY 1, 2
            """), {"bank": bank_canon, "product": product, "days": days}).all()
    except Exception as e:  # noqa: BLE001
        log.warning("topic_day_counts: %s", e)
        return None
    out: dict[str, list[int]] = {}
    for code, d, n in rows:
        if 0 <= int(d) < days:
            out.setdefault(code, [0] * days)[int(d)] += int(n)
    return out


def _weeks_at(daily: list[int] | None, a: int, n: int = 9) -> list[int]:
    """Недели 0..n-1 для якоря на a дней раньше: неделя k — дни [a+7k, a+7k+7)."""
    if not daily:
        return [0] * n
    return [sum(daily[a + 7 * k:a + 7 * k + 7]) for k in range(n)]


# Вес недельной проверки в общем p проблемы: неделя — главный вопрос радара,
# и с весом 3/4 при бюджете частых 4% порог недели не строже прежнего
# (0,04 × 0,75 / 23 ≈ 0,05 / 39); двухнедельной — оставшаяся четверть.
_SIG_W_WEEK = 0.75


def _simes2(p1: float, p2: float, w1: float = _SIG_W_WEEK) -> float:
    """Одно p на две проверки одной проблемы (p1 — неделя, p2 — две недели):
    взвешенный Симс (Беньямини — Хохберг, 1997). Проверки положительно
    зависимы (неделя входит в обе), для них он корректен и мягче Бонферрони."""
    w2 = 1.0 - w1
    a, b = sorted(((p1, w1), (p2, w2)), key=lambda t: t[0] / t[1])
    return min(1.0, a[0] / a[1], b[0])


def _practical(w0: int, w1: int, bw: float, floor_: int = 8) -> tuple[bool, bool]:
    """Практические пороги: (всплеск недели, рост двух недель подряд)."""
    week_ok = w0 >= floor_ and w0 - bw >= 5 and (bw < 0.5 or w0 >= 1.5 * bw)
    s2 = w0 + w1
    # обе недели заметно выше нормы, и вместе — не горстка жалоб
    two_ok = s2 >= 12 and s2 - 2 * bw >= 8 and min(w0, w1) >= max(1.5 * bw, bw + 3)
    return week_ok, two_ok


def _signal_entries(weeks: dict[str, list[int]]) -> dict[str, dict]:
    """Проблемы, по которым на этом якоре сигнал входит. weeks — недели 0..8.

    Вход: значимый рост (q проблемы в своей группе ниже её доли бюджета) и
    практический порог — всплеск недели, рост двух недель подряд или новая
    проблема (почти не было — и сразу ≥ 6)."""
    st: dict[str, dict] = {}
    # общий разброс среза: по 7 неделям одной проблемы его не оценить; приор по всем
    # проблемам удерживает долю ложных у номинала (review_stats.phi_prior)
    prior = _RS.phi_prior([list(arr[2:9]) for arr in weeks.values()]) if weeks else None
    for k, arr in weeks.items():
        base = list(arr[2:9])
        b = sum(base)
        w0, w1 = int(arr[0]), int(arr[1])
        p1, p2 = _nb_tail(w0, base, prior), _nb_tail_sum(w0 + w1, base, 2, prior)
        st[k] = {"w0": w0, "w1": w1, "b": b, "bw": b / 7.0, "p1": p1, "p2": p2,
                 "p": _simes2(p1, p2),
                 "fam": "main" if b / 7.0 >= _SIG_MIN_BASE else "rare"}
    qv: dict[str, float] = {}
    size: dict[str, int] = {}
    for fam in ("main", "rare"):
        ks = [k for k in st if st[k]["fam"] == fam]
        size[fam] = len(ks)
        qv.update(_bh({k: st[k]["p"] for k in ks}))
    out: dict[str, dict] = {}
    for k, r in st.items():
        if qv[k] >= _SIG_ALPHA[r["fam"]]:
            continue
        week_ok, two_ok = _practical(r["w0"], r["w1"], r["bw"])
        new = r["b"] <= 2 and r["w0"] >= 6
        if not (week_ok or two_ok or new):
            continue
        test = "week" if week_ok else ("new_topic" if new else "two_weeks")
        out[k] = {"test": test, "sustained": bool(two_ok), "new": bool(new),
                  "p": r["p"], "q": qv[k], "p1": r["p1"], "p2": r["p2"],
                  "alpha": _SIG_ALPHA[r["fam"]], "fam": r["fam"], "fam_size": size[r["fam"]],
                  "prior": prior}
    return out


def _signal_hold(w0: int, w1: int, base: list[int], entry: str, prior=None) -> float | None:
    """p удержания: неделя (или две) против нормы ДО всплеска. None — уже не
    выше той нормы по практическому порогу (сигнал снимается)."""
    bw = sum(base) / 7.0
    week_ok, two_ok = _practical(w0, w1, bw, 6 if entry == "new_topic" else 8)
    p1 = _nb_tail(w0, base, prior) if week_ok else None
    p2 = _nb_tail_sum(w0 + w1, base, 2, prior) if two_ok else None
    if p1 is not None and p2 is not None:
        return _simes2(p1, p2)
    return p1 if p1 is not None else p2


def _signal_chain(daily: dict[str, list[int]], keys: list[str],
                  lookback: int = _SIG_LOOKBACK, a0: int = 0) -> dict[str, dict]:
    """Проигрывает якоря от a0+lookback до a0 и возвращает сигналы на a0.

    Сигнал поднимается любым входом (_signal_entries) и держится, пока каждый
    день либо снова проходит вход, либо неделя значимо выше нормы, какой она
    была в день подъёма (q < 0,05 среди удерживаемых). since — сколько дней
    назад от a0 он поднялся."""
    state: dict[str, dict] = {}
    for a in range(a0 + lookback, a0 - 1, -1):
        weeks = {k: _weeks_at(daily.get(k), a) for k in keys}
        ent = _signal_entries(weeks)
        hp: dict[str, float] = {}
        for k, ch in state.items():
            if k not in ent:
                p = _signal_hold(weeks[k][0], weeks[k][1], ch["base"], ch["entry"], ch.get("prior"))
                if p is not None:
                    hp[k] = p
        hq = _bh(hp)
        nxt: dict[str, dict] = {}
        for k, ch in state.items():
            if k in ent:
                nxt[k] = {**ch, **ent[k]}
            elif k in hq and hq[k] < _SIG_Q:
                sus = _practical(weeks[k][0], weeks[k][1], sum(ch["base"]) / 7.0)[1]
                nxt[k] = {**ch, "test": "hold", "sustained": sus, "p": hp[k], "q": hq[k]}
        for k, e in ent.items():
            if k not in nxt:
                nxt[k] = {**e, "start": a, "entry": e["test"], "base": list(weeks[k][2:9])}
        state = nxt
    return {k: {**ch, "since": ch["start"] - a0, "weeks": _weeks_at(daily.get(k), a0)}
            for k, ch in state.items()}


@_safe(None)
def weekly_signals(bank: str, product: str | None = None) -> dict | None:
    """Всплески жалоб за 7 дней по главной проблеме из LLM-разметки.

    Сигнал — не «выросло в ×1,8», а статистически значимый рост:
      • норма — 7 прошлых недель (окно 14–63 дня), с их собственным разбросом
        (отрицательно-биномиальное распределение);
      • поправка на множественность — отдельно по частым и редким проблемам
        банка (q < 0,05), и практический порог: ≥ 8 жалоб, избыток ≥ 5 над
        нормой, рост ≥ ×1,5;
      • вторая проверка — две недели подряд выше нормы (затяжной рост);
      • поднятый сигнал держится, пока неделя значимо выше нормы до всплеска
        (status «continuing», since — дата подъёма).
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
        daily = _topic_day_counts(bc, product, 63 + _SIG_LOOKBACK)
        if daily is None:
            return None
        # «банк против рынка» — по сопоставимому корпусу с обеих сторон (CMP_SOURCE) и
        # тестом доли (review_stats.market_verdict): прежнее правило «mratio < 1,4 или
        # ratio ≥ 1,8·mratio» читало нехватку данных по рынку как «рынок ровный»
        mlab = _topic_week_counts(None, product, exclude_bank=bc, source=CMP_SOURCE)
        mrow = mlab[1] if mlab else None
        clab = _topic_week_counts(bc, product, source=CMP_SOURCE)
        crow = clab[1] if clab else None

        def _phi(row) -> float:
            if row is None:
                return 1.0
            ser = [list(row.get(f'{t["key"]}_wk') or [])[2:9] for t in theme_defs]
            ser = [x for x in ser if len(x) == 7]
            return max(1.0, _RS.pooled_overdispersion(ser)) if ser else 1.0
        phi_b, phi_m = _phi(crow), _phi(mrow)
        tdef = {t["key"]: t for t in theme_defs}
        chain = _signal_chain(daily, list(tdef))
        we = week_end()
        issue_day = (_dt.date.fromisoformat(we) + _dt.timedelta(days=1)) if we else None
        out = []
        for k, ch in chain.items():
            t = tdef[k]
            wk = ch["weeks"]
            w0, w1, b = int(wk[0]), int(wk[1]), int(sum(wk[2:9]))
            bw = b / BASE_W
            ratio = (w0 / bw) if bw >= 0.5 else None
            excess = w0 - bw
            accel = w0 > w1 and w0 >= max(8, 1.4 * w1)
            mv = (_RS.market_verdict(int(crow[f"{k}_w0"]), int(mrow[f"{k}_w0"]), int(crow[f"{k}_b"]),
                                     int(mrow[f"{k}_b"]), k=1 / BASE_W, phi=phi_b, phi_market=phi_m)
                  if mrow is not None and crow is not None else None)
            mratio = round(mv["market_ratio"], 2) if mv and mv.get("market_ratio") is not None else None
            bank_specific = bool(mv and mv["bank_specific"])
            since = int(ch["since"])
            fbw = sum(ch["base"]) / BASE_W
            s_ = {"key": k, "label": t["label"], "short": t["short"],
                  "risk": t["risk"], "group": t["group"],
                  "week": w0, "prev_week": w1,
                  "base_count": b, "base_weeks": int(BASE_W),
                  "week_total": int(brow["_tw0"]),
                  "baseline_week": round(bw, 1),
                  "ratio": (round(ratio, 1) if ratio else None),
                  "excess": round(excess, 1),
                  "p_value": round(ch["p"], 5), "q_value": round(ch["q"], 5),
                  "new": bool(ch.get("new")), "accel": bool(accel),
                  "market_ratio": mratio, "bank_specific": bool(bank_specific),
                  "market_flat": bool(mv and mv["market_flat"]),
                  "market_claim": mv["claim"] if mv else "market_unknown",
                  "market_note": (_RS.market_note(mv, "банка") if mv
                                  else "по рынку данных мало для сравнения"),
                  # какой проверкой сигнал держится сегодня и с какого дня
                  "test": ch["test"], "entry_test": ch["entry"],
                  "status": "continuing" if since > 0 else "new",
                  "since": (issue_day - _dt.timedelta(days=since)).isoformat() if issue_day else None,
                  "days_on": since + 1,
                  "sustained": bool(ch.get("sustained")),
                  "two_weeks": w0 + w1, "two_weeks_norm": round(2 * bw, 1),
                  "p_two_weeks": round(_nb_tail_sum(w0 + w1, list(wk[2:9]), 2, ch.get("prior")), 5),
                  "family": ch.get("fam"), "family_size": ch.get("fam_size")}
            if since > 0:
                s_["baseline_before"] = round(fbw, 1)
                s_["ratio_before"] = round(w0 / fbw, 1) if fbw >= 0.5 else None
            out.append(s_)
        for s_ in out:
            strong = s_["q_value"] < 0.001 and s_["week"] >= 12
            s_["level"] = "high" if (strong or (s_["risk"] == "compliance" and s_["q_value"] < 0.01)
                                     or (s_["bank_specific"] and (s_["ratio"] or 0) >= 2.5)) else "medium"
        # новые — впереди продолжающихся того же уровня
        out.sort(key=lambda s_: (s_["level"] == "high", s_["status"] == "new", s_["excess"]),
                 reverse=True)
        if out:
            top = out[0]
            try:
                with db.session() as s:
                    grows = s.execute(text(f"""
                        SELECT i.city, count(*) FROM review_index i
                        WHERE i.bank = :bank AND i.issue = :key AND {_CMP}
                          AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
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
                "week_end": we, "src": "annotation"}
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
               month: str | None = None, source: str | None = None,
               bank: str | None = None) -> set[str] | None:
    """Какие из найденных отзывов показывать — тот же срез, что у ленты (ReviewQuery):
    жалобы (и ещё не размеченные), при теме — с этой главной проблемой, при признаке —
    с этим признаком разметки. Окно и город поиск учёл сам. None — отбор не сработал."""
    if not urls:
        return set()
    rq = ReviewQuery(bank=bank, product=product, theme=theme, flag=flag, esc=esc,
                     source=source, month=month)
    if rq.error():
        return set()
    p: dict = {"u": urls}
    where = rq.where_sql(p, window=False, city=False)
    try:
        with db.session() as s:
            known = set(s.execute(text("SELECT i.url FROM review_index i WHERE i.url = ANY(:u)"),
                                  {"u": urls}).scalars().all())
            ok = set(s.execute(text(
                f"SELECT i.url FROM review_index i WHERE i.url = ANY(:u) AND {where}"),
                p).scalars().all())
    except Exception as e:                                     # noqa: BLE001
        log.warning("reviews_dash: отбор выдачи по разметке не сработал (%s)", e)
        return None                 # не «всё подходит»: вызывающий скажет, что поиск не отработал
    # отзыв, которого нет в индексе (вне окна зеркала), без фильтров не отбрасываем
    return ok | ({u for u in urls if u not in known}
                 if not (theme or esc or product or flag or month or source) else set())


def _nb_tail(x: int, weeks: list[int], prior=None) -> float:
    """P(X ≥ x) при недельной норме из истории — предсказательный хвост
    (rag/review_stats.predictive_tail): ошибка самой нормы из 7 недель и
    перерассеяние с его собственной неопределённостью, prior — общий разброс среза
    (phi_prior). Правило V6, включено 04.11.2026: прежде норма и разброс
    подставлялись как точные, p занижалось в 2–4 раза — на нулевой модели 8,7 / 32 / 49
    ложных эпизодов в год на срез при φ = 1 / 1,5 / 2, с V6 — 1,3 / 10 / 13; на истории
    Сбера за полгода 14 эпизодов → 7 (крупные, вкл. чарджбэк 23.09, остались).
    Решение и замеры — AuditLens_сигналы_решение_2026-11-04 (вне репозитория)."""
    return _RS.predictive_tail(int(x), [float(w) for w in weeks], phi_prior=prior)


def _nb_tail_v1(x: int, weeks: list[int]) -> float:
    """Прежний хвост (до 04.11.2026) — для сравнения в замерах."""
    m, v = _base_mv(weeks)
    return _tail_mv(x, m, v)


def _base_mv(weeks: list[int]) -> tuple[float, float]:
    """Среднее и разброс недельной нормы (нулевая база — не бесконечный рост)."""
    n = len(weeks)
    m = sum(weeks) / n if n else 0.0
    m = max(m, 0.5)
    v = (sum((w - m) ** 2 for w in weeks) / (n - 1)) if n > 1 else m
    return m, v


def _nb_tail_sum(x: int, weeks: list[int], k: int = 2, prior=None) -> float:
    """P(сумма k недель подряд ≥ x) при норме из истории — тот же предсказательный
    хвост, что у _nb_tail, на k недель (волны длиннее недели учтены в нём)."""
    return _RS.predictive_tail(int(x), [float(w) for w in weeks], k=k, phi_prior=prior)


def _nb_tail_sum_v1(x: int, weeks: list[int], k: int = 2) -> float:
    """Прежний хвост суммы (до 04.11.2026) — для сравнения в замерах."""
    m, v = _base_mv(weeks)
    sums = [sum(weeks[i:i + k]) for i in range(len(weeks) - k + 1)]
    vs = 0.0
    if len(sums) > 1:
        ms = sum(sums) / len(sums)
        vs = sum((s_ - ms) ** 2 for s_ in sums) / (len(sums) - 1)
    return _tail_mv(x, k * m, max(k * v, vs))


def _tail_mv(x: int, m: float, v: float) -> float:
    """P(X ≥ x): отрицательно-биномиальное с заданными средним и разбросом,
    при разбросе не больше среднего — Пуассон."""
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


def _rate_change(n: int, prev: int, k: float = 1.0) -> dict | None:
    """Изменение счётчика к прошлому окну: 95% ДИ в процентах и p
    (пуассоновское отношение интенсивностей). k — во сколько раз текущее окно
    длиннее прошлого (review_period.Period.k): ожидаемое «как раньше» = prev·k."""
    if n <= 0 or prev <= 0:
        return None
    se = math.sqrt(1 / n + 1 / prev)
    lr = math.log(n / (prev * k))
    return {"lo": round(100 * (math.exp(lr - 1.96 * se) - 1)),
            "hi": round(100 * (math.exp(lr + 1.96 * se) - 1)), "p": _p2(lr / se)}


# Выборка недельного сигнала по теме: главная проблема, жалоба, последние :d дней
# до конца недели сигнала, события не старше 60 дней — как в _topic_week_counts.
_EVIDENCE_WHERE = f"""i.bank = :bank AND i.issue = :key AND {_CMP}
              AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
              AND i.dt >= {_WEEK_END} - make_interval(days => :d) AND i.dt < {_WEEK_END}
              AND (i.ev_date IS NULL OR i.ev_date >= i.dt::date - 60)"""


@_safe([])
def signal_evidence(bank: str, key: str, product: str | None = None, days: int = 7,
                    limit: int = 30) -> list[dict]:
    """Жалобы, из которых сложился сигнал: та же выборка, что в его счётчике
    (главная проблема, жалоба, события не старше 60 дней). Именно их, а не
    «последние жалобы банка» читает модель, когда объясняет сигнал: 22.09
    сводка взяла формулировку из отзыва другой темы. Сколько их всего — не
    len() от этого списка (он обрезан limit), а signal_evidence_total."""
    bc = resolve_bank(bank)
    if not bc or key not in cb.ISSUES:
        return []
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.url, i.dt, i.city, a.summary, a.quote, a.quote_ok, a.esc,
                   a.no_consent, a.misled, a.issues2
            FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE {_EVIDENCE_WHERE}
            ORDER BY i.dt DESC LIMIT :lim
        """), {"bank": bc, "key": key, "product": product, "d": days, "lim": limit,
               "sv": _ann_schema()}).mappings().all()
    return [{"url": r["url"], "date": r["dt"].date().isoformat() if r["dt"] else None,
             "city": r["city"], "summary": r["summary"],
             "quote": r["quote"] if r["quote_ok"] else None,
             "esc": r["esc"], "no_consent": bool(r["no_consent"]), "misled": bool(r["misled"])}
            for r in rows]


@_safe(None)
def signal_evidence_total(bank: str, key: str, product: str | None = None,
                          days: int = 7) -> int | None:
    """Сколько жалоб в выборке сигнала — отдельным подсчётом по индексу, как
    счётчик недели (_topic_week_counts) и риск-карта (themes). До 04.11.2026
    инструмент ИИ называл числом жалоб темы длину выборки с потолком 40: на
    неделе с большим всплеском модель писала «40», а вкладка — больше."""
    bc = resolve_bank(bank)
    if not bc or key not in cb.ISSUES:
        return None
    with db.session() as s:
        n = s.execute(text(f"SELECT count(*) FROM review_index i WHERE {_EVIDENCE_WHERE}"),
                      {"bank": bc, "key": key, "product": product, "d": days}).scalar()
    return int(n or 0)


@_safe([])
def novel_clusters(bank: str, product: str | None = None, days: int = 7,
                   min_n: int = 5, sim: float = 0.78, history_weeks: int = 8) -> list[dict]:
    """Новые сюжеты недели, сгруппированные кодом: формулировки проблем вне
    кодификатора, похожие по смыслу (векторы bge-m3), от min_n жалоб.

    Раньше «новую тему» решала модель по списку из 20 жалоб и склеивала
    разнородное: три разных случая («скрыли альтернативу», «не дали бонус»,
    «пенсионер») назвала одной темой. Теперь группы считает код, модель их
    только называет.

    «Новое» — только то, чего не было: сюжет сверяется с изложениями жалоб за
    history_weeks недель до окна. 03.10 «Новое: банк занижает суммы по
    исполнительным листам — 5 жалоб» шло пунктом, хотя та же история жила в
    группе из 8 жалоб с июля; порог был 3 жалобы (аудит 03.10, ОТЗ-03)."""
    return _cached(f"novel:{bank}:{product}:{days}:{min_n}:{history_weeks}",
                   lambda: _novel_clusters(bank, product, days, min_n, sim, history_weeks),
                   ttl=900)


def _novel_clusters(bank, product, days, min_n, sim, history_weeks) -> list[dict]:
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
        its = [rows[j] for j in members]
        dates = sorted(x["date"] for x in its if x.get("date"))
        out.append({"n": len(members), "topic": rows[i]["new_topic"], "items": its,
                    "urls": [x["url"] for x in its],
                    "first": dates[0] if dates else None, "last": dates[-1] if dates else None})
    if not out:
        return out
    seen = _seen_before(resolve_bank(bank), out, days, history_weeks)
    if seen is None:
        # проверить историю нечем — «новым» сюжет не объявляем
        return []
    return [c for c, old in zip(out, seen) if not old]


def _seen_before(bc: str | None, clusters: list[dict], days: int,
                 weeks: int, sim: float = 0.84, min_hits: int = 2) -> list[bool] | None:
    """Был ли сюжет раньше: изложения жалоб банка за weeks недель до окна,
    похожие на центр группы (порог 0,84 — «та же история», см. reviews_work)."""
    if not bc:
        return None
    try:
        import numpy as np
        from . import embedder
        from .reviews_work import _store_vecs, _vecs_for
        urls = [u for c in clusters for u in c["urls"]]
        have = _vecs_for(urls)
        miss = [(x["url"], x.get("summary") or x.get("new_topic") or "")
                for c in clusters for x in c["items"] if x["url"] not in have]
        if miss:
            vv = embedder.embed_batch([t[:400] for _u, t in miss])
            fresh = {u: v for (u, _t), v in zip(miss, vv)}
            _store_vecs(fresh)
            have.update(fresh)
        with db.session() as s:
            hist = s.execute(text(f"""
                SELECT v.vec FROM review_summary_vec v
                JOIN review_index i ON i.url = v.url
                WHERE i.bank = :b AND {_CMP}
                  AND i.dt >= {_WEEK_END} - make_interval(days => :d + :hd)
                  AND i.dt < {_WEEK_END} - make_interval(days => :d)
                ORDER BY i.dt DESC LIMIT 5000"""),
                {"b": bc, "d": days, "hd": weeks * 7}).all()
    except Exception as e:  # noqa: BLE001
        log.info("novel_clusters: история не проверена (%s)", e)
        return None
    if not hist:
        return [False] * len(clusters)
    H = np.asarray([h[0] for h in hist], dtype=np.float32)
    H /= np.linalg.norm(H, axis=1, keepdims=True) + 1e-9
    out = []
    for c in clusters:
        mv = [have[u] for u in c["urls"] if u in have]
        if not mv:
            out.append(True)               # не с чем сравнить — не «новое»
            continue
        M = np.asarray(mv, dtype=np.float32)
        M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
        cen = M.mean(axis=0)
        cen /= np.linalg.norm(cen) + 1e-9
        out.append(int(((H @ cen) >= sim).sum()) >= min_hits)
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
              AND (CAST(:product AS text) IS NULL OR al_product_is(i.product, i.slices, :product))
              AND i.dt >= {_WEEK_END} - make_interval(days => :d) AND i.dt < {_WEEK_END}
              AND (a.issue = 'other' OR a.code_fit = 'approx') AND a.new_topic IS NOT NULL
            ORDER BY i.dt DESC LIMIT :lim
        """), {"bank": bc, "product": product, "d": days, "lim": limit,
               "sv": _ann_schema()}).mappings().all()
    return [{"url": r["url"], "date": r["dt"].date().isoformat() if r["dt"] else None,
             "new_topic": r["new_topic"], "summary": r["summary"], "issue": r["issue"]}
            for r in rows]
