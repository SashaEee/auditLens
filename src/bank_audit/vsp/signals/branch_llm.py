"""Вывод по отделению: модель читает все тяжёлые истории отделения разом.

Каждый отзыв модель читает отдельно, а «почерк» — это когда несколько разных клиентов
описывают один и тот же приём, и видно это только при чтении подряд. Здесь модель получает
все тяжёлые истории отделения (по одной на клиента) и отвечает: есть ли общий почерк, какие
истории к нему относятся, что известно об исполнителях, чем это может объясняться безобидно
и что запросить из данных банка. Число клиентов, период и плотность считает код по
названным моделью историям.

Замер 10.11 на 36 отделениях: вывод совпал с ручным чтением у 26 из 28, ≈0,6 ₽ за отделение.
Слабое место — почерк, который на самом деле идёт по всей сети: поэтому модель получает
список текущих волн, а код сверяет названные истории с частотой по сети (risk.assess).

Читаются отделения, где за 5 лет три и больше разных клиентов описали тяжёлое; повторно —
только когда состав историй изменился. Результат — таблица vsp_branch_llm.

Запуск: python -m bank_audit.vsp branch-read [--limit N] [--cap-rub N]  (после расчёта signals)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, Float, String, Table, Text, func, select

from ..models import utcnow
from ..storage import Store, meta, network, signal_runs
from .kinds import BACKGROUND, kind_name
from .llm_read import MODEL, Reader

log = logging.getLogger(__name__)

branch_llm = Table(
    "vsp_branch_llm", meta,
    Column("point", String(160), primary_key=True),
    Column("stories_hash", String(16)),           # состав историй, по которому сделан вывод
    Column("model", String(80)),
    Column("data_json", Text),
    Column("cost_rub", Float),
    Column("at", DateTime(timezone=True)),
)

BRANCH_MODEL = os.getenv("VSP_LLM_BRANCH_MODEL", MODEL)
VERSION = "b1"
MIN_CLIENTS = 3
YEARS = 5
MAX_STORIES = 30
CAP_RUB = float(os.getenv("VSP_LLM_BRANCH_CAP_RUB", "150"))

SYSTEM = """Ты — опытный аудитор розничного банка. Тебе дают все тяжёлые истории клиентов об одном отделении банка
за несколько лет: пересказ отзыва, дата, тип, отметка «пожилой или уязвимый», имя сотрудника и окно, если клиент их
назвал. Каждая история — слова клиента, а не установленный факт. Истории пронумерованы, каждая — от отдельного клиента.

Задача: понять, есть ли у отделения общий «почерк» — один и тот же узкий способ действия, который описывают РАЗНЫЕ
клиенты, — или это случайный набор разных жалоб, какой бывает у любого крупного отделения.

Правила:
- «почерк» — это одинаковый способ действия (например: «готовую кредитную карту выносят вместе с дебетовой», «берут
  телефон клиента и подключают услуги», «пожилым оформляют страховку карты за 30 тысяч»), а не общая тема вроде
  «навязывание» или «плохое обслуживание»;
- жалобы на банкомат, на тарифы банка и на правила всей сети почерком отделения не считай;
- ниже дан список способов, которые сейчас часто встречаются по всей сети. Если почерк отделения совпадает с одним
  из них, так и напиши в network: это может быть общая практика, а не особенность отделения;
- не утверждай вину: пиши «клиенты пишут», «стоит проверить»; не пиши «мошенничество», «обман», «сговор»;
- если почерка нет — так и скажи, не натягивай.

