"""Канарейка качества срезов «Аудита отзывов» (rag/review_slices.py).

Зачем. Качество срезов мерили один раз — на эталоне 04.11.2026 (GOLD_0411): 260 жалоб,
два слепых разметчика и арбитр. Дальше разметка живёт сама: облако меняет версию модели,
мы правим определения, провайдер тихо деградирует — и узнаём об этом от аудиторов, когда
в срезе уже неделю лишнее. Раз в неделю каскад прогоняется на постоянной выборке из
эталона, точность и полнота каждого среза ложатся в review_slice_canary (миграция 107),
«Пульс» показывает последние прогоны, а падение поднимает тревогу.

Как.
• Эталон — review_slice_gold: scripts/load_slice_gold.py из файлов ручной разметки.
• Выборка постоянная (pick): по каждому срезу до PER_SLICE положительных и NEGATIVES жалоб
  без единого среза, порядок — по хэшу адреса. Каждую неделю одни и те же жалобы: разница
  между прогонами — от модели и определений, а не от выборки.
• Каскад — тот же, что в разметке (_cascade — зеркало review_slices._process): те же
  модели, промпт, подготовка текста, контрольная доля для второй модели, большинство из
  трёх. Но БЕЗ записи в review_slice и в журнал трат: канарейка не меняет срезы во
  вкладке и не съедает суточный потолок разметки — у неё свой бюджет на прогон.
• Тревога (alerts) — точность среза ниже FLOOR или упала больше чем на DROP против
  прошлого прогона; срез, который замолчал при живых положительных эталона, — тоже.
  Прошлый прогон берётся любой версии определений: переход s7 → s8 — ровно то место,
  где падение и надо поймать (версия каждого прогона — в строке).
• Бюджет — на прогон (CANARY_RUB, проверка перед каждым пакетом). Оценка 04.11.2026 по
  длине жалоб эталона: полная выборка (около 150 жалоб) стоит порядка 300 ₽, при 150 ₽
  прогон останавливается примерно на половине — почти на той же от недели к неделе
  (порядок по хэшу), но опора каждого среза тоньше; полный прогон — REVIEW_SLICES_CANARY_RUB.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import text

from .. import db
from . import review_annotate as RA
from . import review_slices as RS

log = logging.getLogger(__name__)

EVERY_S = int(os.getenv("REVIEW_SLICES_CANARY_EVERY_S", str(7 * 86400)))
# не вышло (тексты не прочитались, модель молчит, упала запись) — повтор через сутки,
# а не каждый тик: иначе сбой после трат оплачивался бы каждый час
RETRY_S = int(os.getenv("REVIEW_SLICES_CANARY_RETRY_S", str(86400)))
CANARY_RUB = float(os.getenv("REVIEW_SLICES_CANARY_RUB", "300"))
PER_SLICE = int(os.getenv("REVIEW_SLICES_CANARY_PER_SLICE", "25"))
NEGATIVES = int(os.getenv("REVIEW_SLICES_CANARY_NEG", "40"))
# пакеты идут по нескольку разом; бюджет проверяется перед каждым, так что перерасход —
# не больше пакетов, уже ушедших к модели
INFLIGHT = int(os.getenv("REVIEW_SLICES_CANARY_INFLIGHT", "4"))
FLOOR = float(os.getenv("REVIEW_SLICES_CANARY_FLOOR", "0.80"))
DROP = float(os.getenv("REVIEW_SLICES_CANARY_DROP", "0.10"))
# Полнота: срез, который ловит меньше 0,70 жалоб эталона, теряет сюжеты тихо — точность
# этого не видит (на эталоне 04.11 полнота срезов 0,85–1,0)
RECALL_FLOOR = float(os.getenv("REVIEW_SLICES_CANARY_RECALL_FLOOR", "0.70"))
# точность по одному-двум решениям «да» — шум (1 из 2 = 0,50): тревогу по точности
# поднимаем, когда «да» не меньше MIN_SUPPORT
MIN_SUPPORT = int(os.getenv("REVIEW_SLICES_CANARY_MIN_SUPPORT", "5"))
# Каскад ответил меньше чем на половину отправленного (не по бюджету) — прогон не
# мерит ничего: у всех срезов поддержка ниже MIN_SUPPORT и тревоги «нет». Такой
# прогон — тревога сам по себе (ревью 04.11.2026).
MIN_DECIDED = float(os.getenv("REVIEW_SLICES_CANARY_MIN_DECIDED", "0.5"))
# Прогон держит фоновый цикл индекса — не дольше этого
RUN_TIMEOUT_S = float(os.getenv("REVIEW_SLICES_CANARY_TIMEOUT_S", "2400"))
_TICK_KEY = "review_slices_canary_at"
# каскад сам по себе не дал решения: A молчит, B не ответила на запрос подтверждения,
# третья молчит при споре. Разметка такие строки повторяет и в индекс не берёт —
# канарейка не считает их ни «да», ни «нет»
_NO_DECISION = ("failed", "failed_tie", "b_missing")


# ── выборка из эталона ───────────────────────────────────────────────────────

def _h(url: str) -> int:
    # своя соль: выборка не должна совпадать с контрольной долей confirm_negative
    return int(hashlib.blake2b(f"canary:{url}".encode(), digest_size=8).hexdigest(), 16)


def pick(gold: list[dict], per_slice: int = PER_SLICE, negatives: int = NEGATIVES) -> list[dict]:
    """Постоянная выборка: по каждому срезу до per_slice положительных эталона и negatives
    жалоб, где люди не поставили ни одного среза. Отбор и порядок — по хэшу адреса: от
    порядка строк в базе и от прогона к прогону не зависит. Возвращает строки эталона в
    порядке хэша — при остановке по бюджету отрезается один и тот же хвост."""
    rows = sorted((g for g in gold if g.get("url") and isinstance(g.get("labels"), dict)),
                  key=lambda g: (_h(g["url"]), g["url"]))
    chosen: set[str] = set()
    for key in RS.KEYS:
        chosen.update([g["url"] for g in rows if g["labels"].get(key) is True][:per_slice])
    neg = [g["url"] for g in rows if g["url"] not in chosen
           and any(k in g["labels"] for k in RS.KEYS)
           and not any(g["labels"].get(k) is True for k in RS.KEYS)]
    chosen.update(neg[:negatives])
    return [g for g in rows if g["url"] in chosen]


# ── каскад без записи ────────────────────────────────────────────────────────

async def _cascade(c, items: list[dict]) -> tuple[dict[int, dict], float]:
    """Решения каскада по пакету: {номер: итог по срезам} и рубли. Зеркало
    review_slices._process (тест сверяет их на одних ответах), но без записи
    review_slice и журнала трат. Номера без решения (_NO_DECISION) в словарь не попадают."""
    ra, rub = await RS._ask_batch(c, RS.STRONG_A, items)
    # вторая модель: все положительные A и контрольная доля отрицательных
    need_b = [k for k in range(len(items)) if k in ra
              and (any(ra[k][key] for key in RS.KEYS) or RS.confirm_negative(items[k]["url"]))]
    rb: dict[int, dict] = {}
    if need_b:
        got, r = await RS._ask_batch(c, RS.STRONG_B, [items[k] for k in need_b])
        rub += r
        rb = {need_b[j]: v for j, v in got.items()}
    # третья — где разошлись
    need_c = [k for k in rb if any(ra[k][key] != rb[k][key] for key in RS.KEYS)]
    rc: dict[int, dict] = {}
    if need_c:
        got, r = await RS._ask_batch(c, RS.STRONG_TIE, [items[k] for k in need_c])
        rub += r
        rc = {need_c[j]: v for j, v in got.items()}
    out: dict[int, dict] = {}
    for k, a in ra.items():
        b = rb.get(k)
        if k in need_b and b is None:
            continue                                     # b_missing
        final, _ev, status = RS.decide(a, b, rc.get(k))
        if status not in _NO_DECISION:
            out[k] = {key: bool(final.get(key)) for key in RS.KEYS}
    return out, rub


# ── счёт и тревога ───────────────────────────────────────────────────────────

def score(gold: dict[str, dict], got: dict[str, dict]) -> dict:
    """{срез: {tp, fp, fn, precision, recall}} по жалобам, где есть и решение людей по
    срезу, и решение каскада. Нет ни одного «да» — точности нет (None), а не ноль."""
    out = {}
    for key in RS.KEYS:
        tp = fp = fn = 0
        for url, final in got.items():
            lab = gold.get(url) or {}
            if key not in lab:
                continue
            g, p = lab[key] is True, bool(final.get(key))
            tp += g and p
            fp += p and not g
            fn += g and not p
        out[key] = {"tp": tp, "fp": fp, "fn": fn,
                    "precision": round(tp / (tp + fp), 4) if tp + fp else None,
                    "recall": round(tp / (tp + fn), 4) if tp + fn else None}
    return out


def _num(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def alerts(metrics: dict, prev: dict | None = None) -> list[str]:
    """Причины тревоги — пусто, если всё в порядке. Полнота ниже RECALL_FLOOR; точность ниже FLOOR; упала больше
    чем на DROP против прошлого прогона (оба с «да» не меньше MIN_SUPPORT); срез молчит,
    хотя положительных эталона в выборке не меньше MIN_SUPPORT, — полная потеря среза
    точностью не видна (ни одного «да» — нечего делить)."""
    out = []
    for key in RS.KEYS:
        m = (metrics or {}).get(key) or {}
        label = RS.LABELS.get(key, key)
        p = m.get("precision")
        if p is None:
            if (m.get("fn") or 0) >= MIN_SUPPORT:
                out.append(f"{label}: срез замолчал — {m['fn']} жалоб эталона, ни одного «да»")
            continue
        r = m.get("recall")
        if r is not None and (m.get("tp") or 0) + (m.get("fn") or 0) >= MIN_SUPPORT and r < RECALL_FLOOR:
            out.append(f"{label}: полнота {_num(r)} ниже {_num(RECALL_FLOOR)}")
        if (m.get("tp") or 0) + (m.get("fp") or 0) < MIN_SUPPORT:
            continue
        if p < FLOOR:
            out.append(f"{label}: точность {_num(p)} ниже {_num(FLOOR)}")
        q = (prev or {}).get(key) or {}
        pp = q.get("precision")
        # round — чтобы 0,95 → 0,85 (ровно 0,10) не стало тревогой из-за двоичной дроби
        if (pp is not None and (q.get("tp") or 0) + (q.get("fp") or 0) >= MIN_SUPPORT
                and round(pp - p, 4) > DROP):
            out.append(f"{label}: точность {_num(p)} — упала с {_num(pp)}")
    return out


# ── прогон ───────────────────────────────────────────────────────────────────

def _iso(t: datetime) -> str:
    # время базы приходит в часовом поясе сессии — наружу всегда UTC, одной записью
    return t.astimezone(timezone.utc).isoformat()


def _caller():
    return RA.Caller(RA.Ledger())


def _fetch(rows: list[dict]):
    from .bankiru_fts import bodies_for
    return bodies_for(rows)


def _load_gold() -> list[dict]:
    """Эталон с координатами текста из индекса: review_id и source нужны bodies_for."""
    with db.session() as s:
        return [dict(r) for r in s.execute(text("""
            SELECT g.url, g.labels, i.review_id, i.source, i.bank
              FROM review_slice_gold g LEFT JOIN review_index i ON i.url = g.url""")).mappings()]


def _previous(before: datetime) -> dict | None:
    with db.session() as s:
        r = s.execute(text("""SELECT run_at, schema, metrics FROM review_slice_canary
                               WHERE run_at < :t ORDER BY run_at DESC LIMIT 1"""),
                      {"t": before}).mappings().first()
    return dict(r) if r else None


def _save(row: dict) -> None:
    with db.session() as s:
        s.execute(text("""INSERT INTO review_slice_canary (run_at, schema, n, metrics, rub, note)
                          VALUES (:run_at, :schema, :n, CAST(:metrics AS jsonb), :rub, :note)"""),
                  {**row, "metrics": json.dumps(row["metrics"], ensure_ascii=False)})


async def run(sample: list[dict] | None = None, *, caller=None, budget_rub: float = CANARY_RUB,
              bodies=None) -> dict:
    """Прогон на постоянной выборке. sample — строки эталона {url, labels, review_id,
    source, bank} (по умолчанию — review_slice_gold вместе с индексом); caller — модель
    с async ask(model, user) (по умолчанию — как в разметке); bodies — функция текстов
    по строкам индекса (по умолчанию bankiru_fts.bodies_for).

    Пишет строку review_slice_canary, если хоть по одной жалобе есть решение, и
    возвращает её вместе с тревогой и разбором пропусков: нет в индексе (missing),
    хранилище не ответило (failed), текста нет (empty), каскад без решения
    (unanswered), не дошли из-за бюджета (not_reached)."""
    run_at = datetime.now(timezone.utc)
    gold = sample if sample is not None else await asyncio.to_thread(_load_gold)
    picked = pick(gold)
    # прошлый прогон — до трат: нет таблицы — падаем, не заплатив
    prev = await asyncio.to_thread(_previous, run_at)
    fetch = bodies or _fetch
    have = [g for g in picked if g.get("review_id") is not None]
    missing = [g["url"] for g in picked if g.get("review_id") is None]
    got = await asyncio.to_thread(fetch, have) if have else {}
    failed = sorted(set(getattr(got, "failed", ()) or ()) & {g["url"] for g in have})
    items, empty = [], []
    for g in have:
        if g["url"] in failed:
            continue
        t = (got.get(g["url"]) or {}).get("text") or ""
        if len(t.strip()) < 20:
            empty.append(g["url"])
            continue
        items.append({"url": g["url"], "bank": g.get("bank"), "text": RS.clip(RA.scrub(t))})

    c = caller or _caller()
    c.sys = RS.SYSTEM            # тот же промпт, что у разметки, даже у чужого Caller
    sem = asyncio.Semaphore(max(1, INFLIGHT))
    decided: dict[str, dict] = {}
    unanswered: list[str] = []
    not_reached: list[str] = []
    spent = 0.0
    stop = None

    async def one(part: list[dict]) -> None:
        nonlocal spent, stop
        async with sem:
            if stop or spent >= budget_rub:
                stop = "budget"
                not_reached.extend(x["url"] for x in part)
                return
            res, r = await _cascade(c, part)
            spent += r
            for k, x in enumerate(part):
                if k in res:
                    decided[x["url"]] = res[k]
                else:
                    unanswered.append(x["url"])

    await asyncio.gather(*(one(items[k:k + RS.BATCH]) for k in range(0, len(items), RS.BATCH)))

    metrics = score({g["url"]: g["labels"] for g in picked}, decided)
    reasons = alerts(metrics, (prev or {}).get("metrics"))
    sent = len(items) - len(not_reached)
    if sent and len(decided) < MIN_DECIDED * sent:
        reasons.insert(0, f"каскад ответил на {len(decided)} из {sent} — замер неполный")
    parts = [f"выборка {len(picked)}, решений {len(decided)}"]
    for name, xs in (("нет в индексе", missing), ("хранилище не ответило", failed),
                     ("без текста", empty), ("каскад без решения", unanswered),
                     ("не дошли — бюджет", not_reached)):
        if xs:
            parts.append(f"{name}: {len(xs)}")
    if reasons:
        parts.append("тревога: " + "; ".join(reasons))
    row = {"run_at": run_at, "schema": RS.SCHEMA, "n": len(decided), "metrics": metrics,
           "rub": round(spent, 2), "note": "; ".join(parts)}
    if decided:
        await asyncio.to_thread(_save, row)
    return {**row, "run_at": _iso(run_at), "saved": bool(decided),
            "alert": bool(reasons), "reasons": reasons, "stop": stop, "picked": len(picked),
            "prev": _iso(prev["run_at"]) if prev and prev.get("run_at") else None,
            "missing": missing, "failed": failed, "empty": empty,
            "unanswered": unanswered, "not_reached": not_reached}


def history(limit: int = 4) -> list[dict]:
    """Последние прогоны для «Пульса», свежие первыми; тревога каждого — против
    предыдущего прогона, тем же правилом, что в run."""
    with db.session() as s:
        rows = s.execute(text("""SELECT run_at, schema, n, metrics, rub, note FROM review_slice_canary
                                 ORDER BY run_at DESC LIMIT :n"""), {"n": limit + 1}).mappings().all()
    out = []
    for i, r in enumerate(rows[:limit]):
        prev = rows[i + 1]["metrics"] if i + 1 < len(rows) else None
        reasons = alerts(r["metrics"] or {}, prev)
        out.append({"run_at": _iso(r["run_at"]), "schema": r["schema"], "n": int(r["n"] or 0),
                    "metrics": r["metrics"] or {}, "rub": float(r["rub"] or 0), "note": r["note"],
                    "alert": bool(reasons), "reasons": reasons})
    return out


# ── фоновый тик ──────────────────────────────────────────────────────────────

def _last_at() -> float:
    with db.session() as s:
        row = s.execute(text("SELECT v FROM review_index_state WHERE k = :k"), {"k": _TICK_KEY}).first()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def _mark(at: float) -> None:
    with db.session() as s:
        s.execute(text("""INSERT INTO review_index_state (k, v, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)
                          ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = CURRENT_TIMESTAMP"""),
                  {"k": _TICK_KEY, "v": f"{at:.0f}"})


def _gold_count() -> int:
    try:
        with db.session() as s:
            return int(s.execute(text("SELECT count(*) FROM review_slice_gold")).scalar() or 0)
    except Exception:  # noqa: BLE001 — миграция 107 ещё не накатана: эталона нет
        return 0


async def tick(now: float | None = None) -> dict:
    """Для фонового цикла индекса отзывов: прогон не чаще раза в EVERY_S (неделя) и только
    при непустом эталоне. Не вышло — следующая попытка через RETRY_S."""
    at = time.time() if now is None else now
    last = await asyncio.to_thread(_last_at)
    if at - last < EVERY_S:
        return {"skipped": "not_due"}
    if not await asyncio.to_thread(_gold_count):
        return {"skipped": "no_gold"}
    retry = at - EVERY_S + RETRY_S
    # отметка «повтор через RETRY_S» — ДО прогона: перезапуск посреди прогона (выкатка)
    # иначе запускал его заново сразу, и деньги прерванного терялись дважды
    await asyncio.to_thread(_mark, retry)
    res = await asyncio.wait_for(run(), timeout=RUN_TIMEOUT_S)
    if res.get("saved"):
        await asyncio.to_thread(_mark, at)
    else:
        log.warning("канарейка срезов: прогон без решений (%s) — повтор через %.0f ч",
                    res.get("note") or res.get("stop") or "?", RETRY_S / 3600)
    if res.get("alert"):
        log.warning("канарейка срезов: тревога — %s", "; ".join(res["reasons"]))
    return res


# ── загрузка эталона (scripts/load_slice_gold.py) ────────────────────────────

def parse_gold(gold: dict, sample) -> tuple[list[dict], dict]:
    """Файлы ручной разметки → [{url, labels: {срез: bool}}] и разбор.

    gold — {"gold": {id: {срез: {"v": bool, "clause": …}}}} (или сразу {id: …}; метка
    может быть и голым bool); sample — список {id, url, …} или {id: url | {url}}.
    Срезы не из реестра отбрасываются (опечатка или срез, которого уже нет)."""
    g = gold.get("gold", gold) if isinstance(gold, dict) else {}
    if isinstance(sample, dict):
        urls = {str(k): (v.get("url") if isinstance(v, dict) else v) for k, v in sample.items()}
    else:
        urls = {str(x.get("id")): x.get("url") for x in sample or [] if isinstance(x, dict)}
    rows: dict[str, dict] = {}
    no_url, unknown, dup = [], set(), 0
    for gid, labs in (g or {}).items():
        url = urls.get(str(gid))
        if not url or not isinstance(labs, dict):
            no_url.append(str(gid))
            continue
        lab = {}
        for key, v in labs.items():
            if key not in RS.KEYS:
                unknown.add(key)
                continue
            lab[key] = bool(v.get("v") if isinstance(v, dict) else v)
        if not lab:
            no_url.append(str(gid))
            continue
        dup += url in rows
        rows[url] = {"url": url, "labels": lab}
    out = list(rows.values())
    pos = {k: sum(1 for r in out if r["labels"].get(k)) for k in RS.KEYS}
    neg = sum(1 for r in out if not any(r["labels"].values()))
    return out, {"ids": len(g or {}), "rows": len(out), "no_url": no_url,
                 "unknown_keys": sorted(unknown), "duplicates": dup, "positives": pos, "negatives": neg}


def save_gold(rows: list[dict], source: str | None) -> int:
    """Upsert эталона: повторная загрузка того же набора обновляет метки, не множит строки."""
    if not rows:
        return 0
    with db.session() as s:
        s.execute(text("""INSERT INTO review_slice_gold (url, labels, source) VALUES (:u, CAST(:l AS jsonb), :s)
                          ON CONFLICT (url) DO UPDATE SET labels = EXCLUDED.labels,
                              source = EXCLUDED.source, added_at = now()"""),
                  [{"u": r["url"], "l": json.dumps(r["labels"]), "s": source} for r in rows])
    return len(rows)


def gold_stats() -> dict:
    """Сколько эталона в базе и сколько из него канарейка может прочитать (есть в индексе)."""
    with db.session() as s:
        n, in_index = s.execute(text("""SELECT count(*), count(i.url) FROM review_slice_gold g
                                         LEFT JOIN review_index i ON i.url = g.url""")).one()
    return {"rows": int(n), "in_index": int(in_index)}
