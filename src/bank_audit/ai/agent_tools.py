"""Инструменты данных AuditLens для агента Hermes (подключаются через MCP).

Тонкий слой: каждый инструмент зовёт ту же функцию, что рисует вкладку, и
отдаёт её данные компактным JSON с русскими подписями и ссылкой на ту же
страницу AuditLens. Выводы, сюжеты и формулировки — работа модели; методика
(норма, эскалация, ПСК против рекламной ставки) — в навыках Hermes
(deploy/hermes-al/skills), которые агент дописывает сам по опыту.

Почему не psql и curl, как раньше: аудит 25.09 — модель угадывала таблицы,
путала поля, выдумывала рейтинг, упиралась в лимит шагов. Здесь числа ровно
те, что видит пользователь, а аргументы — перечни допустимых значений.

Инструменты только читают (запись в базу у агента есть через alsql в
терминале). Синхронные (БД) — MCP-сервер зовёт их в пуле потоков.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .. import categories as _cm
from ..rag import review_codebook as _cb
from ..rag import review_glossary as _gl

log = logging.getLogger(__name__)


def _rs_labels() -> tuple[str, ...]:
    from ..rag import review_slices
    return tuple(review_slices.LABELS.values())


MSK = ZoneInfo("Europe/Moscow")
SBER = "Сбербанк"

# Перечни допустимых значений — модель видит их в схеме инструмента.
THEME_KEYS = tuple(k for k in _cb.ISSUES if k != "no_issue")
# продукты кодификатора и особые срезы «Аудита отзывов» (зарплатные проекты, УС, выездной
# сервис, самозанятые и «Своё дело», дети и молодёжь, участники СВО): без срезов модель на
# вопрос о «Своём деле» искала по продукту «Бизнес» и приносила жалобы на СберБизнес
PRODUCT_LABELS = (tuple(v for v in _cb.PRODUCTS.values() if v)
                  + tuple(lbl for lbl in _rs_labels() if lbl not in _cb.PRODUCTS.values()))
CATEGORY_IDS = tuple(c["id"] for c in _cm.CATEGORIES)
Theme = Literal[THEME_KEYS]            # type: ignore[valid-type]
Product = Literal[PRODUCT_LABELS]      # type: ignore[valid-type]
Category = Literal[CATEGORY_IDS]       # type: ignore[valid-type]

THEMES_HELP = "; ".join(f"{k} — {_cb.ISSUES[k][0]}" for k in THEME_KEYS)
CATEGORIES_HELP = "; ".join(f"{c['id']} — {c['label']}" for c in _cm.CATEGORIES)


def theme_label(key: str) -> str:
    """Подпись темы для человека: служебный ключ в интерфейс не выходит."""
    return _cb.ISSUES[key][0] if key in _cb.ISSUES else key


# ── вывод ────────────────────────────────────────────────────────────────────

def _clean(v):
    """Числа — округлённые, даты — ISO, пустое — выкинуть."""
    if isinstance(v, dict):
        r = {k: _clean(x) for k, x in v.items()}
        return {k: x for k, x in r.items() if x not in (None, "", [], {})}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, _dt.datetime):
        return v.astimezone(MSK).strftime("%Y-%m-%d %H:%M") if v.tzinfo else v.isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    return v


def out(obj) -> str:
    return json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"), default=str)


def clip(s, limit: int = 240) -> str | None:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    if not s:
        return None
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def today_msk() -> _dt.date:
    return _dt.datetime.now(MSK).date()


# ── ссылки на страницы AuditLens ─────────────────────────────────────────────
# Фронт читает параметры из hash: #reviews?theme=…, #market?cat=…, #knowledge?doc=…

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# ключ фильтра «Площадка» во вкладке → как её называть человеку
SOURCE_LABEL = {"banki": "banki.ru", "sravni": "sravni.ru", "bankiros": "bankiros.ru",
                "finuslugi": "finuslugi.ru"}


def _iso_day(d) -> str | None:
    """Дата → «ГГГГ-ММ-ДД»; что на дату не похоже — None."""
    if d is None or d == "":
        return None
    s = (d.isoformat() if isinstance(d, _dt.date) else str(d)).strip()[:10]
    if not _ISO_DAY.match(s):
        return None
    try:
        _dt.date.fromisoformat(s)
    except ValueError:
        return None
    return s


def source_key(source: str | None) -> str | None:
    """Площадка → ключ фильтра вкладки (banki, sravni, bankiros, finuslugi).
    Жалобы приходят с источником индекса («bankiru», «banki_reviews»,
    «sravni_reviews») или доменом («banki.ru»); banki.ru — одна площадка, хоть
    в индексе и два источника. Незнакомое — None."""
    if not source:
        return None
    s = str(source).strip().lower().removeprefix("www.")
    keys = _rd()._SOURCE_KEYS
    if s in keys:
        return s
    for k, raw in keys.items():
        if s in raw or s.split(".")[0] == k:
            return k
    return None


def link_reviews(bank: str | None = SBER, product: str | None = None,
                 theme: str | None = None, days: int | None = None,
                 city: str | None = None, q: str | None = None, *,
                 flag: str | None = None, source: str | None = None,
                 date_from=None, date_to=None, period=None, esc: bool = False,
                 week_end: str | None = None) -> str:
    """Ссылка на «Аудит отзывов» с тем же срезом, по которому посчитано число.

    Параметры — как у вкладки (ReviewsPage в app.jsx): tab, bank, product, p или
    from/to, theme, flag, esc, city, source, q. До 04.11.2026 ссылка теряла
    признак, площадку и период «с — по»: аудитор открывал вкладку и видел не то
    число, что назвал ИИ. Период пишется всегда, в том числе 90 дней: без него
    вкладка берёт период, выбранный в этом браузере в прошлый раз.

    period — review_period.Period, по которому считали (важнее дат); date_from и
    date_to — «с — по» включительно; days — «последние N дней»; week_end — неделя
    всплеска (month=sig:…: те же жалобы, что в его числе, с отсевом старых событий,
    поэтому «с — по» здесь не годится). Неизвестные тема,
    признак и площадка в ссылку не идут: вкладка показала бы ошибку вместо жалоб."""
    # класс среза и словарь признаков — из модуля напрямую, не через _rd(): ссылка —
    # чистая функция, данные ей не нужны (заглушка _rd() в тестах ронял сбор целиком)
    from ..rag.reviews_dash import ReviewQuery, _flag_sql
    we = _iso_day(week_end)
    d0 = _iso_day(date_from) if period is None else None
    # ссылку строит тот же объект среза, что и ленту, выгрузку и группы (ReviewQuery)
    return ReviewQuery(
        bank=bank, product=product, theme=theme if theme in _cb.ISSUES else None,
        flag=flag if flag and _flag_sql(flag) else None, esc=bool(esc), city=city,
        source=source_key(source), month=f"sig:{we}" if we else None,
        period=period, days=days if (period is None and not d0) else None, q=q,
        date_from=d0, date_to=_iso_day(date_to) if d0 else None).to_url()


def link_stories(bank: str, product: str | None, period) -> str:
    """Ссылка на «Сюжеты» того же среза: лента жалоб, вид «сюжеты»."""
    from ..rag.reviews_dash import ReviewQuery
    url = ReviewQuery(bank=bank, product=product, period=period).to_url(tab="complaints")
    return url + ("&" if "?" in url else "?") + "view=stories"


def _week_dates(week_end, days: int = 7) -> tuple[str | None, str | None]:
    """Дни выборки недельного сигнала «с — по» включительно: она кончается днём
    week_end (reviews_dash.week_end) и длится days дней."""
    d1 = _iso_day(week_end)
    if not d1:
        return None, None
    end = _dt.date.fromisoformat(d1)
    return (end - _dt.timedelta(days=max(1, int(days)) - 1)).isoformat(), d1


def _period(product: str | None, days: int, date_from: str | None = None,
            date_to: str | None = None):
    """Период инструмента — так же, как его строит вкладка (web/app._review_period):
    «с — по» включительно или последние N дней; у особых срезов («Зарплатные
    проекты» и др.) — своё начало данных. ValueError — даты не разобрать."""
    from ..rag import review_period as rp
    d0, d1 = _iso_day(date_from), _iso_day(date_to)
    if (date_from and not d0) or (date_to and not d1):
        raise ValueError("даты периода — в виде ГГГГ-ММ-ДД, например 2026-05-01")
    if d1 and not d0:
        raise ValueError("date_to задана без date_from: укажите начало периода")
    return rp.from_request(None, d0, d1, None, None if d0 else days,
                           data_start=_rd().data_start_for(product))


def link_market(cat: str | None = None, q: str | None = None) -> str:
    qs = urlencode({k: v for k, v in {"cat": cat, "q": q}.items() if v})
    return "#market" + ("?" + qs if qs else "")


def link_doc(document_id) -> str:
    return f"#knowledge?doc={int(document_id)}"


# ── доступ к данным вкладок ──────────────────────────────────────────────────

def _rd():
    from ..rag import reviews_dash
    return reviews_dash


def _rw():
    from ..rag import reviews_work
    return reviews_work


def _web():
    from ..web import app
    return app


def _q(sql: str, params: dict | None = None) -> list[dict]:
    from sqlalchemy import text
    from .. import db
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params or {}).mappings().all()]


def _signal(s: dict, bank: str, product: str | None, week_end: str | None = None) -> dict:
    g = s.get("geo") or {}
    # ссылка — на жалобы самой недели сигнала (month=sig:…), а не «последние 7 дней»
    # от сегодня: неделя кончается последним полным днём данных
    return {"theme": s.get("key"), "label": s.get("label"), "week": s.get("week"),
            "prev_week": s.get("prev_week"), "norm_per_week": s.get("baseline_week"),
            "ratio": s.get("ratio"), "market_ratio": s.get("market_ratio"),
            "market_note": s.get("market_note"), "bank_specific": s.get("bank_specific"),
            "new": s.get("new"), "accelerating": s.get("accel"), "level": s.get("level"),
            # продолжение ли это и с какого дня: «держится с 25.09» — не новый всплеск
            "status": s.get("status"), "since": s.get("since"),
            "two_weeks_in_a_row": s.get("sustained"),
            "norm_before_spike": s.get("baseline_before"),
            "top_city": g.get("city"), "top_city_share_pct": g.get("share"),
            "link": link_reviews(bank, product, s.get("key"), 7, week_end=week_end)}


def _complaint(it: dict, text_chars: int = 700) -> dict:
    """Жалоба из ленты или карточки: пересказ разметки + начало текста."""
    a = it.get("ann") or {}
    esc = a.get("esc")
    return {"date": it.get("date"), "city": it.get("city"), "product": it.get("product"),
            "themes": [t.get("label") for t in (it.get("themes") or [])[:3]] or None,
            "summary": a.get("summary"), "quote": a.get("quote"),
            "escalation": esc if esc not in (None, "none") else None,
            "escalation_to": a.get("esc_to") or None, "amount_rub": a.get("amount"),
            "text": clip(it.get("text"), text_chars), "source": it.get("source"),
            "url": it.get("url")}


# ── инструменты ──────────────────────────────────────────────────────────────

def tool_day_brief() -> str:
    from ..digest import store
    doc = store.read_latest(today_msk())
    S = doc.get("sections") or {}
    hp = (S.get("headline") or {}).get("payload") or {}
    ins = []
    for x in hp.get("insights") or []:
        dr = x.get("drill") or {}
        pr = dr.get("params") or {}
        ins.append({"title": x.get("title"), "why_it_matters": x.get("so_what"),
                    "what_to_check": x.get("idea"), "severity": x.get("severity"),
                    "evidence": x.get("evidence"), "url": dr.get("url"),
                    "link": link_reviews(theme=pr.get("theme"), city=pr.get("city"), days=7)
                    if dr.get("page") == "reviews" else None})
    upd = ((S.get("update") or {}).get("payload") or {}).get("items") or []
    return out({
        "date": doc.get("date"),
        "snapshot": "выпуск собирается в 07:00 МСК, числа в нём — на это время",
        "headline": hp.get("headline"), "insights": ins,
        "market_note": hp.get("market_note"), "quiet_note": hp.get("quiet_note"),
        "complaints_brief_markdown":
            ((S.get("reviews_brief") or {}).get("payload") or {}).get("markdown"),
        "update_of_the_day": [{"title": x.get("title"), "summary": x.get("summary"),
                               "what_to_check": x.get("idea"), "ts": x.get("ts"),
                               "url": x.get("url")} for x in upd[:6]],
        "link": "#overview"})


def tool_complaint_signals(bank: str = SBER, product: Product | None = None) -> str:
    rd = _rd()
    ws = rd.weekly_signals(bank, product) or {}
    wp = rd.week_pulse(bank, product) or {}
    ov = ws.get("overall") or {}
    we = ws.get("week_end")
    keys = {s.get("key") for s in ws.get("signals") or []}
    sigs = []
    for i, s in enumerate(ws.get("signals") or []):
        item = _signal(s, bank, product, we)
        if i < 3:
            # Жалобы, из которых сложился всплеск: причина почти всегда видна в
            # них самих (одно событие, продавец, сервис), а не в названии темы.
            ev = rd.signal_evidence(bank, s["key"], product, 7, 20) or []
            item["complaints"] = [{k: e.get(k) for k in ("date", "city", "summary", "quote",
                                                          "url")} for e in ev]
        sigs.append(item)
    d0, d1 = _week_dates(we)
    return out({
        "bank": bank, "product": product, "week_end": we,
        "overall": {"week": ov.get("week"), "norm_per_week": ov.get("baseline_week"),
                    "ratio": ov.get("ratio"),
                    # рынок — только banki.ru; сравнивать его с cmp_ratio (банк там же),
                    # а не с ratio по всем площадкам
                    "market_ratio": ov.get("market_ratio"), "cmp_ratio": ov.get("cmp_ratio")},
        "signals": sigs,
        "watch_faster_than_market": [_signal(s, bank, product, we)
                                     for s in (wp.get("diverge") or [])[:6] if s.get("faster_sig")
                                     if s.get("key") not in keys],
        "counting": _gl.COMPLAINT_SHORT + "; неделя — по главной проблеме, события не "
                    "старше 60 дней",
        "link": link_reviews(bank, product, days=7, date_from=d0, date_to=d1)})


def tool_complaint_theme(theme: Theme, bank: str = SBER, product: Product | None = None,
                         days: int = 7, date_from: str | None = None,
                         date_to: str | None = None) -> str:
    rd, rw = _rd(), _rw()
    days = max(1, min(int(days or 7), 365))
    dated = bool(date_from or date_to)
    try:
        per = _period(product, days, date_from, date_to)
    except ValueError as e:
        return out({"error": str(e)})
    ws = rd.weekly_signals(bank, product) or {}
    we = ws.get("week_end")
    sig = next((s for s in ws.get("signals") or [] if s.get("key") == theme), None)
    if not sig:
        sig = next((s for s in (rd.week_pulse(bank, product) or {}).get("diverge") or []
                    if s.get("key") == theme), None)
    res: dict = {"theme": theme, "label": _cb.ISSUES[theme][0], "bank": bank,
                 "product": product, "days": per.days if dated else days, "week_end": we,
                 "weekly_signal": _signal(sig, bank, product, we) if sig else None}
    if days <= 7 and not dated:
        # ровно те жалобы, из которых сложился недельный счётчик; сколько их всего —
        # отдельным подсчётом: список обрезан 40 строками, и до 04.11.2026 модель
        # называла числом жалоб темы его длину
        ev = rd.signal_evidence(bank, theme, product, days, 40) or []
        total = rd.signal_evidence_total(bank, theme, product, days)
        cards = {c["url"]: c for c in (rw.reviews_by_urls([e["url"] for e in ev]) if ev else [])}
        items = []
        for e in ev:
            c = cards.get(e["url"]) or {}
            ann = {**(c.get("ann") or {}), "summary": e.get("summary"),
                   "quote": e.get("quote"), "esc": e.get("esc")}
            items.append(_complaint({**c, "ann": ann, "date": e.get("date"),
                                     "city": e.get("city"), "url": e.get("url")}))
        what = ("жалоб недельной выборки темы (она — главная проблема жалобы; события не "
                "старше 60 дней — как в числе всплеска)")
        if days == 7 and we:          # неделя всплеска во вкладке — ровно эти жалобы
            link = link_reviews(bank, product, theme, days, week_end=we)
        else:
            d0, d1 = _week_dates(we, days)
            link = link_reviews(bank, product, theme, days, date_from=d0, date_to=d1)
        cl = rw.clusters(bank, product, 7, theme) or {}
    else:
        feed = rd.list_reviews_ex(bank, product=product, theme=theme, period=per,
                                  limit=25, sort="date") or {}
        items = [_complaint(it, 500) for it in feed.get("items") or []]
        total = feed.get("total")
        what = "последних жалоб, где эта тема — главная проблема"
        link = link_reviews(bank, product, theme, period=per)
        cl = rw.clusters(bank, product, per.days, theme, period=per) or {}
    res["total"], res["shown"] = total, len(items)
    res["sample"] = (f"показано {len(items)} из {total} {what}" if total is not None
                     else f"показано {len(items)} {what}")
    urls = {i.get("url") for i in items}
    res["similar_groups"] = [{"n": g.get("n"), "in_sample": len(urls & set(g.get("urls") or [])),
                              "summary": g.get("summary"), "quote": g.get("quote"),
                              "first": g.get("first"), "last": g.get("last"),
                              "cities": g.get("cities"), "escalation_threats": g.get("esc")}
                             for g in (cl.get("clusters") or [])[:6]]
    res["complaints"] = items
    res["counting"] = _gl.COMPLAINT_SHORT
    res["glossary"] = _gl.terms("тема")
    res["link"] = link
    return out(res)


def _by_source(rows: list[dict] | None, bank: str, product: str | None, per) -> list[dict]:
    """Площадки — как фильтр вкладки: banki.ru одной строкой (в индексе это два
    источника — внешний корпус и наш сбор), со ссылкой на жалобы этой площадки."""
    acc: dict[str, dict] = {}
    for x in rows or []:
        k = source_key(x.get("source"))
        name = SOURCE_LABEL.get(k or "", x.get("source") or "—")
        it = acc.setdefault(name, {"source": name, "n": 0,
                                   "link": link_reviews(bank, product, source=k, period=per)
                                   if k else None})
        it["n"] += int(x.get("n") or 0)
    return sorted(acc.values(), key=lambda x: -x["n"])


def tool_complaints_overview(bank: str = SBER, product: Product | None = None,
                             days: int = 90, date_from: str | None = None,
                             date_to: str | None = None) -> str:
    rd = _rd()
    days = max(7, min(int(days or 90), 730))
    try:
        per = _period(product, days, date_from, date_to)
    except ValueError as e:
        return out({"error": str(e)})
    ov = rd.overview(bank, product, period=per) or {}
    if not ov:
        return out({"error": f"банк «{bank}» не найден в корпусе отзывов"})
    th = rd.themes(bank, product, period=per) or {}
    rf = rd.risk_flags(bank, product, period=per) or {}
    geo = rd.geo(bank, product, per.days, 20, period=per) or {}
    info = per.info()
    return out({
        "bank": ov.get("bank"), "product": product, "days": per.days,
        "period": {"from": info.get("from"), "to": info.get("to")}, "as_of": ov.get("as_of"),
        "complaints": ov.get("total"), "prev_period": ov.get("prev"),
        "change_pct": ov.get("delta_pct"), "last_window_incomplete": ov.get("delta_partial"),
        "share_of_all_bank_complaints_pct": ov.get("market_share_pct"),
        "rank_by_complaints": ov.get("market_rank"), "banks_in_corpus": ov.get("market_banks"),
        "escalation_pct": ov.get("escalation_pct"),
        "escalation_filed_pct": ov.get("escalation_filed_pct"),
        "market_escalation_pct": ov.get("market_escalation_pct"),
        "escalation_differs_significantly": ov.get("escalation_sig"),
        "themes": [{"theme": t["key"], "label": t["label"], "n": t["n"], "pct": t.get("pct"),
                    "prev": t.get("prev"), "change_pct": t.get("delta_pct"),
                    "change_significant": t.get("delta_sig"),
                    "link": link_reviews(bank, product, t["key"], period=per)}
                   for t in (th.get("themes") or [])[:14]],
        "what_changed": [{"text": x.get("text"), "detail": x.get("detail")}
                         for x in (rd.changes(bank, product, period=per) or {}).get("items")
                         or []],
        "risk_flags": [{"group": g.get("label"), "flag": it.get("label"), "n": it.get("n"),
                        "pct": it.get("pct"), "market_pct": it.get("market_pct"),
                        "index": it.get("index"), "significant": it.get("sig"),
                        "link": link_reviews(bank, product, flag=it.get("flag"), period=per)}
                       for g in rf.get("groups") or [] for it in g.get("items") or []],
        "banks_by_complaints": (rd.vs_market(bank, product, period=per) or {}).get("rows"),
        "cities": [{"city": c["city"], "n": c["n"], "index": c.get("index"),
                    "above_normal": c.get("anomaly")} for c in (geo.get("cities") or [])[:10]],
        "by_source": _by_source(ov.get("by_source"), bank, product, per),
        "counting": _gl.COMPLAINT_SHORT,
        "glossary": _gl.terms("тема", "срез"),
        "link": link_reviews(bank, product, period=per)})


def tool_complaint_search(query: str, bank: str = SBER, product: Product | None = None,
                          theme: Theme | None = None, days: int = 90, limit: int = 10,
                          date_from: str | None = None, date_to: str | None = None) -> str:
    rd = _rd()
    days = max(1, min(int(days or 90), 730))
    try:
        per = _period(product, days, date_from, date_to)
    except ValueError as e:
        return out({"error": str(e)})
    # период — тем же объектом, что строит вкладка по ссылке ниже: набор поиска и
    # его счётчики совпадают с тем, что аудитор увидит, открыв ссылку
    res = rd.list_reviews_ex(bank, product=product, theme=theme, q=query, period=per,
                             limit=max(1, min(int(limit or 10), 25)), sort="auto") or {}
    sm = res.get("search") or {}
    return out({"query": query, "bank": bank, "product": product, "theme": theme,
                "days": per.days, "total": res.get("total"),
                # сбой поиска — не «0 жалоб»: модель должна сказать, что поиск не отработал
                "error": res.get("error") or ("search_failed: поиск по словам не отработал (сбой базы), "
                                              "число жалоб неизвестно — повторить позже"
                                              if sm.get("failed") or sm.get("words_failed") else None),
                # total = все жалобы, где слова запроса есть (by_words), плюс до 20
                # похожих по смыслу без этих слов (by_sense) — то же число видит
                # аудитор во вкладке; «нашлось N» говорить только по by_words
                "by_words": sm.get("n_words"), "by_sense": sm.get("n_sense"),
                "by_words_capped": sm.get("words_capped"),
                "glossary": _gl.terms("со словами запроса", "по смыслу"),
                "complaints": [_complaint(it, 500) for it in res.get("items") or []],
                "link": link_reviews(bank, product, theme, q=query, period=per)})


def tool_complaint_stories(bank: str = SBER, product: Product | None = None, days: int = 90,
                           limit: int = 8, date_from: str | None = None,
                           date_to: str | None = None) -> str:
    """«Сюжеты» раздела «Аудит отзывов»: устойчивые группы похожих историй клиентов.

    Поиск жалоб писался, когда разметки ИИ и сюжетов ещё не было, и помощник не
    видел того, что аудитор видит во вкладке: какая история повторяется, сколько
    в ней жалоб, новая она или выросла. Числа — те же, что во вкладке."""
    from ..rag import review_stories as rs
    rd, rw = _rd(), _rw()
    days = max(7, min(int(days or 90), 365))
    try:
        per = _period(product, days, date_from, date_to)
    except ValueError as e:
        return out({"error": str(e)})
    bc = rd.resolve_bank(bank)
    if not bc:
        return out({"error": f"банк «{bank}» не найден в корпусе отзывов"})
    b = per.binds()
    meta: dict = {}
    try:
        items = rs.list_stories(bc, product=product, limit=max(1, min(int(limit or 8), 20)),
                                t0=b["t0"], t1=b["t1"], _meta=meta)
    except Exception as e:  # noqa: BLE001 — сбой — не «сюжетов нет»
        log.info("complaint_stories: %s", type(e).__name__)
        return out({"error": "stories_failed: сюжеты не прочитаны (сбой базы), повторить позже"})
    try:
        grown = {int(g["story_id"]): g for g in rs.story_signals(bc, product=product) or []}
    except Exception as e:  # noqa: BLE001 — рост за неделю необязателен
        log.info("complaint_stories, рост недели: %s", type(e).__name__)
        grown = {}
    cards = {c["url"]: c for c in (rw.reviews_by_urls(
        [u for x in items for u in (x.get("top_urls") or [])[:2]]) if items else [])}
    stories = []
    for x in items:
        g = grown.get(x["story_id"]) or {}
        stories.append({
            "title": x["title"], "what": x.get("summary"), "problem": x.get("issue_label"),
            "complaints_in_period": x["n_window"], "complaints_all_time": x["n"],
            "first_seen": (x.get("first_seen") or "")[:10], "last_seen": (x.get("last_seen") or "")[:10],
            "status": "затих" if x.get("status") == "quiet" else "действует",
            "badges": [bd.get("label") for bd in x.get("badges") or []] or None,
            # вырос за неделю сверх своей нормы — то же правило, что у всплесков тем
            "grew_this_week": ({"week": g.get("week"), "norm_per_week": g.get("baseline_week"),
                                "ratio": g.get("ratio"), "same_at_other_banks": g.get("market")}
                               if g else None),
            "examples": [_complaint(cards[u], 500) for u in (x.get("top_urls") or [])[:2]
                         if u in cards]})
    info = per.info()
    return out({"bank": bc, "product": product, "days": per.days,
                "period": {"from": info.get("from"), "to": info.get("to")},
                "stories": stories, "total_stories": meta.get("total", len(stories)),
                "counting": ("сюжет — устойчивая группа похожих историй клиентов, от трёх жалоб; "
                             "пересчитываются раз в сутки по жалобам за 90 дней; число в сюжете "
                             "— жалобы периода, которые в него входят, а не все жалобы темы"),
                "glossary": _gl.terms("тема"),
                "link": link_stories(bc, product, per)})


_CELL_KEYS = ("category", "label", "rank", "n_banks", "percentile", "tied", "metric_label",
              "metric_unit", "lower_is_better", "title", "value", "gap_median", "gap_leader",
              "gap_unit", "degenerate", "small_n", "teaser", "no_metric", "implausible_excluded",
              "subsidized_excluded", "comparable", "attainability",
              # голова — главная группа; слияние видов и окна срока справкой
              "group_label", "overall", "by_term", "near_guard", "psk_mismatch")


def tool_market_position(category: Category | None = None) -> str:
    mv = _web().market_verdict(None) or {}
    cells = [{k: c.get(k) for k in _CELL_KEYS} | {"link": link_market(c.get("category"))}
             for c in mv.get("cells") or [] if not category or c.get("category") == category]
    return out({"as_of": mv.get("as_of"), "lead": None if category else mv.get("lead"),
                "cells": cells, "weak_spots": mv.get("weak"),
                "method_caveats": mv.get("doubts")})


_OFFER_KEYS = ("bank_name", "is_sber", "title", "segment", "sub_segment", "rate_pct",
               "rate_kind", "psk_min", "psk_max", "rate_min", "rate_max", "term_months_min",
               "term_months_max", "amount_min", "amount_max", "fee_open", "fee_service",
               "grace_days", "cashback_pct", "capitalization", "replenishable", "early_withdraw",
               "conditions", "rate_requires", "attain", "free_kind", "implausible_reason",
               "url")

# Две даты, которые легко спутать. Помощник назвал «датой витрины 22.09» день,
# с которого у вклада не менялась ставка, — аудитор прочёл это как «данным
# полтора месяца» (12.11). Поэтому у предложения дата названа по смыслу, а дата
# самой витрины отдаётся отдельным полем и объяснена словами.
_OFFER_DATES = ("as_of — день последнего обновления витрины «Рынок»: предложения действуют "
                "на эту дату, её и называй датой данных; terms_since у предложения — с "
                "какого дня действуют его нынешние условия (числа с тех пор не менялись), "
                "это не дата сбора данных")


def _day(v) -> str | None:
    s = str(v or "")[:10]
    return s if len(s) == 10 else None


# Места считает код. Модель получала десять строк по убыванию ставки и сама
# расставляла номера: в отчёте 12.11 вклады под 14,77% и 14,7% оказались третьим
# и четвёртым, а шесть вкладов под 15% — «пятым местом, ничья».
_OFFER_PLACES = ("place — место предложения среди предложений категории по метрике (не место "
                 "банка: оно в market_position). Одинаковое значение делит места: «3–8» — "
                 "ничья шести предложений. Топ-N — строки по порядку мест; если граница N "
                 "попадает внутрь ничьей, называй всех равных. Список не обрезан внутри ничьей")
_TOP_MAX = 40          # ничья на границе списка дотягивается целиком, но не бесконечно


def _places(values: list) -> list[str | None]:
    """Места по значениям, уже стоящим от лучшего к худшему; равные делят места."""
    keys = [None if v is None else round(float(v), 4) for v in values]
    res: list[str | None] = []
    i = 0
    while i < len(keys):
        j = i
        while j + 1 < len(keys) and keys[j + 1] == keys[i] and keys[i] is not None:
            j += 1
        label = None if keys[i] is None else (str(i + 1) if i == j else f"{i + 1}–{j + 1}")
        res += [label] * (j - i + 1)
        i = j + 1
    return res


def tool_market_offers(category: Category, banks: list[str] | None = None,
                       query: str | None = None, limit: int = 12) -> str:
    meta = _cm.CAT_META[category]
    metric, lower = meta["metric"], meta["metric_lower_is_better"]

    def val(r):
        v = r.get(metric)
        if v is None and metric == "psk_min":
            v = r.get("rate_pct")
        return float(v) if v is not None else None

    def rows(q: str | None, n: int) -> list[dict]:
        rs = _web()._market_rows(category, n, 0, q, None, None, None) or []
        rs.sort(key=lambda r: (val(r) is None, (val(r) or 0) * (1 if lower else -1)))
        return rs

    def pack(r):
        return {k: r.get(k) for k in _OFFER_KEYS} | {
            "terms_since": _day(r.get("valid_from")), "metric_value": val(r)}

    try:
        as_of = _day(_web().market_as_of())
    except Exception:  # noqa: BLE001 — дата справочная, предложения важнее
        as_of = None

    res: dict = {"category": category, "label": meta["label"], "metric": meta["metric_label"],
                 "metric_unit": meta["metric_unit"].strip(), "lower_is_better": lower,
                 "how_to_compare": meta.get("caveat"), "as_of": as_of, "dates": _OFFER_DATES,
                 "link": link_market(category, query)}
    if banks:
        names = list(dict.fromkeys(
            ([SBER] if not any("сбер" in b.lower() for b in banks) else []) + banks))
        res["by_bank"] = {}
        for b in names:
            rs = [r for r in rows(b, 80) if _same_bank(b, r.get("bank_name"))]
            if query:
                words = _words(query)
                rs = [r for r in rs if words & _words(r.get("title") or "")] or rs
            res["by_bank"][b] = {"offers_total": len(rs), "best": [pack(r) for r in rs[:5]]}
    else:
        rs = rows(query, 200)
        res["offers_total"] = len(rs)
        n = max(1, min(int(limit or 12), 30))
        # не режем внутри ничьей: половина равных на границе — это случайные имена
        while n < min(len(rs), _TOP_MAX) and val(rs[n]) is not None and val(rs[n]) == val(rs[n - 1]):
            n += 1
        if query:           # отбор по словам — не рынок целиком, мест у него нет
            res["top"] = [pack(r) for r in rs[:n]]
        else:
            places = _places([val(r) for r in rs])
            res["top"] = [pack(r) | {"place": places[i]} for i, r in enumerate(rs[:n])]
            res["places"] = _OFFER_PLACES
        sber = next((r for r in rs if r.get("is_sber")), None)
        res["sber_best"] = pack(sber) if sber else None
    return out(res)


def _words(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]{4,}", (s or "").lower())}


def _same_bank(a: str, b: str | None) -> bool:
    na = re.sub(r"[^a-zа-я0-9]", "", (a or "").lower().replace("ё", "е"))
    nb = re.sub(r"[^a-zа-я0-9]", "", (b or "").lower().replace("ё", "е"))
    return bool(na and nb) and (na in nb or nb in na)


def tool_bank_profile(bank: str) -> str:
    rows = _web().banks() or []
    cand = [r for r in rows if _same_bank(bank, r.get("name")) or r.get("slug") == bank.lower()]
    cand.sort(key=lambda r: (len(r.get("name") or "") != len(bank),
                             -(r.get("total_reviews") or 0)))
    if not cand:
        return out({"error": f"банк «{bank}» не найден в витрине «Банки»"})
    r = cand[0]
    res: dict = {
        "bank": r.get("name"),
        "bankiru_people_rating": {"score": r.get("rating_score"), "place": r.get("place"),
                                  "avg_grade_of_5": r.get("avg_grade"),
                                  "reviews_total": r.get("total_reviews"),
                                  "reviews_last_year": r.get("reviews_year"),
                                  "solved_pct": r.get("solved_pct"),
                                  "updated": r.get("rating_at")},
        "our_corpus": {"reviews": r.get("own_reviews"), "avg_rating": r.get("own_avg_rating"),
                       "last_review": r.get("own_last_dt")},
        "other_name_matches": [c.get("name") for c in cand[1:4]], "link": "#banks"}
    ov = _rd().overview(r["name"], None, 90) or {}
    if ov:
        th = _rd().themes(r["name"], None, 90) or {}
        res["complaints_90d"] = {"n": ov.get("total"), "change_pct": ov.get("delta_pct"),
                                 "escalation_pct": ov.get("escalation_pct"),
                                 "market_escalation_pct": ov.get("market_escalation_pct"),
                                 "top_themes": [{"label": t["label"], "n": t["n"]}
                                                for t in (th.get("themes") or [])[:6]],
                                 "link": link_reviews(r["name"], days=90)}
    return out(res)


def tool_knowledge_search(query: str, bank: str | None = None, doc_type: str | None = None,
                          fresh_days: int | None = None) -> str:
    from ..rag.retriever import hybrid_search
    slug = _bank_slug(bank) if bank else None
    res = hybrid_search(query, limit=6, per_doc=3, bank_slugs=[slug] if slug else None,
                        doc_types=[doc_type] if doc_type else None,
                        max_age_days=fresh_days or None)
    groups = res.get("groups") or []
    # сниппет поиска — 100 знаков, строка тарифа в нём обрывается («3,9% + …»);
    # для первых документов отдаём фрагменты целиком
    ctx = _chunks({g["document_id"]: [h["idx"] for h in (g.get("hits") or [])[:2]]
                   for g in groups[:3]})
    docs = [{"document_id": g["document_id"], "title": clip(g.get("title"), 160),
             "head": clip(g.get("text_head"), 160), "bank": g.get("bank_name"),
             "type": g.get("doc_type"), "domain": g.get("source_domain"),
             "loaded": g.get("fetched_at"), "url": g.get("url"),
             "link": link_doc(g["document_id"]),
             "fragments": ctx.get(g["document_id"]) or
             [clip((h.get("snippet") or "").replace("⟦", "").replace("⟧", ""), 400)
              for h in (g.get("hits") or [])[:3]]}
            for g in groups]
    return out({"query": query, "bank_filter": bank if slug else None,
                "total_documents": res.get("total"), "documents": docs})


def _chunks(want: dict[int, list[int]]) -> dict[int, list[str]]:
    if not want:
        return {}
    # фрагменты вне поиска (меню, хвост интерфейса) в контекст модели не идут
    rows = _q("SELECT dc.document_id, dc.idx, dc.text FROM document_chunk dc "
              "WHERE dc.document_id = ANY(:d) AND NOT EXISTS (SELECT 1 FROM "
              "document_chunk_excluded x WHERE x.chunk_id = dc.chunk_id)",
              {"d": list(want)})
    by: dict[int, dict[int, str]] = {}
    for r in rows:
        by.setdefault(r["document_id"], {})[r["idx"]] = r["text"] or ""
    res: dict[int, list[str]] = {}
    for d, idxs in want.items():
        seen: set[int] = set()
        for i in idxs:
            if i in seen:
                continue
            seen.update({i, i + 1})
            txt = " ".join(by.get(d, {}).get(j, "") for j in (i, i + 1)).strip()
            if txt:
                res.setdefault(d, []).append(txt[:1600])
    return res


def _bank_slug(bank: str) -> str | None:
    rows = _q("""SELECT slug FROM bank WHERE lower(name) = lower(:b) OR slug = lower(:b)
                  OR lower(name) LIKE lower(:bb)
                  ORDER BY (lower(name) = lower(:b)) DESC, (slug NOT LIKE 'unknown_%') DESC
                  LIMIT 1""", {"b": bank, "bb": f"%{bank}%"})
    return rows[0]["slug"] if rows else None


def tool_knowledge_read(document_id: int, query: str | None = None) -> str:
    head = _q("""SELECT d.document_id, d.url, d.title, d.doc_type::text AS doc_type,
                        d.fetched_at, length(d.content_text) AS chars, b.name AS bank
                   FROM document d LEFT JOIN bank b ON b.bank_id = d.bank_id
                  WHERE d.document_id = :i""", {"i": int(document_id)})
    if not head:
        return out({"error": f"документ {document_id} не найден"})
    h = head[0] | {"link": link_doc(document_id)}
    if query:
        chunks = _q("SELECT dc.idx, dc.headings_path, dc.text FROM document_chunk dc "
                    "WHERE dc.document_id = :i AND NOT EXISTS (SELECT 1 FROM "
                    "document_chunk_excluded x WHERE x.chunk_id = dc.chunk_id) "
                    "ORDER BY dc.idx", {"i": int(document_id)})
        words, nums = _words(query), set(re.findall(r"\d+", query))
        scored = []
        for c in chunks:
            t = (c["text"] or "").lower()
            sc = sum(1 for w in words if w in t) + sum(1 for x in nums if x in t)
            if sc:
                scored.append((sc, c["idx"]))
        keep = sorted({j for _, i in sorted(scored, key=lambda x: (-x[0], x[1]))[:4]
                       for j in (i, i + 1)})
        by = {c["idx"]: c for c in chunks}
        parts, budget = [], 8000
        for i in keep:
            c = by.get(i)
            if c and budget > 0:
                parts.append({"place": c.get("headings_path") or str(i),
                              "text": (c["text"] or "")[:budget]})
                budget -= len(c["text"] or "")
        if parts:
            return out(h | {"matched_by": query, "parts": parts})
        h["note"] = f"по словам «{query}» совпадений нет — отдано начало документа"
    t = _q("SELECT left(content_text, 8000) AS t FROM document WHERE document_id = :i",
           {"i": int(document_id)})
    return out(h | {"text": t[0]["t"] if t else None})


_NEWS_COLS = "url, parent_url, source, ts, title, body, value, s2"


def tool_news_find(query: str | None = None, url: str | None = None, days: int = 7) -> str:
    days = max(1, min(int(days or 7), 60))
    if url:
        u = url.strip().split("?")[0].rstrip("/")
        rows = _q(f"""SELECT {_NEWS_COLS} FROM news_item
                      WHERE rtrim(split_part(url, '?', 1), '/') = :u
                         OR rtrim(split_part(parent_url, '?', 1), '/') = :u
                      ORDER BY (rtrim(split_part(url, '?', 1), '/') = :u) DESC, ts DESC
                      LIMIT 6""", {"u": u})
        pool = [] if rows else _digest_news(days, url=u)
        return out({"url": u, "news": [_news(r) for r in rows], "from_daily_briefs": pool,
                    "not_in_feed": not rows and not pool})
    if not query:
        return out({"error": "нужен query или url"})
    rows = _news_semantic(query, days)
    have = {r["url"] for r in rows}
    return out({"query": query, "days": days, "news": [_news(r) for r in rows],
                "from_daily_briefs": [p for p in _digest_news(days, query=query)
                                      if p.get("url") not in have][:6]})


def _news(r: dict) -> dict:
    s2 = r.get("s2") or {}
    return {"ts": r.get("ts"), "source": r.get("source"), "importance_of_10": r.get("value"),
            "title": clip(r.get("title"), 300), "text": clip(r.get("body"), 1500),
            "summary": s2.get("summary"), "affects_sber": s2.get("sber"),
            "what_to_check": s2.get("idea"), "what_to_request": s2.get("request"),
            "deadline": s2.get("deadline"), "n_sources": s2.get("n_sources"),
            "url": r.get("url"), "roundup_post": r.get("parent_url")}


def _news_semantic(query: str, days: int) -> list[dict]:
    rows: list[dict] = []
    try:
        from ..rag import embedder
        vec = embedder.embed_batch([query])[0]
        vs = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        rows = _q(f"""SELECT {_NEWS_COLS}, 1 - (emb <=> CAST(:v AS vector)) AS sim
                      FROM news_item
                      WHERE emb IS NOT NULL AND ts > now() - make_interval(days => :d)
                      ORDER BY emb <=> CAST(:v AS vector) LIMIT 8""", {"v": vs, "d": days})
        rows = [r for r in rows if (r.get("sim") or 0) >= 0.45]
    except Exception as e:  # noqa: BLE001 — без векторов остаётся поиск по словам
        log.info("news semantic: %s", e)
    words = sorted(_words(query))[:6]
    if words:
        conds = " OR ".join(f"(title ILIKE :w{i} OR body ILIKE :w{i})" for i in range(len(words)))
        extra = _q(f"""SELECT {_NEWS_COLS} FROM news_item
                       WHERE ts > now() - make_interval(days => :d) AND ({conds})
                       ORDER BY value DESC NULLS LAST, ts DESC LIMIT 8""",
                   {f"w{i}": f"%{w}%" for i, w in enumerate(words)} | {"d": days})
        have = {r["url"] for r in rows}
        rows += [r for r in extra if r["url"] not in have]
    return rows[:10]


def _digest_news(days: int, url: str | None = None, query: str | None = None) -> list[dict]:
    rows = _q("""SELECT digest_date, payload FROM daily_digest
                  WHERE section = 'news' AND digest_date >= current_date - :d
                  ORDER BY digest_date DESC""", {"d": days})
    words = _words(query or "")
    seen, res = set(), []
    for r in rows:
        p = r.get("payload") or {}
        items = list(p.get("pool") or [])
        for g in p.get("groups") or []:
            items += g.get("items") or []
        for it in items:
            u = (it.get("url") or "").split("?")[0].rstrip("/")
            if not u or u in seen or (url and u != url):
                continue
            if query:
                hay = f"{it.get('title') or ''} {it.get('snippet') or it.get('summary') or ''}"
                if len(words & _words(hay)) < min(2, len(words)):
                    continue
            seen.add(u)
            res.append({"brief_date": r["digest_date"], "ts": it.get("ts"),
                        "title": it.get("title"),
                        "summary": it.get("summary") or it.get("snippet"),
                        "what_to_check": it.get("idea") or it.get("why"),
                        "source": it.get("domain") or it.get("source"), "url": it.get("url")})
    return res[:10]


def tool_web_search(query: str, sites: list[str] | None = None,
                    fresh_days: int | None = None) -> str:
    from ..rag.web_search import search
    res = search(query, max_results=8, site_filter=[s for s in sites or [] if s] or None,
                 fresh_hours=int(fresh_days) * 24 if fresh_days else None, caller="hermes")
    return out({"query": query, "sites": sites,
                "results": [{"title": r.get("title"), "url": r.get("url"),
                             "date": r.get("date"), "snippet": clip(r.get("snippet"), 400)}
                            for r in res or []]})


def tool_read_page(url: str, query: str | None = None) -> str:
    from ..research.v2.passive_indexer import index_and_get_text
    from ..research.v2.tools.web_tools import _looks_like_stub
    u = (url or "").strip()
    if not re.match(r"^https?://", u):
        return out({"error": "нужна полная ссылка http(s)"})
    try:
        idx = index_and_get_text(u, query_hint=query or "", budget=9000)
    except Exception as e:  # noqa: BLE001
        return out({"url": u, "error": f"не загрузилась: {clip(str(e), 160)}"})
    status, text_, title = idx.get("status"), idx.get("text") or "", idx.get("title") or ""
    if isinstance(status, int) and status >= 400:
        return out({"url": u, "error": f"HTTP {status}"})
    if not text_ or _looks_like_stub(title, text_):
        return out({"url": u, "error": "страница не отдала содержимое "
                    "(заглушка антибота или пустая)", "reason": idx.get("skipped_reason")})
    return out({"url": u, "title": title, "text": text_[:9000],
                "files": [{"anchor": x.get("anchor"), "url": x.get("url")}
                          for x in (idx.get("file_links") or [])[:8]]})


# Слова, которые есть почти в каждой записи раздела (или задаются фильтром банка),
# только размывают поиск: «лазейки по кредиткам Сбера» ≈ «кредитные карты».
_LH_NOISE = re.compile(r"\b(лазейк\w*|уязвим\w*|схем\w*|сбер\w*|банк\w*)\b", re.I)


def tool_loopholes(query: str, bank: str = SBER, days: int | None = None,
                   limit: int = 12) -> str:
    """Раздел «Уязвимости»: записи, признанные лазейками, штатным поиском модуля."""
    from ..loophole import repository as lr
    limit = max(1, min(int(limit or 12), 30))
    q = re.sub(r"\s+", " ", _LH_NOISE.sub(" ", query or "")).strip() or (query or "")
    slug = _bank_slug(bank) if bank else None
    word = (bank or "").lower().replace("банк", "").strip()[:5]
    found: dict[int, dict] = {}
    # Период поиску модуля не передаём: он режет по дате публикации исходной
    # статьи, а схема 2020 года может работать и сейчас. Период — ниже, по
    # дате, когда запись нашла система. Банк у многих записей не проставлен
    # (виден только в тексте) — ищем и с фильтром банка, и по тексту.
    if slug:
        for r in lr.search_relevant(q, bank_slugs=[slug], only_loophole=True, limit=limit * 2):
            found.setdefault(r["record_id"], r)
    for r in lr.search_relevant(f"{q} {bank or ''}".strip(), only_loophole=True,
                                limit=limit * 3):
        found.setdefault(r["record_id"], r)
    ids = list(found)
    # суть, тип, сомнение модели и решение эксперта — без них модель подавала оценку
    # модели как «зафиксированные схемы» (аудит 03.10, УЯЗ-05)
    extra = {x["record_id"]: x for x in _q(
        """SELECT record_id, left(raw_text, 900) AS text, status, published_at, collected_at,
                  headline, left(summary, 600) AS summary, summary_doubt, classification,
                  COALESCE(verdict_model, '') = 'manual' AS expert_checked
             FROM loophole_record WHERE record_id = ANY(:i)""", {"i": ids})} if ids else {}

    def about(r) -> bool:
        if slug and r.get("bank_slug") == slug:
            return True
        hay = f"{r.get('title') or ''} {r.get('snippet') or ''} {r.get('verdict_reason') or ''}"
        return bool(word) and word in hay.lower()

    since = _dt.datetime.now(MSK) - _dt.timedelta(days=int(days)) if days else None

    def in_period(r) -> bool:
        c = (extra.get(r["record_id"]) or {}).get("collected_at")
        return since is None or (c is not None and c >= since)

    def doubt(r) -> bool:
        return bool(((extra.get(r["record_id"]) or {}).get("summary_doubt") or "").strip())

    # сомнительные (модель сама засомневалась) — в конце выдачи
    rows = sorted(found.values(), key=lambda r: (not in_period(r), doubt(r), not about(r),
                                                 -(r.get("relevance") or 0)))
    older = [r for r in rows if not in_period(r)]
    rows = [r for r in rows if in_period(r)][:limit] or rows[:limit]
    # Имена показателей говорят, чей это счётчик: модель путала «все банки за 30
    # дней» со «Сбер» (217 «по Сберу» вместо 90).
    stats = _q("""SELECT min(collected_at) AS collected_since,
                         count(*) FILTER (WHERE is_loophole) AS all_banks_total,
                         count(*) FILTER (WHERE is_loophole
                                          AND collected_at > now() - interval '30 days')
                             AS all_banks_found_last_30d,
                         count(*) FILTER (WHERE is_loophole AND bank_slug = :s)
                             AS this_bank_tagged_total,
                         count(*) FILTER (WHERE is_loophole AND bank_slug = :s
                                          AND collected_at > now() - interval '30 days')
                             AS this_bank_tagged_last_30d,
                         count(*) FILTER (WHERE is_loophole AND status = 'preliminary')
                             AS all_banks_preliminary
                    FROM loophole_record""", {"s": slug or ""})[0]
    return out({
        "section": "Раздел «Аудит уязвимостей»: схемы обхода условий продуктов, найденные в интернете и "
                   "отзывах и признанные моделью лазейками",
        "query": query, "searched_for": q, "bank": bank, "days": days,
        "stats": stats,
        "stats_meaning": "all_banks_* — все банки; this_bank_* — записи с меткой банка "
                         "из поля bank (у многих банк виден только в тексте, их больше); "
                         "*_total — за всё время сбора (с collected_since); *_last_30d — "
                         "найдено системой за 30 дней",
        "status_meaning": "expert_checked=false — оценка модели, экспертом ЦК КС не проверена; "
                          "type — тип по оценке модели; model_doubt — модель сама сомневается",
        "how_to_cite": "Непроверенную запись называй «возможная уязвимость (оценка модели, экспертом "
                       "не проверена)», а не «зафиксированная схема». Запись с model_doubt — только "
                       "с этой оговоркой или не используй. «Схема мошенничества» — только при "
                       "type = мошенническая схема.",
        "older_than_period": len(older) if days else None,
        "records": [{"record_id": r["record_id"], "about_bank": about(r),
                     "title": clip(r.get("title"), 200),
                     "headline": (extra.get(r["record_id"]) or {}).get("headline"),
                     "summary": (extra.get(r["record_id"]) or {}).get("summary"),
                     "type": _LH_TYPE.get((extra.get(r["record_id"]) or {}).get("classification") or "",
                                          "без типа"),
                     "model_doubt": (extra.get(r["record_id"]) or {}).get("summary_doubt") or None,
                     "expert_checked": bool((extra.get(r["record_id"]) or {}).get("expert_checked")),
                     "link": f"#loophole?record={r['record_id']}",
                     "why_loophole": r.get("verdict_reason"),
                     "confidence": r.get("verdict_confidence"),
                     "found_by": r.get("via"),
                     "text": clip((extra.get(r["record_id"]) or {}).get("text")
                                  or r.get("snippet"), 900),
                     "source": r.get("domain"), "url": r.get("url"),
                     "published": (extra.get(r["record_id"]) or {}).get("published_at"),
                     "found": (extra.get(r["record_id"]) or {}).get("collected_at"),
                     "status": (extra.get(r["record_id"]) or {}).get("status"),
                     "bank_tag": r.get("bank_slug")} for r in rows],
        "link": "#loophole"})


_LH_TYPE = {"vulnerability": "уязвимость", "fraud_scheme": "мошенническая схема",
            "not_confirmed": "не подтверждено"}


# Старая разметка жалоб: темы регулярками (review_topic, v_review_topics) и
# тональность по словарю (review_sentiment, v_review_sentiment_share) — по небольшой
# таблице review, сводка поверх них (review_summary) и векторная таксономия
# (review_topic_def/_label/_state), которую в сентябре 2026 сменила LLM-разметка.
# Числа из них не сходятся с «Аудитом отзывов», и модель приносила «топ тем»,
# которых во вкладке нет (04.11.2026). Жалобы — только инструментами жалоб.
STALE_REVIEW_RELATIONS = re.compile(
    r"(?<![\w$])(review_topic(?:_def|_label|_state)?|v_review_topics|review_sentiment"
    r"|v_review_sentiment_share|review_summary)(?![\w$])", re.I)


def tool_sql(query: str) -> str:
    """SELECT к основной базе в транзакции только для чтения."""
    sql = (query or "").strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", sql, re.I) or ";" in sql:
        return out({"error": "один запрос SELECT (или WITH … SELECT); запись — через alsql"})
    stale = sorted({m.group(1).lower() for m in STALE_REVIEW_RELATIONS.finditer(sql)})
    if stale:
        return out({"error": f"{', '.join(stale)} — старая разметка жалоб, её числа не совпадают "
                             "с разделом «Аудит отзывов». Жалобы, темы и признаки — инструментами "
                             "complaints_overview, complaint_stories, complaint_theme, "
                             "complaint_search, complaint_signals.",
                    "counting": _gl.COMPLAINT_SHORT})
    from sqlalchemy import text
    from .. import db
    with db.session() as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        s.execute(text("SET LOCAL statement_timeout = '15s'"))
        try:
            rows = [dict(r) for r in s.execute(text(f"SELECT * FROM ({sql}) AS _q LIMIT 200"))
                    .mappings().all()]
        except Exception as e:  # noqa: BLE001
            s.rollback()
            return out({"error": clip(str(getattr(e, "orig", e)), 500)})
    return out({"rows": len(rows), "truncated_at_200": len(rows) >= 200,
                "data": [{k: (clip(v, 800) if isinstance(v, str) else v) for k, v in r.items()}
                         for r in rows]})


# ── реестр ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str            # подпись шага в интерфейсе
    fn: Callable[..., str]
    description: str


TOOLS: list[ToolSpec] = [
    ToolSpec("day_brief", "Выпуск дня", tool_day_brief,
             "Выпуск раздела «Новостные обзоры» на сегодня: заголовок дня, поводы «что проверить» с объяснением "
             "и доказательствами, сводка жалоб недели, заметка о рынке, дополнение дня."),
    ToolSpec("complaint_signals", "Сигналы жалоб", tool_complaint_signals,
             "Всплески жалоб за последнюю неделю, как в разделе «Аудит отзывов»: тема, число за неделю, норма, "
             "кратность к норме, то же у рынка, главный город и сами жалобы всплеска "
             "(пересказ, цитата, ссылка); плюс темы, растущие быстрее рынка ниже порога "
             "значимости. Полный разбор темы с текстами — complaint_theme."),
    ToolSpec("complaint_theme", "Разбор темы жалоб", tool_complaint_theme,
             "Одна тема жалоб целиком: недельный сигнал, total — сколько всего жалоб, где тема "
             "главная, shown — сколько из них в ответе, сами жалобы (дата, город, продукт, "
             "пересказ, цитата, эскалация, начало текста, ссылка) и группы похожих жалоб. "
             "days=7 — ровно выборка недельного сигнала; date_from/date_to — период «с — по», "
             "как во вкладке. Число жалоб темы — total, не длина списка. Темы: "
             + THEMES_HELP),
    ToolSpec("complaint_stories", "Сюжеты жалоб", tool_complaint_stories,
             "«Сюжеты» раздела «Аудит отзывов»: устойчивые группы похожих историй клиентов "
             "банка (или продукта, среза) за период — название и суть сюжета, сколько жалоб "
             "в периоде и за всё время, когда появился, значки «новый сюжет» / «вырос с A до "
             "B» / «вернулся», вырос ли за неделю сверх своей нормы и есть ли та же история "
             "у других банков, две жалобы-примера. С этого начинай ответ на «на что "
             "жалуются», «какие истории повторяются», «что нового в жалобах»; числа по "
             "темам — complaints_overview."),
    ToolSpec("complaints_overview", "Сводка жалоб", tool_complaints_overview,
             "Жалобы на банк (или продукт) за период — те же числа, что в разделе «Аудит "
             "отзывов»: число и изменение, доля и место среди банков, эскалация против рынка, "
             "темы с изменением, что изменилось, признаки риска против рынка, города, "
             "площадки. Период — days или date_from/date_to («с — по»)."),
    ToolSpec("complaint_search", "Поиск жалоб", tool_complaint_search,
             "Поиск жалоб по словам и смыслу (сервис, схема, формулировка, событие) с "
             "фильтрами банка, продукта, темы и периода (days или date_from/date_to). "
             "by_words — сколько всего жалоб со словами запроса (полный счёт: «нашлось N» — "
             "это он); by_sense — ближайшие по смыслу жалобы без этих слов, их число "
             "ограничено (примеры, не счёт); total — длина списка во вкладке, by_words + "
             "by_sense. Точные формулировки — в поле glossary ответа."),
    ToolSpec("market_position", "Позиция на рынке", tool_market_position,
             "Место Сбера среди банков по категориям продуктов, как в «Рынке»: ранг, метрика "
             "категории, значение и продукт Сбера, разрыв с медианой и лидером, сегменты, "
             "доступность ставки, оговорки методики. Категории: " + CATEGORIES_HELP),
    ToolSpec("market_offers", "Предложения банков", tool_market_offers,
             "Предложения банков в категории из витрины «Рынок», отсортированные по метрике "
             "категории: сравнение Сбера с названными банками (banks) или топ рынка; "
             "query — название или вид продукта. Дата данных — as_of; terms_since у "
             "предложения — с какого дня действуют его условия; place в топе рынка — "
             "готовое место предложения с учётом ничьих."),
    ToolSpec("bank_profile", "Карточка банка", tool_bank_profile,
             "Карточка банка: народный рейтинг banki.ru (баллы, место, средняя оценка, "
             "отзывы, доля решённых), наш корпус отзывов, жалобы за 90 дней и их темы."),
    ToolSpec("knowledge_search", "База знаний", tool_knowledge_search,
             "Поиск в базе знаний AuditLens: тарифы и условия банков (PDF и страницы), "
             "документы ЦБ, законы, методички. Отдаёт документы с фрагментами вокруг "
             "совпадений."),
    ToolSpec("knowledge_read", "Чтение документа", tool_knowledge_read,
             "Читать документ базы знаний по document_id: места по словам query или начало."),
    ToolSpec("news_find", "Новости", tool_news_find,
             "Новости из ленты AuditLens (ЦБ, СМИ, Telegram-каналы) по ссылке или по теме: "
             "текст, пересказ, что затрагивает у Сбера, что проверить и запросить, срок; "
             "плюс совпадения из выпусков раздела «Новостные обзоры»."),
    ToolSpec("web_search", "Веб-поиск", tool_web_search,
             "Поиск в интернете (Яндекс через корпоративный шлюз); sites — ограничить "
             "сайтами, fresh_days — только свежее."),
    ToolSpec("read_page", "Чтение страницы", tool_read_page,
             "Текст страницы или PDF по ссылке (с отбором мест по query) и ссылки на файлы."),
    ToolSpec("loopholes", "Аудит уязвимостей", tool_loopholes,
             "Раздел «Аудит уязвимостей»: лазейки — схемы, которыми клиенты, партнёры или "
             "мошенники обходят условия продуктов банка (продление грейса, обход лимитов и "
             "комиссий, двойные бонусы, вывод кредитных средств). Поиск по словам и смыслу "
             "(query: продукт или механика, например «кредитная карта», «грейс», «снятие "
             "наличных»); days — только если в вопросе задан период (схема старой "
             "публикации может работать и сейчас). Статистика раздела, записи с причиной, "
             "текстом, источником, датами и статусом проверки."),
    ToolSpec("sql", "SQL к базе", tool_sql,
             "SELECT к основной базе AuditLens (транзакция только для чтения, до 200 строк). "
             "Схема и ловушки — в навыке auditlens-data."),
]

BY_NAME = {t.name: t for t in TOOLS}


def label_for(tool_name: str) -> str:
    """Подпись шага для интерфейса (Hermes зовёт MCP-инструменты mcp__auditlens__<имя>)."""
    base = re.sub(r"^mcp_+auditlens_+", "", tool_name or "")
    t = BY_NAME.get(base)
    return t.label if t else tool_name
