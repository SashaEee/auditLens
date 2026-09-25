"""Рабочее место аудитора во вкладке «Отзывы» (волна 4).

Здесь то, что нужно аудитору после того, как вкладка показала проблему:

  • группы похожих жалоб в ленте — по векторам изложений из разметки;
  • жалобы по списку ссылок — для аудит-дела и снимков журнала сигналов;
  • журнал сигналов — эпизод всплеска со снимком чисел и жалоб и отметкой
    «подтвердился / ложный», точность сигналов для «Пульса»;
  • подписки на сигналы по банку и продукту — для «Для вас»;
  • связка с «Рынком» — изменения условий банка на графике жалоб продукта.

Числа считает код; модели здесь нет вовсе.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import text

from .. import db
from . import review_codebook as cb
from . import reviews_dash as rd

log = logging.getLogger(__name__)


# ── Векторы изложений ───────────────────────────────────────────────────────
# Изложение из разметки («Банк повысил ставку по льготной ипотеке из-за
# подписки») группирует жалобы точнее полного текста: в тексте много
# постороннего, и тексты одного банка все похожи друг на друга. Замер на
# жалобах Сбера: одна история — сходство 0,86–0,89, соседние — ниже 0,81.

def embed_summaries(limit: int = 3000, days: int = 400, budget_s: float = 240.0,
                    bank: str | None = None) -> dict:
    """Досчитать векторы изложений жалоб, у которых их нет, — свежие первыми.
    Идёт фоном после разметки; лента берёт готовые. bank — первичный досчёт
    по одному банку."""
    from . import embedder
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT a.url, a.summary FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE {rd._CMP} AND i.dt > now() - make_interval(days => :d) AND i.dt <= now()
              AND (CAST(:bank AS text) IS NULL OR i.bank = :bank)
              AND coalesce(a.summary, '') <> ''
              AND NOT EXISTS (SELECT 1 FROM review_summary_vec v WHERE v.url = i.url)
            ORDER BY i.dt DESC LIMIT :lim"""),
            {"sv": rd._ann_schema(), "d": days, "lim": limit, "bank": bank}).all()
    t0, done = time.time(), 0
    for j in range(0, len(rows), 64):
        if time.time() - t0 > budget_s:
            break
        chunk = rows[j:j + 64]
        vecs = embedder.embed_batch([r[1][:400] for r in chunk])
        _store_vecs({r[0]: v for r, v in zip(chunk, vecs)})
        done += len(chunk)
    return {"todo": len(rows), "done": done, "seconds": round(time.time() - t0, 1)}


def _store_vecs(vecs: dict[str, list[float]]) -> None:
    if not vecs:
        return
    with db.session() as s:
        s.execute(text("""INSERT INTO review_summary_vec (url, vec)
                          VALUES (:u, CAST(:v AS real[])) ON CONFLICT (url) DO NOTHING"""),
                  [{"u": u, "v": [float(x) for x in v]} for u, v in vecs.items()])


def _vecs_for(urls: list[str]) -> dict[str, list[float]]:
    if not urls:
        return {}
    with db.session() as s:
        return {r[0]: r[1] for r in s.execute(text(
            "SELECT url, vec FROM review_summary_vec WHERE url = ANY(:u)"), {"u": urls}).all()}


# ── Группы похожих жалоб ────────────────────────────────────────────────────
_CL_SIM = 0.84        # сходство изложений для одной группы (та же главная проблема)
_CL_SIM_X = 0.88      # для разных главных проблем — строже
_CL_MIN = 3
_CL_ON_FLY = 160      # сколько векторов досчитать прямо в запросе (≈4 с)


def clusters(bank: str, product: str | None = None, days: int = 90,
             theme: str | None = None, flag: str | None = None, city: str | None = None,
             source: str | None = None, esc: bool = False, limit: int = 2500) -> dict | None:
    """Похожие жалобы группами: число, главная проблема, пример и ссылки.

    Берутся последние `limit` жалоб с фильтрами ленты. Группа — жалобы, чьи
    изложения близки к изложению «ядра» группы (жадно, от самых плотных); от
    трёх жалоб. Остальные — «единичные»: они не хуже, просто не повторяются."""
    bc = rd.resolve_bank(bank)
    if not bc:
        return None
    key = f"cl:{bc}:{product}:{days}:{theme}:{flag}:{city}:{source}:{esc}:{limit}"
    return rd._cached(key, lambda: _clusters(bc, product, days, theme, flag, city, source,
                                             esc, limit), ttl=1800)