Верни JSON:
{"pattern": true|false,
 "pattern_name": "3–8 слов простым языком" | null,
 "stories": [номера историй, которые описывают именно этот почерк],
 "who": "что известно об исполнителях: имя, окно, должность или «разные сотрудники»" | null,
 "victims": "кто чаще пострадал" | null,
 "money": "какие суммы называли клиенты" | null,
 "network": "название способа из списка сети, с которым совпадает почерк" | null,
 "other": "что ещё заметного в этом отделении, не относящееся к почерку (одна фраза)" | null,
 "benign": "чем это может объясняться безобидно (одна фраза)",
 "request": ["что запросить из данных банка, чтобы подтвердить или снять вопрос — 2–3 пункта, конкретно: какие
   операции, за какой период, в каком разрезе"],
 "confidence": "низкая" | "средняя" | "высокая",
 "why": "1–2 фразы: почему такой вывод"}
Отвечай строго JSON."""


def _aware(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _inputs(store: Store) -> tuple[dict[str, list[dict]], list[dict], dict[str, dict]]:
    """Истории по отделениям, волны и справочник точек — из последнего расчёта."""
    with store.engine.connect() as c:
        rid = c.execute(select(func.max(signal_runs.c.run_id)).where(signal_runs.c.status == "done")).scalar()
        if rid is None:
            return {}, [], {}
        net = {k: json.loads(v) for k, v in c.execute(select(network.c.key, network.c.value_json).where(
            network.c.run_id == rid, network.c.key.in_(["stories", "waves", "places"])))}
    by: dict[str, list[dict]] = defaultdict(list)
    for s in net.get("stories") or []:
        if s.get("severity", 0) >= 2 and not s.get("elsewhere") and not s.get("hq") and not s.get("copy_of") \
                and s.get("kind") not in BACKGROUND:
            by[s["point"]].append(s)
    return by, net.get("waves") or [], net.get("places") or {}


def candidates(by: dict[str, list[dict]], *, now: datetime) -> dict[str, list[dict]]:
    cut = now - timedelta(days=365 * YEARS + 1)
    out = {}
    for p, ss in by.items():
        recent = [s for s in ss if _aware(s["at"]) >= cut]
        if len({s.get("client") or s["rid"] for s in recent}) >= MIN_CLIENTS:
            out[p] = sorted(ss, key=lambda s: s["at"])[-MAX_STORIES:]
    return out


def stories_hash(ss: list[dict]) -> str:
    return hashlib.sha1((VERSION + "|" + "|".join(sorted(s["rid"] for s in ss))).encode()).hexdigest()[:16]


def prompt(place: dict, ss: list[dict], waves: list[dict]) -> str:
    net = [w["name"] for w in waves if w.get("status") in ("rising", "new", "steady") and (w.get("year") or 0) >= 8][:14]
    head = f"Отделение {place.get('code') or ''}, {place.get('city') or ''}. Тяжёлых историй: {len(ss)}."
    lines = [head, "", "Сейчас часто встречается по всей сети: " + ("; ".join(net) if net else "нет данных") + ".", ""]
    for i, s in enumerate(ss, 1):
        extra = []
        if s.get("vulnerable"):
            extra.append("пожилой или уязвимый")
        if s.get("employee"):
            extra.append("сотрудник: " + s["employee"])
        if s.get("window"):
            extra.append(str(s["window"]))
        m = s.get("money")
        if isinstance(m, (int, float)) and m:
            extra.append(f"сумма по словам клиента: {int(m)} ₽")
        lines.append(f"{i}. {s.get('date')} · {kind_name(s.get('kind')).lower()} · тяжесть {s.get('severity')}"
                     + (" · " + " · ".join(extra) if extra else "") + f"\n   {(s.get('episode') or '')[:420]}")
    return "\n".join(lines)


def normalize(d: dict | None, ss: list[dict]) -> dict | None:
    """Ответ модели → вывод. Число клиентов, период и плотность считаем сами по названным историям."""
    if not isinstance(d, dict) or "pattern" not in d:
        return None
    idx = sorted({int(n) for n in (d.get("stories") or []) if str(n).isdigit() and 1 <= int(n) <= len(ss)})
    picked = [ss[i - 1] for i in idx]
    pat = bool(d.get("pattern")) and len(picked) >= 2
    txt = lambda k, n=400: (str(d.get(k)).strip()[:n] if d.get(k) not in (None, "", "null") else None)  # noqa: E731
    out = {"pattern": pat, "pattern_name": txt("pattern_name", 160) if pat else None,
           "rids": [s["rid"] for s in picked] if pat else [],
           "clients": len({s.get("client") or s["rid"] for s in picked}) if pat else 0,
           "who": txt("who"), "victims": txt("victims"), "money": txt("money"), "network": txt("network", 200),
           "other": txt("other"), "benign": txt("benign"),
           "request": [str(x).strip()[:500] for x in (d.get("request") or []) if str(x).strip()][:3],
           "confidence": d.get("confidence") if d.get("confidence") in ("низкая", "средняя", "высокая") else "низкая",
           "why": txt("why", 600)}
    if pat:
        ds = sorted(_aware(s["at"]) for s in picked)
        out["first"], out["last"] = ds[0].isoformat(), ds[-1].isoformat()
        out["period"] = f"{ds[0].strftime('%m.%Y')} — {ds[-1].strftime('%m.%Y')}"
        best, j = 1, 0
        for i in range(len(ds)):
            while (ds[i] - ds[j]).days > 365:
                j += 1
            best = max(best, i - j + 1)
        out["dense"] = best >= 3
        out["kinds"] = sorted({s.get("kind") for s in picked if s.get("kind")})
    return out


def load(store: Store) -> dict[str, dict]:
    """Выводы по отделениям для расчёта: точка → вывод (с отметкой, по какому составу историй он сделан)."""
    meta.create_all(store.engine, tables=[branch_llm])
    out = {}
    with store.engine.connect() as c:
        for p, h, dj, at in c.execute(select(branch_llm.c.point, branch_llm.c.stories_hash, branch_llm.c.data_json,
                                             branch_llm.c.at)):
            try:
                d = json.loads(dj or "{}")
            except ValueError:
                continue
            d["_hash"], d["at"] = h, at.isoformat() if at else None
            out[p] = d
    return out


async def run(store: Store, *, limit: int = 2000, cap_rub: float | None = None, now: datetime | None = None,
              reader: Reader | None = None) -> dict:
    cap = CAP_RUB if cap_rub is None else cap_rub
    now = now or utcnow()
    meta.create_all(store.engine, tables=[branch_llm])
    by, waves, places = _inputs(store)
    cand = candidates(by, now=now)
    with store.engine.connect() as c:
        have = dict(c.execute(select(branch_llm.c.point, branch_llm.c.stories_hash)).all())
    todo = [(p, ss) for p, ss in cand.items() if have.get(p) != stories_hash(ss)]
    todo.sort(key=lambda t: max(s["at"] for s in t[1]), reverse=True)      # сначала отделения со свежими историями
    todo = todo[:limit]
    rd = reader or Reader(BRANCH_MODEL, SYSTEM)
    saved = 0

    async def one(p: str, ss: list[dict]) -> None:
        nonlocal saved
        if rd.rub >= cap:
            return
        d, rub = await rd.ask(prompt(places.get(p) or {}, ss, waves))
        v = normalize(d, ss)
        if v is None:
            return
        with store.engine.begin() as c:
            c.execute(branch_llm.delete().where(branch_llm.c.point == p))
            c.execute(branch_llm.insert().values(point=p, stories_hash=stories_hash(ss), model=rd.model,
                                                 data_json=json.dumps(v, ensure_ascii=False), cost_rub=round(rub, 4),
                                                 at=utcnow()))
        saved += 1
    await asyncio.gather(*(one(p, ss) for p, ss in todo))
    res = {"candidates": len(cand), "todo": len(todo), "saved": saved, "rub": round(rd.rub, 2), "calls": rd.calls,
           "errors": rd.errors, "model": rd.model}
    log.info("выводы по отделениям: %s", res)
    return res
