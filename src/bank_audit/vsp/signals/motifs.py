"""Почерки: узкие способы действия внутри типа истории — из самих историй, без закрытого списка.

Этап «уточнение» (llm_read) даёт каждой тяжёлой истории тип и короткое описание способа
действия своими словами. Здесь модель-редактор раскладывает эти описания на почерки:

  1) для типа без каталога — предлагает почерки по описаниям (propose);
  2) каждое новое описание относит к почерку каталога, к «прочему» или предлагает новый
     почерк, если один и тот же новый приём встретился у нескольких историй (assign).

Каталог только растёт: id почерка не меняется между запусками, поэтому ссылки и история
волн не ломаются, а новый почерк — это и есть «новая схема» из данных. Код проверяет, что
каждое описание отнесено ровно к одному почерку. Таблицы: vsp_motif, vsp_motif_label.

Запуск: python -m bank_audit.vsp motifs [--cap-rub N]
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter, defaultdict

from sqlalchemy import Column, DateTime, Integer, String, Table, Text, select

from ..models import utcnow
from ..storage import Store, meta
from .kinds import BACKGROUND, KINDS, kind_name
from .llm_read import DETAIL_VERSION, MODEL, PROMPT_VERSION, Reader, review_llm

log = logging.getLogger(__name__)

motif_t = Table(
    "vsp_motif", meta,
    Column("motif_id", String(12), primary_key=True),         # m0803: тип 8, почерк 3; mNN00 — «прочее»
    Column("kind", Integer, index=True),
    Column("name", Text), Column("description", Text),
    Column("benign", Text), Column("ops", Text), Column("metric", Text),
    Column("created_at", DateTime(timezone=True)), Column("updated_at", DateTime(timezone=True)),
)
motif_label = Table(
    "vsp_motif_label", meta,
    Column("kind", Integer, primary_key=True),
    Column("label", String(160), primary_key=True),            # описание способа действия от модели, нормализованное
    Column("motif_id", String(12), index=True),
    Column("at", DateTime(timezone=True)),
)

MOTIF_MODEL = os.getenv("VSP_LLM_MOTIF_MODEL", MODEL)
PROPOSE_MIN = 20           # столько описаний нужно, чтобы предлагать почерки для типа
PROPOSE_SAMPLE = 300
ASSIGN_BATCH = 40
CAP_RUB = float(os.getenv("VSP_LLM_MOTIF_CAP_RUB", "300"))
FORBIDDEN = re.compile(r"мошенни|обман|сговор|воровств|кража|афер", re.I)

SYSTEM_PROPOSE = """Ты — руководитель внутреннего аудита розничного банка. Тебе дают короткие описания того, что, по
словам клиентов, сделали сотрудники отделений. Все описания одного типа: «{kind}». Раздели их на узкие «почерки» —
способы действия, которые повторяются у разных клиентов.

Правила:
- почерк — это конкретный приём («готовую кредитную карту выносят вместе с дебетовой»), а не тема («навязывание карт»);
- от 3 до 14 почерков; редкие и разнородные описания в почерки не выделяй — они пойдут в «прочее»;
- name — 3–8 слов простым русским языком. Это слова клиентов, вина не установлена: не пиши «мошенничество», «обман»,
  «сговор», «кража»;
- description — одно предложение, начинается со слов «Клиенты пишут, что»;
- benign — чем это может объясняться безобидно (одна фраза);
- ops — какие операции запросить из данных банка, чтобы проверить именно этот приём (одна фраза: какие операции и по
  какому признаку их отобрать);
- metric — какой показатель подтвердит или снимет вопрос (одна фраза).

Верни JSON: {"motifs": [{"name": "...", "description": "...", "benign": "...", "ops": "...", "metric": "..."}, ...]}
Отвечай строго JSON."""

SYSTEM_ASSIGN = """Ты — руководитель внутреннего аудита розничного банка. Есть перечень «почерков» — узких способов
действия сотрудников отделений. Все они одного типа: «{kind}». Отнеси каждую историю к одному почерку из перечня.

Правила:
- выбирай почерк, только если способ действия действительно тот же; если не подходит ни один — ставь 0 («прочее»);
- если несколько историй из этого списка описывают один и тот же новый приём, которого нет в перечне, предложи новый
  почерк в "new" и ссылайся на него как "new1", "new2"; одну необычную историю в новый почерк не выделяй;
- суди по смыслу описания и пересказа, а не по отдельным словам;
- name нового почерка — 3–8 слов простым языком, без слов «мошенничество», «обман», «сговор», «кража»; description
  начинается со слов «Клиенты пишут, что».