def _clusters(bc, product, days, theme, flag, city, source, esc, limit) -> dict:
    import numpy as np
    p: dict = {"bank": bc, "product": product, "d": days, "lim": limit, "sv": rd._ann_schema()}
    extra = rd._source_clause("i", source, p) or ""
    fcond = rd._flag_sql(flag)
    if fcond:
        extra += f" AND {fcond}"
    if theme:
        extra += " AND i.issue = :t"
        p["t"] = theme
    if city:
        extra += " AND i.city = :city"
        p["city"] = city
    if esc:
        extra += " AND i.esc"
    with db.session() as s:
        rows = [dict(r) for r in s.execute(text(f"""
            SELECT i.url, i.dt, i.city, i.issue, a.summary, a.quote, a.quote_ok, a.esc,
                   cardinality(a.vulnerable) > 0 AS vuln
            FROM review_index i
            JOIN review_annotation a ON a.url = i.url AND a.schema_version = :sv
            WHERE i.bank = :bank AND {rd._CMP}
              AND (CAST(:product AS text) IS NULL OR i.product = :product)
              AND i.dt >= now() - make_interval(days => :d) AND i.dt <= now()
              AND coalesce(a.summary, '') <> ''{extra}
            ORDER BY i.dt DESC LIMIT :lim"""), p).mappings().all()]
    vecs = _vecs_for([r["url"] for r in rows])
    missing = [r for r in rows if r["url"] not in vecs][:_CL_ON_FLY]
    if missing:
        try:
            from . import embedder
            new = embedder.embed_batch([r["summary"][:400] for r in missing])
            fresh = {r["url"]: v for r, v in zip(missing, new)}
            _store_vecs(fresh)
            vecs.update(fresh)
        except Exception as e:  # noqa: BLE001 — без векторов группы по тем, что есть
            log.info("clusters: векторы не досчитались (%s)", e)
    items = [r for r in rows if r["url"] in vecs]
    out = {"total": len(rows), "no_vec": len(rows) - len(items), "clusters": [],
           "clustered": 0, "days": days}
    if len(items) < _CL_MIN:
        return out
    m = np.asarray([vecs[r["url"]] for r in items], dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
    sim = m @ m.T
    codes = {c: k for k, c in enumerate({r["issue"] for r in items})}
    iss = np.asarray([codes[r["issue"]] for r in items])
    same = iss[:, None] == iss[None, :]
    near = (sim >= np.where(same, _CL_SIM, _CL_SIM_X))
    np.fill_diagonal(near, False)
    taken = np.zeros(len(items), dtype=bool)
    groups = []
    for i in np.argsort(-near.sum(axis=1)):
        if taken[i]:
            continue
        members = [i] + [j for j in np.flatnonzero(near[i]) if not taken[j]]
        if len(members) < _CL_MIN:
            continue
        taken[members] = True
        groups.append(members)
    for g in groups:
        its = [items[k] for k in g]
        lead = its[0]
        issues: dict[str, int] = {}
        cities: dict[str, int] = {}
        for x in its:
            issues[x["issue"]] = issues.get(x["issue"], 0) + 1
            if x["city"]:
                cities[x["city"]] = cities.get(x["city"], 0) + 1
        top_issue = max(issues, key=issues.get)
        o = cb.issue_obj(top_issue) or {}
        quote = next((x["quote"] for x in [lead] + its if x["quote_ok"] and x["quote"]), None)
        dts = sorted(x["dt"] for x in its if x["dt"])
        out["clusters"].append({
            "n": len(its), "issue": top_issue, "label": o.get("label"), "short": o.get("short"),
            "risk": o.get("risk"), "summary": lead["summary"], "quote": quote,
            "first": dts[0].date().isoformat() if dts else None,
            "last": dts[-1].date().isoformat() if dts else None,
            "cities": [c for c, _n in sorted(cities.items(), key=lambda kv: -kv[1])[:2]],
            "esc": sum(1 for x in its if x["esc"] in ("threat", "filed")),
            "vuln": sum(1 for x in its if x["vuln"]),
            "urls": [x["url"] for x in its],
        })
    out["clusters"].sort(key=lambda c: -c["n"])
    out["clustered"] = sum(c["n"] for c in out["clusters"])
    return out


# ── Жалобы по списку ссылок ─────────────────────────────────────────────────

def reviews_by_urls(urls: list[str]) -> list[dict]:
    """Карточки жалоб в заданном порядке — для группы, дела и снимка сигнала.
    Тот же вид, что в ленте: разметка, ответ банка, площадка."""
    urls = [u for u in dict.fromkeys(urls or []) if u][:200]
    if not urls:
        return []
    with db.session() as s:
        rows = [dict(r) for r in s.execute(text("""
            SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt, i.city, i.rating
            FROM review_index i WHERE i.url = ANY(:u)"""), {"u": urls}).mappings().all()]
    from . import bankiru_fts
    bodies = bankiru_fts.bodies_for(rows)
    by = {}
    for r in rows:
        b = bodies.get(r["url"]) or {}
        by[r["url"]] = {"bank": r["bank"], "product": r["product"],
                        "date": r["dt"].date().isoformat() if r["dt"] else None,
                        "city": r["city"] or b.get("city"), "url": r["url"],
                        "text": (b.get("text") or "").strip(), "similar": 0,
                        "rating": float(r["rating"]) if r["rating"] is not None else None,
                        "source": r["source"], "themes": []}
    items = [by[u] for u in urls if u in by]
    rd._attach_themes(items)
    return items


# ── Журнал сигналов ─────────────────────────────────────────────────────────
# Сигнал недели живёт несколько дней подряд: окно скользит, и один всплеск
# виден с понедельника по пятницу. Журнал хранит ЭПИЗОД — запись обновляется,
# пока сигнал держится (перерыв до 8 дней), снимок жалоб берётся на пике.

_JOURNAL_GAP_DAYS = 8
_REC_LOCK = threading.Lock()
_REC_AT: dict[str, float] = {}


def record_signals(sig: dict | None, bank: str, product: str | None = None,
                   min_interval_s: float = 3600.0) -> int:
    """Записать сигналы недели в журнал. Вызывается там, где сигналы
    показываются (радар вкладки, «Обзор»); не чаще раза в час на срез."""
    signals = (sig or {}).get("signals") or []
    if not signals:
        return 0
    key = f"{bank}|{product or ''}"
    with _REC_LOCK:
        if time.time() - _REC_AT.get(key, 0) < min_interval_s:
            return 0
        _REC_AT[key] = time.time()
    import json
    n = 0
    for s_ in signals:
        stats = {k: s_.get(k) for k in ("week", "baseline_week", "ratio", "excess", "q_value",
                                        "market_ratio", "bank_specific", "new", "accel",
                                        "prev_week", "week_total")}
        with db.session() as s:
            cur = s.execute(text("""
                SELECT signal_id, stats FROM signal_journal
                WHERE bank = :b AND product = :p AND issue = :k
                  AND last_seen > now() - make_interval(days => :gap)
                ORDER BY last_seen DESC LIMIT 1"""),
                {"b": bank, "p": product or "", "k": s_["key"],
                 "gap": _JOURNAL_GAP_DAYS}).first()
        peak = not cur or int(s_.get("week") or 0) >= int((cur[1] or {}).get("week") or 0)
        urls = ([e["url"] for e in rd.signal_evidence(bank, s_["key"], product, limit=60)]
                if peak else None)
        p = {"b": bank, "p": product or "", "k": s_["key"], "lvl": s_.get("level"),
             "st": json.dumps(stats, ensure_ascii=False), "u": urls or [],
             "we": sig.get("week_end")}
        with db.session() as s:
            if not cur:
                s.execute(text("""
                    INSERT INTO signal_journal (bank, product, issue, week_end, level, stats, urls)
                    VALUES (:b, :p, :k, CAST(:we AS date), :lvl, CAST(:st AS jsonb), :u)"""), p)
            elif peak:
                s.execute(text("""
                    UPDATE signal_journal SET last_seen = now(), week_end = CAST(:we AS date),
                           level = :lvl, stats = CAST(:st AS jsonb), urls = :u
                    WHERE signal_id = :id"""), {**p, "id": cur[0]})
            else:
                s.execute(text("UPDATE signal_journal SET last_seen = now() WHERE signal_id = :id"),
                          {"id": cur[0]})
        n += 1
    return n


def _precision(rows: list[dict]) -> dict:
    conf = sum(1 for r in rows if r["verdict"] == "confirmed")
    false = sum(1 for r in rows if r["verdict"] == "false")
    return {"episodes": len(rows), "rated": conf + false, "confirmed": conf, "false": false,
            "precision": round(100 * conf / (conf + false)) if conf + false else None}


def journal(bank: str, product: str | None = None, days: int = 180) -> dict | None:
    """Эпизоды сигналов по срезу за период — свежие первыми, с точностью."""
    bc = rd.resolve_bank(bank)
    if not bc:
        return None
    with db.session() as s:
        rows = [dict(r) for r in s.execute(text("""
            SELECT signal_id, issue, first_seen, last_seen, week_end, level, stats,
                   cardinality(urls) AS n_urls, verdict, verdict_by, verdict_at, verdict_note
            FROM signal_journal
            WHERE bank = :b AND product = :p AND last_seen > now() - make_interval(days => :d)
            ORDER BY last_seen DESC"""), {"b": bc, "p": product or "", "d": days}).mappings().all()]
    for r in rows:
        o = cb.issue_obj(r["issue"]) or {}
        r.update({"label": o.get("label") or r["issue"], "short": o.get("short"),
                  "risk": o.get("risk")})
        for k in ("first_seen", "last_seen", "verdict_at"):
            r[k] = r[k].isoformat() if r[k] else None
        r["week_end"] = r["week_end"].isoformat() if r["week_end"] else None
    return {"bank": bc, "product": product, "days": days, **_precision(rows), "items": rows}


def journal_urls(signal_id: int) -> list[str]:
    with db.session() as s:
        return list(s.execute(text("SELECT urls FROM signal_journal WHERE signal_id = :i"),
                              {"i": signal_id}).scalar() or [])


def set_verdict(signal_id: int, verdict: str | None, username: str,
                note: str | None = None) -> bool:
    """Отметка аудитора. None — снять отметку."""
    if verdict not in ("confirmed", "false", None):
        return False
    with db.session() as s:
        r = s.execute(text("""
            UPDATE signal_journal SET verdict = :v, verdict_by = :u,
                   verdict_at = CASE WHEN CAST(:v AS text) IS NULL THEN NULL ELSE now() END,
                   verdict_note = :n
            WHERE signal_id = :i"""),
            {"v": verdict, "u": username if verdict else None,
             "n": (note or "").strip()[:500] or None, "i": signal_id})
        return bool(r.rowcount)


def journal_stats(days: int = 180) -> dict:
    """Точность сигналов для «Пульса»: сколько эпизодов, сколько отмечено,
    доля подтвердившихся — всего и по банкам."""
    with db.session() as s:
        rows = [dict(r) for r in s.execute(text("""
            SELECT bank, verdict, last_seen FROM signal_journal
            WHERE last_seen > now() - make_interval(days => :d)"""), {"d": days}).mappings().all()]
    banks: dict[str, list[dict]] = {}
    for r in rows:
        banks.setdefault(r["bank"], []).append(r)
    by_bank = sorted(({"bank": b, **_precision(v)} for b, v in banks.items()),
                     key=lambda x: -x["episodes"])
    return {"days": days, **_precision(rows), "by_bank": by_bank[:8]}


# ── Подписки ────────────────────────────────────────────────────────────────

def subs_add(username: str, bank: str, product: str | None) -> bool:
    bc = rd.resolve_bank(bank)
    if not bc or not username:
        return False
    with db.session() as s:
        s.execute(text("""
            INSERT INTO review_subscription (username, bank, product) VALUES (:u, :b, :p)
            ON CONFLICT DO NOTHING"""), {"u": username, "b": bc, "p": (product or "")[:120]})
    return True


def subs_del(username: str, bank: str, product: str | None) -> None:
    with db.session() as s:
        s.execute(text("""DELETE FROM review_subscription
                          WHERE username = :u AND bank = :b AND product = :p"""),
                  {"u": username, "b": rd.resolve_bank(bank) or bank, "p": product or ""})


def subs_status(username: str) -> list[dict]:
    """Подписки пользователя с текущим состоянием: сигналы недели (порог
    пробит) и проблемы, растущие быстрее рынка (ниже порога) — как в радаре."""
    with db.session() as s:
        subs = s.execute(text("""SELECT bank, product, created_at FROM review_subscription
                                 WHERE username = :u ORDER BY created_at"""),
                         {"u": username}).all()
    out = []
    for bank, product, created in subs:
        sig = rd.weekly_signals(bank, product or None) or {}
        signals = [{k: x.get(k) for k in ("key", "label", "short", "risk", "week",
                                          "baseline_week", "ratio", "level", "new", "accel")}
                   for x in (sig.get("signals") or [])[:3]]
        watch = []
        if not signals:
            wp = rd.week_pulse(bank, product or None) or {}
            watch = [{k: d.get(k) for k in ("key", "label", "short", "week", "gap")}
                     for d in (wp.get("diverge") or []) if (d.get("gap") or 0) >= 1.15][:2]
        out.append({"bank": bank, "product": product or None,
                    "since": created.isoformat() if created else None,
                    "signals": signals, "watch": watch, "week_end": sig.get("week_end")})
    return out


def is_subscribed(username: str, bank: str, product: str | None) -> bool:
    with db.session() as s:
        return bool(s.execute(text("""SELECT 1 FROM review_subscription
                                      WHERE username = :u AND bank = :b AND product = :p"""),
                              {"u": username, "b": rd.resolve_bank(bank) or bank,
                               "p": product or ""}).scalar())


# ── Связка с «Рынком» ───────────────────────────────────────────────────────
# Продукт разметки → категория тарифного трекера. Продукты без тарифов на
# агрегаторе (переводы, страхование, подписки) не связываются.
_PRODUCT_CATEGORY = {"Дебетовая карта": "card_debit", "Кредитная карта": "card_credit",
                     "Потребительский кредит": "credit", "Ипотека": "mortgage",
                     "Автокредит": "auto_loan", "Накопительный счёт": "savings_account",
                     "Вклад": "deposit", "Бизнес: счёт и РКО": "rko"}
# Банк в корпусе отзывов → код банка трекера, где названия расходятся
_BANK_SLUG = {"ВТБ": "vtb", "Альфа-Банк": "alfabank", "Газпромбанк": "gazprombank",
              "ПСБ": "psb", "Почта Банк": "pochtabank", "Московский кредитный банк (МКБ)": "mkb",
              "Совкомбанк": "sovcombank", "Россельхозбанк": "rshb", "Т-Банк": "tinkoff",
              "МТС Банк": "mtsbank", "Райффайзен Банк": "raiffeisen"}


def market_events(bank: str, product: str | None, months: int = 14) -> dict:
    """Изменения условий банка по продукту — помесячно, для меток на графике
    жалоб. Те же правила, что в журнале «Рынка»: смена выдачи агрегатора и
    микрошум ставки изменением не считаются."""
    from ..normalizer.offers import CTX_JOIN_SQL, SAME_CTX_SQL, SIGNIFICANT_CHANGE_SQL
    bc = rd.resolve_bank(bank) or bank
    cat = _PRODUCT_CATEGORY.get(product or "")
    out: dict = {"category": cat, "months": {}, "since": None}
    if not cat:
        return out

    def _compute():
        if bc == "Сбербанк":
            bcond, p = "b.is_sber", {}
        elif bc in _BANK_SLUG:
            bcond, p = "b.slug = :slug", {"slug": _BANK_SLUG[bc]}
        else:
            bcond, p = "lower(b.name) = lower(:bn)", {"bn": bc}
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT ch.changed_at, ch.diff, o.title, o.url, o.offer_id
                FROM change_history ch
                JOIN product_offer o USING (offer_id)
                JOIN bank b USING (bank_id)
                {CTX_JOIN_SQL}
                WHERE {bcond} AND o.category = :cat
                  AND ch.changed_at > date_trunc('month', now()) - make_interval(months => :m)
                  AND {SAME_CTX_SQL} AND {SIGNIFICANT_CHANGE_SQL}
                ORDER BY ch.changed_at"""), {**p, "cat": cat, "m": months - 1}).all()
            since = s.execute(text("SELECT min(changed_at)::date FROM change_history")).scalar()
        res: dict = {"category": cat, "months": {},
                     "since": since.isoformat() if since else None}
        for at, diff, title, url, oid in rows:
            ym = at.strftime("%Y-%m")
            res["months"].setdefault(ym, []).append({
                "date": at.date().isoformat(), "title": title, "url": url, "offer_id": oid,
                "diff": {k: {"from": (v or {}).get("from"), "to": (v or {}).get("to")}
                         for k, v in (diff or {}).items()}})
        return res
    return rd._cached(f"mev:{bc}:{cat}:{months}", _compute, ttl=3600)
