"""Срезы «Аудита отзывов», которых нет в справочнике продуктов: зарплатные проекты,
УС (банкоматы и терминалы), выездной сервис.

Срез — не продукт договора, а сюжет: зарплатная карта — это дебетовая карта,
банкомат — канал. Поэтому основная разметка (review_annotate) их не ловит, а
подсказки её каналов неточны: курьер маркетплейса — не выездной сервис банка,
POS-терминал магазина — не УС. Здесь — отдельная LLM-разметка флагов:

1. Кандидаты — полнотекстовый фильтр по индексу (с запасом на полноту) плюс
   подсказки основной разметки (канал «банкомат»/«курьер», проблема cash_atm).
2. Две модели размечают независимо (A и B), расхождения решает арбитр.
3. Итог — review_slice.tags, в индекс — review_index.slices; фильтр продукта
   во вкладке — SQL-функция al_product_is (migrations/099).

Качество замеряется слепой разметкой сильной модели-судьи (scripts/
review_slices_judge.py); прежде чем менять определения — замер до и после.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from sqlalchemy import text

from .. import db
from . import review_annotate as RA

log = logging.getLogger(__name__)

SCHEMA = os.getenv("REVIEW_SLICES_SCHEMA", "s6")   # s6 — payroll решают сильные модели (замер 14.10)
# окно разметки перекрывает самый длинный период вкладки (12 мес.) с запасом
SINCE = os.getenv("REVIEW_SLICES_SINCE", "2025-09-01")
MODEL_A = os.getenv("REVIEW_SLICES_A", RA.MODEL_A)
MODEL_B = os.getenv("REVIEW_SLICES_B", RA.MODEL_B)
MODEL_ARB = os.getenv("REVIEW_SLICES_ARB", RA.MODEL_C2)
BATCH = int(os.getenv("REVIEW_SLICES_BATCH", "10"))
INFLIGHT = int(os.getenv("REVIEW_SLICES_INFLIGHT", "12"))
CAP_RUB = float(os.getenv("REVIEW_SLICES_CAP_RUB", "15000"))
DAILY_CAP_RUB = float(os.getenv("REVIEW_SLICES_DAILY_CAP_RUB", "2000"))    # доразметка за сутки (МСК)
_LOCK_KEY = 710_022_402
_MAX_CHARS = 3000

# ключ: (подпись в списке продуктов «Аудита отзывов», определение для модели)
SLICES: dict[str, tuple[str, str]] = {
    "payroll": ("Зарплатные проекты", (
        "предмет претензии — сам зарплатный проект. ДА, если: (1) условия зарплатной карты или счёта как "
        "зарплатных — тариф и льготы зарплатного клиента, плата за обслуживание после потери статуса "
        "зарплатной (увольнение, смена работодателя, перевод проекта), выпуск зарплатной карты по проекту "
        "работодателя; (2) зачисление зарплаты, аванса, отпускных через банк — не пришли, задержаны, "
        "зачислены частично или с ошибкой; (3) работодатель (юрлицо, ИП) о зарплатном проекте — подключение, "
        "реестры, перечисление зарплаты, аванса, отпускных сотрудникам, тарифы, перевод проекта. "
        "НЕ payroll, если: (а) клиент лишь упоминает, что он зарплатный клиент или что на карту приходит "
        "зарплата, а претензия о другом — блокировка, перевыпуск, переводы, подписки, лимиты, кредит, "
        "обслуживание в офисе; (б) арест, взыскание или списание по исполнительному документу или требованию "
        "налоговой — даже если списана зарплата и нарушен лимит удержаний; (в) зарплата — только доход "
        "(кредит, проверка доходов); (г) пенсии и пособия.")),
    "us": ("УС — банкоматы и терминалы", (
        "устройства самообслуживания банка (УС): банкомат, платёжный терминал, инфокиоск, устройство приёма "
        "наличных — не выдал или не зачислил деньги, удержал карту, сбой операции, устройства нет или не "
        "работают, комиссия или лимит при операции в УС, очередь к банкомату. "
        "НЕ us: кассовые и эквайринговые POS-терминалы магазинов и бизнеса; операции в кассе отделения у "
        "сотрудника.")),
    "field": ("Выездной сервис — курьеры и выезд", (
        "выездной сервис банка: курьерская доставка карт и документов банка, выездной менеджер или "
        "специалист банка (оформление, подписание, открытие счёта), выездное обслуживание на дому или "
        "маломобильных клиентов, выезд инженера банка к бизнес-клиенту. "
        "НЕ field: доставка товаров маркетплейсов, еды и других небанковских сервисов; почта, если банк "
        "ничего не вёз.")),
}
KEYS = tuple(SLICES)
LABELS = {k: v[0] for k, v in SLICES.items()}

# Кандидаты: префиксные лексемы по tsv индекса (берём с запасом — лишнее отсеет модель)
_TSQ = {
    "payroll": "зарплатн:* | зарплат:* | заработн:* | работодател:*",
    "us": "банкомат:* | терминал:* | купюр:* | самообслуживан:* | инкассац:* | инфокиоск:*",
    "field": "курьер:* | выездн:* | выезд:* | доставк:*",
}
_HINT = ("'atm' = ANY(coalesce(i.channels, '{}'))", "i.issue = 'cash_atm'",
         "'courier' = ANY(coalesce(i.channels, '{}'))")


def candidate_sql() -> str:
    """Условие «отзыв — кандидат хотя бы в один срез» (алиас индекса — i)."""
    parts = [f"i.tsv @@ to_tsquery('russian', '{q}')" for q in _TSQ.values()]
    return "(" + " OR ".join(parts + list(_HINT)) + ")"


SYSTEM = (
    "Ты — старший аудитор розничного банка. По каждому отзыву клиента поставь три флага — относится ли "
    "претензия к срезу. Флаг true, только если это суть претензии или её существенная часть, а не "
    "упоминание вскользь. Читай весь текст.\n\nСРЕЗЫ\n"
    + "\n".join(f"{k} — {v[1]}" for k, v in SLICES.items())
    + '\n\nОтвечай ТОЛЬКО JSON без пояснений: {"items":[{"n":1,"payroll":false,"us":false,"field":false,'
      '"quote":""}]} — по объекту на каждый отзыв, поле n обязательно; quote — дословная цитата до 150 '
      "знаков, подтверждающая срез, если хоть один флаг true, иначе пусто.")


def flags(d) -> dict | None:
    """Ответ модели по одному отзыву → {payroll, us, field, quote} или None."""
    if not isinstance(d, dict) or not all(k in d for k in KEYS):
        return None
    out = {k: d[k] is True or str(d[k]).strip().lower() in ("true", "1", "да") for k in KEYS}
    out["quote"] = str(d.get("quote") or "")[:200]
    return out


def merge(a: dict | None, b: dict | None) -> tuple[dict, list[str]]:
    """Итог двух разметчиков и список спорных срезов (их решает арбитр). Если
    один разметчик не ответил — спорны все срезы: одного голоса мало."""
    if a is None or b is None:
        base = a or b or {k: False for k in KEYS}
        return {k: base[k] for k in KEYS}, list(KEYS)
    final = {k: a[k] for k in KEYS if a[k] == b[k]}
    return final, [k for k in KEYS if a[k] != b[k]]


def tags_of(final: dict) -> list[str]:
    return [k for k in KEYS if final.get(k)]


def _card(x: dict, n: int | None = None) -> str:
    head = f"[{n}] " if n else ""
    return f"{head}Банк: {x.get('bank') or '—'}.\nТекст:\n{x['text']}"


async def _tag_batch(c: RA.Caller, model: str, items: list[dict]) -> dict[int, dict]:
    user = ("Поставь флаги срезов каждому отзыву.\n\n"
            + "\n\n".join(_card(x, k + 1) for k, x in enumerate(items)))
    d, tin, tout = await c.ask(model, user)
    arr = [it for it in ((d or {}).get("items") or []) if isinstance(it, dict)] if isinstance(d, dict) else []
    by_n = {int(it["n"]): it for it in arr if str(it.get("n", "")).isdigit()}
    share = RA.Ledger().add(model, tin, tout) / max(1, len(items)) if (tin or tout) else 0.0
    out = {}
    for k in range(len(items)):
        v = flags(by_n.get(k + 1))
        if v is not None:
            v["_rub"] = share
            out[k] = v
    return out


async def _arbitrate(c: RA.Caller, x: dict, a, b, disputed: list[str]) -> tuple[dict, float]:
    show = lambda v: json.dumps({k: v[k] for k in KEYS}, ensure_ascii=False) if v else "нет ответа"  # noqa: E731
    user = (_card(x) + "\n\nДва аналитика независимо поставили флаги и разошлись по срезам: "
            + ", ".join(disputed) + f".\nАналитик A: {show(a)}\nАналитик B: {show(b)}\n"
            "Реши сам по определениям срезов. Верни JSON {\"payroll\":…,\"us\":…,\"field\":…,\"quote\":\"…\"} "
            "по всем трём срезам.")
    d, tin, tout = await c.ask(MODEL_ARB, user)
    rub = RA.Ledger().add(MODEL_ARB, tin, tout) if (tin or tout) else 0.0
    return flags(d) or {}, rub


# Зарплатный срез тонкий: дешёвые модели путают его с «клиент упомянул зарплату»
# (замер 14.10 против эталона из трёх сильных моделей: точность 0,73), а перепроверка
# одной моделью срезала полноту до 0,5. Поэтому payroll решают две сильные модели,
# при расхождении — третья (большинство трёх ≈ эталон). Чтобы не гонять их по всем
# кандидатам, сильным идут только отзывы с признаком: дешёвый проход сказал «да» или в
# тексте есть «зарплатн…», «з/п», «аванс», «отпускные», «реестр» — на замере в эту
# группу попали все 34 из 34 зарплатных случаев эталона.
STRONG_A = os.getenv("REVIEW_SLICES_STRONG_A", "google/gemini-3.1-pro-preview")
STRONG_B = os.getenv("REVIEW_SLICES_STRONG_B", "anthropic/claude-opus-4.8")
STRONG_TIE = os.getenv("REVIEW_SLICES_STRONG_TIE", "openai/gpt-5.4")
STRONG_KEYS = ("payroll",)
_STRONG_HINT = {"payroll": re.compile(
    r"зарплатн|зачисл\w*\s+(?:заработн\w*\s+плат|зарплат)|реестр|з/п|\bзп\b|аванс|отпускн", re.I)}
# ₽ за 1 млн токенов (вход, выход) — тарифы облака из /v1/models (14.10.2026)
_PRICES_EXTRA = {"google/gemini-3.1-pro-preview": (341.6, 2049.6),
                 "anthropic/claude-opus-4.8": (854.0, 4270.0),
                 "openai/gpt-5.4": (427.0, 2562.0)}
for _m in (STRONG_A, STRONG_B, STRONG_TIE):
    RA._MAX_TOKENS.setdefault(_m, 6000)
    RA._SEM.setdefault(_m, int(os.getenv("REVIEW_SLICES_SEM_STRONG", "16")))
_STRONG_SYS = {k: ("Ты — эксперт по аудиту банка. Применяй определение БУКВАЛЬНО к каждому отзыву.\n\n" + k + " — "
                   + SLICES[k][1] + '\n\nВерни ТОЛЬКО JSON {"items":[{"n":1,"clause":"1|2|3|а|б|в|г|нет","' + k
                   + '":true|false}]} — по объекту на каждый отзыв; clause — какой пункт ДА или НЕТ применим.')
               for k in STRONG_KEYS}


def _rub(model: str, tin: int, tout: int) -> float:
    pin, pout = RA.PRICES.get(model) or _PRICES_EXTRA.get(model) or (0.0, 0.0)
    return tin / 1e6 * pin + tout / 1e6 * pout


def needs_strong(key: str, text_: str, a: dict | None, b: dict | None, arb: dict | None) -> bool:
    """Идёт ли отзыв к сильным моделям по срезу key: дешёвый проход сказал «да» или в
    тексте есть признак среза. Остальное — «нет» без сильных моделей."""
    if any((v or {}).get(key) is True for v in (a, b, arb)):
        return True
    return bool(_STRONG_HINT[key].search(text_ or ""))


async def _strong_batch(cs: RA.Caller, model: str, key: str, items: list[dict]) -> tuple[dict[int, bool], float]:
    """Сильная модель по пакету: {номер: да/нет} и рубли. cs — свой клиент с подсказкой среза."""
    user = "\n\n".join(f"[{k + 1}]\n{x['text']}" for k, x in enumerate(items))
    d, tin, tout = await cs.ask(model, user)
    arr = [it for it in ((d or {}).get("items") or []) if isinstance(it, dict)] if isinstance(d, dict) else []
    by_n = {int(it["n"]): it for it in arr if str(it.get("n", "")).isdigit()}
    out = {k: str(by_n[k + 1].get(key)).strip().lower() == "true" for k in range(len(items))
           if k + 1 in by_n and key in by_n[k + 1]}
    return out, _rub(model, tin, tout)


async def _strong_decide(cs: RA.Caller, key: str, items: list[dict]) -> tuple[dict[int, dict], float]:
    """Две сильные модели, при расхождении или пропуске — третья. {номер: {a, b, tie, final}}."""
    (va, ra_), (vb, rb_) = await asyncio.gather(_strong_batch(cs, STRONG_A, key, items),
                                                _strong_batch(cs, STRONG_B, key, items))
    rub = ra_ + rb_
    out = {}
    for k in range(len(items)):
        a, b = va.get(k), vb.get(k)
        out[k] = {"a": a, "b": b, "tie": None, "final": a if (a is not None and a == b) else None}
    unsure = [k for k, v in out.items() if v["final"] is None]
    if unsure:
        vt, rt = await _strong_batch(cs, STRONG_TIE, key, [items[k] for k in unsure])
        rub += rt
        for j, k in enumerate(unsure):
            v = out[k]
            v["tie"] = vt.get(j)
            votes = [x for x in (v["a"], v["b"], v["tie"]) if x is not None]
            v["final"] = (sum(votes) * 2 > len(votes)) if votes else None
    return out, rub


def _spent(today: bool) -> float:
    with db.session() as s:
        return float(s.execute(text(
            "SELECT coalesce(sum(cost_rub), 0) FROM review_slice WHERE schema = :v"
            + (" AND created_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
               " AT TIME ZONE 'Europe/Moscow'" if today else "")), {"v": SCHEMA}).scalar() or 0)


def _todo(limit: int, urls: list[str] | None = None) -> list[dict]:
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT i.url, i.review_id, i.source, i.bank FROM review_index i
             WHERE i.kind IN ('complaint', 'mixed')
               AND (CAST(:u AS text[]) IS NOT NULL AND i.url = ANY(:u)
                    OR CAST(:u AS text[]) IS NULL AND i.dt >= CAST(:since AS timestamp)
                       AND i.dt <= now() AND {candidate_sql()})
               AND NOT EXISTS (SELECT 1 FROM review_slice s WHERE s.url = i.url AND s.schema = :v)
             ORDER BY i.dt DESC NULLS LAST LIMIT :n"""),
            {"u": urls, "since": SINCE, "v": SCHEMA, "n": limit}).mappings().all()
    return [dict(r) for r in rows]