Верни JSON: {"items": [{"n": 1, "motif": 3}, {"n": 2, "motif": 0}, {"n": 3, "motif": "new1"}, ...],
"new": [{"id": "new1", "name": "...", "description": "...", "benign": "...", "ops": "...", "metric": "..."}]}
Отвечай строго JSON."""


def norm_label(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip(" .«»\"").lower().replace("ё", "е"))[:160]


def other_id(kind: int) -> str:
    return f"m{int(kind):02d}00"


def load(store: Store) -> tuple[dict[str, dict], dict[tuple, str]]:
    """Каталог почерков и соответствие «(тип, описание) → почерк»."""
    meta.create_all(store.engine, tables=[motif_t, motif_label])
    with store.engine.connect() as c:
        ms = {r["motif_id"]: dict(r) for r in c.execute(select(motif_t)).mappings()}
        lab = {(k, l): m for k, l, m in c.execute(select(motif_label.c.kind, motif_label.c.label, motif_label.c.motif_id))}
    return ms, lab


def mapper(labels: dict[tuple, str]):
    """Функция для risk.stories_from: (rid, тип, описание) → id почерка или None."""
    def motif_of(_rid, kind, label):
        return labels.get((kind, norm_label(label))) if kind and label else None
    return motif_of


def _pending(store: Store, labels: dict[tuple, str]) -> dict[int, dict[str, dict]]:
    """Описания тяжёлых историй, ещё не отнесённые к почеркам: тип → описание → {n, episode}."""
    out: dict[int, dict[str, dict]] = defaultdict(dict)
    with store.engine.connect() as c:
        ep = {rid: s or "" for rid, s in c.execute(select(review_llm.c.review_id, review_llm.c.summary).where(
            review_llm.c.prompt_version == PROMPT_VERSION, review_llm.c.relevant == 1, review_llm.c.severity >= 2))}
        for rid, dj in c.execute(select(review_llm.c.review_id, review_llm.c.data_json).where(
                review_llm.c.prompt_version == DETAIL_VERSION)):
            try:
                d = json.loads(dj or "{}")
            except ValueError:
                continue
            kind, label = d.get("kind"), norm_label(d.get("label"))
            if not kind or kind in BACKGROUND or not label or rid not in ep:
                continue
            if (kind, label) in labels:
                continue
            x = out[kind].setdefault(label, {"n": 0, "episode": ep[rid]})
            x["n"] += 1
    return out


def _clean(m: dict) -> dict | None:
    name = str(m.get("name") or "").strip(" .«»\"")
    if not (5 <= len(name) <= 120) or FORBIDDEN.search(name):
        return None
    desc = str(m.get("description") or "").strip()
    if FORBIDDEN.search(desc):
        desc = ""
    return {"name": name[0].upper() + name[1:], "description": desc[:400],
            "benign": str(m.get("benign") or "").strip(" .")[:300], "ops": str(m.get("ops") or "").strip(" .")[:400],
            "metric": str(m.get("metric") or "").strip(" .")[:300]}


def _next_id(kind: int, have: set) -> str:
    n = 1
    while f"m{kind:02d}{n:02d}" in have:
        n += 1
    return f"m{kind:02d}{n:02d}"


async def build(store: Store, *, cap_rub: float | None = None, reader: Reader | None = None,
                kinds: list[int] | None = None) -> dict:
    """kinds — только эти типы историй (проба на одном-двух типах перед полным прогоном)."""
    cap = CAP_RUB if cap_rub is None else cap_rub
    ms, labels = load(store)
    pend = _pending(store, labels)
    stats = Counter()
    spent = {"rub": 0.0, "calls": 0}
    now = utcnow()

    def rdr(system: str) -> Reader:
        """Свой читатель на тип: подсказка у типа одна, и расход держится под лимитом токенов в минуту."""
        if reader is not None:
            reader.system = system
            return reader
        return Reader(MOTIF_MODEL, system)

    async def ask(rd: Reader, user: str):
        d, rub = await rd.ask(user)
        spent["rub"] += rub
        spent["calls"] += 1
        return d if isinstance(d, dict) else {}

    def save_motifs(rows: list[dict]) -> None:
        if rows:
            with store.engine.begin() as c:
                c.execute(motif_t.insert(), rows)
            for r in rows:
                ms[r["motif_id"]] = r

    def save_labels(rows: list[dict]) -> None:
        if rows:
            with store.engine.begin() as c:
                c.execute(motif_label.insert(), rows)
            for r in rows:
                labels[(r["kind"], r["label"])] = r["motif_id"]

    for kind in sorted(pend):
        todo = pend[kind]
        if not todo or kind not in KINDS or (kinds and kind not in kinds):
            continue
        if other_id(kind) not in ms:
            save_motifs([{"motif_id": other_id(kind), "kind": kind, "name": f"Прочее: {kind_name(kind).lower()}",
                          "description": "", "benign": "", "ops": "", "metric": "", "created_at": now, "updated_at": now}])
        mine = [m for m in ms.values() if m["kind"] == kind and m["motif_id"] != other_id(kind)]
        # 1) каталог для типа, у которого его ещё нет
        if not mine and len(todo) >= PROPOSE_MIN and spent["rub"] < cap:
            sample = sorted(todo.items(), key=lambda kv: -kv[1]["n"])[:PROPOSE_SAMPLE]
            d = await ask(rdr(SYSTEM_PROPOSE.replace("{kind}", kind_name(kind))),
                          "Тип: " + kind_name(kind) + "\nОписания (в скобках — сколько раз встретилось):\n" +
                          "\n".join(f"- {l} ({x['n']})" for l, x in sample))
            got = [x for x in (_clean(m) for m in (d.get("motifs") or []) if isinstance(m, dict)) if x][:14]
            have = set(ms)
            rows = []
            for g in got:
                mid = _next_id(kind, have)
                have.add(mid)
                rows.append({"motif_id": mid, "kind": kind, **g, "created_at": now, "updated_at": now})
            save_motifs(rows)
            stats["proposed"] += len(rows)
            mine = [m for m in ms.values() if m["kind"] == kind and m["motif_id"] != other_id(kind)]
        if not mine:                                     # описаний мало — пока всё в «прочее»
            save_labels([{"kind": kind, "label": l, "motif_id": other_id(kind), "at": now} for l in todo])
            stats["other"] += len(todo)
            continue
        # 2) каждое описание — к почерку каталога, к «прочему» или к новому почерку
        items = list(todo.items())
        rd_k = rdr(SYSTEM_ASSIGN.replace("{kind}", kind_name(kind)))

        async def one(batch: list[tuple[str, dict]], kind: int = kind, rd_k: Reader = rd_k) -> None:
            if spent["rub"] >= cap:
                return
            cat = sorted((m for m in ms.values() if m["kind"] == kind and m["motif_id"] != other_id(kind)),
                         key=lambda m: m["motif_id"])
            user = ("Перечень почерков:\n" + "\n".join(f"{i}. {m['name']} — {m['description']}" for i, m in enumerate(cat, 1)) +
                    "\n\nИстории:\n" + "\n".join(f"[{i}] {l} | Пересказ: {x['episode'][:260]}"
                                                 for i, (l, x) in enumerate(batch, 1)))
            d = await ask(rd_k, user)
            new = {}
            for m in d.get("new") or []:
                if isinstance(m, dict) and str(m.get("id", "")).startswith("new"):
                    c = _clean(m)
                    if c:
                        new[str(m["id"])] = c
            used_new = Counter(str(it.get("motif")) for it in (d.get("items") or []) if isinstance(it, dict))
            new_ids = {}
            for nid, c in new.items():
                if used_new[nid] < 2:                    # новый почерк — только если приём повторился
                    continue
                mid = _next_id(kind, set(ms))
                save_motifs([{"motif_id": mid, "kind": kind, **c, "created_at": utcnow(), "updated_at": utcnow()}])
                new_ids[nid] = mid
                stats["new"] += 1
            rows, done = [], set()
            for it in d.get("items") or []:
                if not isinstance(it, dict) or not str(it.get("n", "")).isdigit():
                    continue
                n = int(it["n"])
                if not (1 <= n <= len(batch)) or n in done:
                    continue
                mv = it.get("motif")
                if isinstance(mv, str) and mv in new_ids:
                    mid = new_ids[mv]
                elif str(mv).isdigit() and 1 <= int(mv) <= len(cat):
                    mid = cat[int(mv) - 1]["motif_id"]
                else:
                    mid = other_id(kind)
                done.add(n)
                rows.append({"kind": kind, "label": batch[n - 1][0], "motif_id": mid, "at": utcnow()})
            save_labels(rows)
            stats["assigned"] += len(rows)
            stats["missed"] += len(batch) - len(rows)    # модель пропустила — останутся на следующий запуск

        # первая пачка — отдельно: новые почерки из неё должны попасть в перечень для остальных
        batches = [items[i:i + ASSIGN_BATCH] for i in range(0, len(items), ASSIGN_BATCH)]
        if batches:
            await one(batches[0])
            await asyncio.gather(*(one(b) for b in batches[1:]))
    res = {"motifs": len([m for m in ms.values() if not m["motif_id"].endswith("00")]), "labels": len(labels),
           "rub": round(spent["rub"], 2), "calls": spent["calls"], **dict(stats)}
    log.info("почерки: %s", res)
    return res