_UPSERT = text("""
    INSERT INTO review_slice (url, schema, tags, status, a, b, arb, quote, cost_rub)
    VALUES (:url, :schema, :tags, :status, CAST(:a AS jsonb), CAST(:b AS jsonb), CAST(:arb AS jsonb),
            :quote, :rub)
    ON CONFLICT (url) DO UPDATE SET schema = EXCLUDED.schema, tags = EXCLUDED.tags,
        status = EXCLUDED.status, a = EXCLUDED.a, b = EXCLUDED.b, arb = EXCLUDED.arb,
        quote = EXCLUDED.quote, cost_rub = EXCLUDED.cost_rub, created_at = now()
""")


async def _process(c: RA.Caller, items: list[dict], cs: dict | None = None) -> tuple[list[dict], float]:
    ra, rb = await asyncio.gather(_tag_batch(c, MODEL_A, items), _tag_batch(c, MODEL_B, items))
    rows, rub = [], 0.0
    pre = []
    for k, x in enumerate(items):
        a, b = ra.get(k), rb.get(k)
        final, disputed = merge(a, b)
        cost = (a or {}).get("_rub", 0.0) + (b or {}).get("_rub", 0.0)
        arb = None
        status = "agree"
        if disputed:
            arb, r = await _arbitrate(c, x, a, b, disputed)
            cost += r
            if not arb:
                if a is None and b is None:
                    continue                      # никто не ответил — повтор в следующий прогон
                status = "failed"
                final = {**{k2: False for k2 in KEYS}, **final}
            else:
                final.update({k2: arb[k2] for k2 in disputed})
                status = "arbitrated" if a is not None and b is not None else "arb_single"
        pre.append((k, x, a, b, arb, final, status, cost))
    # тонкие срезы — сильным моделям пакетом, только отзывам с признаком
    strong: dict[tuple[str, int], dict] = {}
    extra: dict[int, float] = {}               # рубли сильных моделей — поровну на отзывы пакета
    for key in STRONG_KEYS:
        if cs is None or key not in cs:
            continue
        idx = [j for j, (k, x, a, b, arb, final, status, cost) in enumerate(pre)
               if needs_strong(key, x["text"], a, b, arb)]
        if idx:
            res, r = await _strong_decide(cs[key], key, [pre[j][1] for j in idx])
            for n_, j in enumerate(idx):
                strong[(key, j)] = res[n_]
                extra[j] = extra.get(j, 0.0) + r / len(idx)
    for j, (k, x, a, b, arb, final, status, cost) in enumerate(pre):
        votes = {}
        cost += extra.get(j, 0.0)
        for key in STRONG_KEYS:
            if cs is None or key not in cs:
                continue
            v = strong.get((key, j))
            if v is None:
                final[key] = False                 # без признака — не этот срез
            elif v["final"] is not None:
                final[key] = v["final"]
                votes[key] = v
            else:                                  # сильные не ответили — остаётся дешёвый проход
                status += "+strong_missing"
        quote = next((v.get("quote") for v in (arb, a, b) if v and v.get("quote")), "") if tags_of(final) else ""
        rows.append({"url": x["url"], "schema": SCHEMA, "tags": tags_of(final), "status": status,
                     "a": json.dumps({k2: a[k2] for k2 in KEYS} if a else None),
                     "b": json.dumps({k2: b[k2] for k2 in KEYS} if b else None),
                     "arb": json.dumps({**({k2: arb[k2] for k2 in KEYS} if arb else {}),
                                        **({"strong": votes} if votes else {})} or None),
                     "quote": quote, "rub": round(cost, 4)})
        rub += cost
    if rows:
        with db.session() as s:
            s.execute(_UPSERT, rows)
    return rows, rub


def _try_lock():
    if db._engine is None:
        db.init()
    conn = db._engine.connect()
    got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar())
    conn.commit()
    if not got:
        conn.close()
        return None
    return conn


async def run(limit: int = 2000, urls: list[str] | None = None, daily: bool = True) -> dict:
    """Размечает срезы у неразмеченных кандидатов, от свежих к старым. Потолок трат —
    за сутки по Москве (daily) или общий по версии; второй процесс не стартует."""
    lock = await asyncio.to_thread(_try_lock)
    if lock is None:
        return {"skipped": "locked"}
    t0 = time.time()
    done = rub = 0
    stop = None
    try:
        from .bankiru_fts import bodies_for
        todo = await asyncio.to_thread(_todo, limit, urls)
        if not todo:
            return {"done": 0}
        cap = DAILY_CAP_RUB if daily else CAP_RUB
        base = await asyncio.to_thread(_spent, daily)
        c = RA.Caller(RA.Ledger())
        c.sys = SYSTEM
        cs = {}
        for key in STRONG_KEYS:                # свой клиент на каждую подсказку: c.sys на лету не меняем
            cs[key] = RA.Caller(RA.Ledger())
            cs[key].sys = _STRONG_SYS[key]
        sem = asyncio.Semaphore(INFLIGHT)

        async def one(part):
            nonlocal done, rub, stop
            if stop:
                return
            async with sem:
                if base + rub >= cap:
                    stop = "cap"
                    return
                bodies = await asyncio.to_thread(bodies_for, part)
                items = []
                for x in part:
                    t = (bodies.get(x["url"]) or {}).get("text") or ""
                    if len(t.strip()) >= 20:
                        items.append({**x, "text": RA.scrub(t)[:_MAX_CHARS]})
                if not items:
                    return
                rows, r = await _process(c, items, cs)
                done += len(rows)
                rub += r

        await asyncio.gather(*(one(todo[k:k + BATCH]) for k in range(0, len(todo), BATCH)))
        return {"done": done, "rub": round(rub, 2), "stop": stop, "seconds": round(time.time() - t0, 1)}
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock.commit()
        except Exception:  # noqa: BLE001
            lock.invalidate()
        finally:
            lock.close()


def apply_to_index(chunk: int = 1000) -> int:
    """review_slice → review_index.slices порциями (фоновые задачи тоже пишут индекс —
    крупный UPDATE ловил взаимную блокировку)."""
    with db.session() as s:
        urls = s.execute(text("""
            SELECT s.url FROM review_slice s JOIN review_index i ON i.url = s.url
             WHERE s.schema = :v AND i.slices IS DISTINCT FROM s.tags ORDER BY s.url"""),
            {"v": SCHEMA}).scalars().all()
    n = 0
    for k in range(0, len(urls), chunk):
        part = urls[k:k + chunk]
        for attempt in range(5):
            try:
                with db.session() as s:
                    n += s.execute(text("""
                        UPDATE review_index i SET slices = s.tags FROM review_slice s
                         WHERE s.url = i.url AND s.schema = :v AND i.url = ANY(:u)"""),
                        {"v": SCHEMA, "u": part}).rowcount
                break
            except Exception:  # noqa: BLE001
                time.sleep(2 + attempt * 3)
    if n:
        log.info("срезы отзывов: в индекс перенесено %d", n)
    return n


READY_SHARE = float(os.getenv("REVIEW_SLICES_READY", "0.97"))


def coverage(days: int = 365) -> dict:
    """Доля размеченных кандидатов за окно. Пока разметка не догнала (массовый
    прогон, смена версии определений), срезы в списке продуктов не показываются:
    недоразмеченный срез показал бы заниженные числа."""
    with db.session() as s:
        n, tagged = s.execute(text(f"""
            SELECT count(*), count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM review_slice s WHERE s.url = i.url AND s.schema = :v))
              FROM review_index i
             WHERE i.kind IN ('complaint', 'mixed') AND i.dt >= now() - make_interval(days => :d)
               AND i.dt <= now() AND i.dt >= CAST(:since AS timestamp) AND {candidate_sql()}"""),
            {"v": SCHEMA, "d": days, "since": SINCE}).one()
    share = (tagged / n) if n else 1.0
    return {"candidates": int(n), "tagged": int(tagged), "share": round(share, 4),
            "ready": share >= READY_SHARE}

