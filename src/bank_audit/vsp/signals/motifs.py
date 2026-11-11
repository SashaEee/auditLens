"""Почерки: узкие способы действия внутри типа истории — из самих историй, без закрытого списка.

Этап «уточнение» (llm_read) даёт каждой тяжёлой истории тип и короткое описание способа
действия своими словами. Здесь модель-редактор раскладывает эти описания на почерки:

  1) для типа без каталога — предлагает почерки по самым частым описаниям (не больше MAX_FIRST);
  2) каждое описание относит к почерку каталога или к «прочему» — пачками, параллельно;
  3) смотрит на «прочее» типа целиком — и на свежий остаток, и на то, что легло туда раньше: если там
     повторяется приём, которого нет в каталоге, добавляет почерк (не больше MAX_MORE за запуск) и
     относит «прочее» ещё раз. Так новый приём, который копится по одной истории за ночь, со временем
     становится почерком. Повторно «прочее» смотрится, только когда оно выросло на PROPOSE_MIN описаний
     с прошлого раза (счётчик в kv «motifs:more_seen»), — иначе платили бы за один и тот же ответ.

Кончился потолок трат — остаток уходит в «прочее», но счётчик не трогается: следующий запуск
вернётся к шагу 3 (первый полный прогон 11.11 упёрся в потолок, и восемь крупных типов остались
без этого шага).

Новые почерки появляются только на шаге 3, одним вызовом: когда их предлагали параллельные
пачки, каждая придумывала свой вариант одного и того же приёма (проба 10.11: 21 почерк на тип
вместо десятка, с дублями).

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
MAX_FIRST = 10             # почерков в первом каталоге типа
MAX_MORE = 3               # новых почерков за один запуск — из остатка
MAX_MOTIFS = 16            # почерков у типа всего: мельче — уже не почерк, а отдельные истории
CAP_RUB = float(os.getenv("VSP_LLM_MOTIF_CAP_RUB", "300"))
FORBIDDEN = re.compile(r"мошенни|обман|сговор|воровств|кража|афер", re.I)

SYSTEM_PROPOSE = """Ты — руководитель внутреннего аудита розничного банка. Тебе дают короткие описания того, что, по
словам клиентов, сделали сотрудники отделений. Все описания одного типа: «{kind}». Раздели их на узкие «почерки» —
способы действия, которые повторяются у разных клиентов.

Правила:
- почерк — это конкретный приём («готовую кредитную карту выносят вместе с дебетовой»), а не тема («навязывание карт»);
- от 3 до {max} почерков. Почерк должен встречаться часто: не меньше чем в трёх описаниях из ста. Редкие и разнородные
  описания в почерки не выделяй — они пойдут в «прочее»;
- почерки не должны пересекаться: если два приёма различаются только названием продукта или поводом визита, это один
  почерк;
- name — 3–8 слов простым русским языком. Это слова клиентов, вина не установлена: не пиши «мошенничество», «обман»,
  «сговор», «кража»;
- description — одно предложение, начинается со слов «Клиенты пишут, что»;
- benign — чем это может объясняться безобидно (одна фраза);
- ops — какие операции запросить из данных банка, чтобы проверить именно этот приём (одна фраза: какие операции и по
  какому признаку их отобрать). Начни с существительного: «договоры…», «заявки…», «операции…»;
- metric — какой показатель подтвердит или снимет вопрос (одна фраза).

Верни JSON: {"motifs": [{"name": "...", "description": "...", "benign": "...", "ops": "...", "metric": "..."}, ...]}
Отвечай строго JSON."""

SYSTEM_MORE = """Ты — руководитель внутреннего аудита розничного банка. Для типа историй «{kind}» уже есть перечень
«почерков» — узких способов действия сотрудников отделений. Ниже — описания, которые не подошли ни к одному почерку.
Посмотри, повторяется ли среди них ещё какой-нибудь приём.

Правила:
- предлагай новый почерк, только если один и тот же приём виден не меньше чем в пяти описаниях из списка;
- не больше {max} новых почерков; не повторяй и не дроби почерки из перечня;
- если повторяющихся приёмов нет — верни пустой список, это нормальный ответ;
- поля те же: name (3–8 слов простым языком, без слов «мошенничество», «обман», «сговор», «кража»), description
  (начинается со слов «Клиенты пишут, что»), benign, ops (какие операции запросить и по какому признаку отобрать;
  начни с существительного), metric.

Верни JSON: {"motifs": [{"name": "...", "description": "...", "benign": "...", "ops": "...", "metric": "..."}, ...]}
Отвечай строго JSON."""

SYSTEM_ASSIGN = """Ты — руководитель внутреннего аудита розничного банка. Есть перечень «почерков» — узких способов
действия сотрудников отделений. Все они одного типа: «{kind}». Отнеси каждую историю к одному почерку из перечня.

Правила:
- выбирай почерк, только если способ действия действительно тот же; если не подходит ни один — ставь 0 («прочее»);
- суди по смыслу описания и пересказа, а не по отдельным словам;
- новых почерков не придумывай.

Верни JSON: {"items": [{"n": 1, "motif": 3}, {"n": 2, "motif": 0}, ...]} — по объекту на каждую историю.
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
        lab = {(k, lb): m for k, lb, m in c.execute(select(motif_label.c.kind, motif_label.c.label, motif_label.c.motif_id))}
    return ms, lab


def mapper(labels: dict[tuple, str]):
    """Функция для risk.stories_from: (rid, тип, описание) → id почерка или None."""
    def motif_of(_rid, kind, label):
        return labels.get((kind, norm_label(label))) if kind and label else None
    return motif_of


def _descriptions(store: Store) -> dict[int, dict[str, dict]]:
    """Описания способа действия у тяжёлых историй: тип → описание → {n, episode}."""
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
    descr = _descriptions(store)
    pend = {k: {lb: x for lb, x in d.items() if (k, lb) not in labels} for k, d in descr.items()}
    seen = dict(store.kv_get("motifs:more_seen") or {})       # тип → сколько было в «прочем», когда его смотрели
    stats = Counter()
    spent = {"rub": 0.0, "calls": 0}
    now = utcnow()

    # один клиент на весь запуск: типы идут параллельно, а расход токенов в минуту считается общий
    rd = reader or Reader(MOTIF_MODEL, "")

    async def ask(system: str, user: str):
        d, rub = await rd.ask(user, system=system)
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
        """Новое описание — строкой; описание из «прочего», которому нашёлся почерк, — переносом."""
        new = [r for r in rows if (r["kind"], r["label"]) not in labels]
        moved = [r for r in rows if (r["kind"], r["label"]) in labels and labels[(r["kind"], r["label"])] != r["motif_id"]]
        if new or moved:
            with store.engine.begin() as c:
                if new:
                    c.execute(motif_label.insert(), new)
                for r in moved:
                    c.execute(motif_label.update().where(motif_label.c.kind == r["kind"], motif_label.c.label == r["label"])
                              .values(motif_id=r["motif_id"], at=r["at"]))
            for r in new + moved:
                labels[(r["kind"], r["label"])] = r["motif_id"]
            stats["moved"] += len(moved)

    def in_other(kind: int) -> list[tuple[str, dict]]:
        oid = other_id(kind)
        return [(lb, x) for lb, x in descr.get(kind, {}).items() if labels.get((kind, lb)) == oid]

    def catalog(kind: int) -> list[dict]:
        return sorted((m for m in ms.values() if m["kind"] == kind and m["motif_id"] != other_id(kind)),
                      key=lambda m: m["motif_id"])

    def add_motifs(kind: int, raw, limit: int) -> int:
        """Сохранить предложенные почерки: без запретных слов, без повторов названий, не больше limit."""
        have_names = {m["name"].lower() for m in catalog(kind)}
        rows, have = [], set(ms)
        for g in (x for x in (_clean(m) for m in (raw or []) if isinstance(m, dict)) if x):
            if g["name"].lower() in have_names or len(rows) >= limit:
                continue
            mid = _next_id(kind, have)
            have.add(mid)
            have_names.add(g["name"].lower())
            rows.append({"motif_id": mid, "kind": kind, **g, "created_at": utcnow(), "updated_at": utcnow()})
        save_motifs(rows)
        return len(rows)

    async def assign(kind: int, items: list[tuple[str, dict]], system: str) -> list[tuple[str, dict]]:
        """Отнести описания к каталогу. Возвращает те, что модель отправила в «прочее» (0) — их ещё не сохраняем:
        остаток посмотрим на новые почерки. Пропущенные моделью остаются на следующий запуск."""
        cat = catalog(kind)
        head = "Перечень почерков:\n" + "\n".join(f"{i}. {m['name']} — {m['description']}" for i, m in enumerate(cat, 1))
        left: list[tuple[str, dict]] = []

        async def one(batch: list[tuple[str, dict]]) -> None:
            if spent["rub"] >= cap:
                return
            d = await ask(system, head + "\n\nИстории:\n" + "\n".join(
                f"[{i}] {lb} | Пересказ: {x['episode'][:260]}" for i, (lb, x) in enumerate(batch, 1)))
            rows, done = [], set()
            for it in d.get("items") or []:
                if not isinstance(it, dict) or not str(it.get("n", "")).isdigit():
                    continue
                n = int(it["n"])
                if not (1 <= n <= len(batch)) or n in done:
                    continue
                done.add(n)
                mv = it.get("motif")
                if str(mv).isdigit() and 1 <= int(mv) <= len(cat):
                    rows.append({"kind": kind, "label": batch[n - 1][0], "motif_id": cat[int(mv) - 1]["motif_id"],
                                 "at": utcnow()})
                else:
                    left.append(batch[n - 1])
            save_labels(rows)
            stats["assigned"] += len(rows)
            stats["missed"] += len(batch) - len(done)       # модель пропустила — останутся на следующий запуск

        await asyncio.gather(*(one(items[i:i + ASSIGN_BATCH]) for i in range(0, len(items), ASSIGN_BATCH)))
        return left

    async def do_kind(kind: int) -> None:
        todo = list(pend.get(kind, {}).items())
        if other_id(kind) not in ms:
            save_motifs([{"motif_id": other_id(kind), "kind": kind, "name": f"Прочее: {kind_name(kind).lower()}",
                          "description": "", "benign": "", "ops": "", "metric": "", "created_at": now, "updated_at": now}])
        name = kind_name(kind)
        was = int(seen.get(str(kind), 0))
        tried = False
        left = todo
        if not catalog(kind):
            # 1) каталог для типа, у которого его ещё нет: по новым описаниям и по тому, что уже лежит в «прочем»
            pool = in_other(kind) + todo
            if len(pool) >= PROPOSE_MIN and len(pool) - was >= PROPOSE_MIN and spent["rub"] < cap:
                sample = sorted(pool, key=lambda kv: -kv[1]["n"])[:PROPOSE_SAMPLE]
                d = await ask(SYSTEM_PROPOSE.replace("{kind}", name).replace("{max}", str(MAX_FIRST)),
                              "Тип: " + name + "\nОписания (в скобках — сколько раз встретилось):\n" +
                              "\n".join(f"- {lb} ({x['n']})" for lb, x in sample))
                stats["proposed"] += add_motifs(kind, d.get("motifs"), MAX_FIRST)
                tried = True
            if catalog(kind):
                left = await assign(kind, pool, SYSTEM_ASSIGN.replace("{kind}", name))
        elif todo:
            # 2) каждое новое описание — к почерку каталога или в остаток
            left = await assign(kind, todo, SYSTEM_ASSIGN.replace("{kind}", name))
        # 3) «прочее» типа вместе со свежим остатком: повторяется ли там приём, которого нет в каталоге
        pool = list(dict(in_other(kind) + left).items())
        room = min(MAX_MORE, MAX_MOTIFS - len(catalog(kind)))
        if catalog(kind) and room > 0 and len(pool) >= PROPOSE_MIN and len(pool) - was >= PROPOSE_MIN and spent["rub"] < cap:
            sample = sorted(pool, key=lambda kv: -kv[1]["n"])[:PROPOSE_SAMPLE]
            d = await ask(SYSTEM_MORE.replace("{kind}", name).replace("{max}", str(room)),
                          "Перечень почерков:\n" + "\n".join(f"- {m['name']} — {m['description']}" for m in catalog(kind)) +
                          "\n\nОписания, которые не подошли (в скобках — сколько раз встретилось):\n" +
                          "\n".join(f"- {lb} ({x['n']})" for lb, x in sample))
            added = add_motifs(kind, d.get("motifs"), room)
            stats["new"] += added
            tried = True
            if added:
                left = await assign(kind, pool, SYSTEM_ASSIGN.replace("{kind}", name))
        fresh = [lb for lb, _ in left if (kind, lb) not in labels]
        save_labels([{"kind": kind, "label": lb, "motif_id": other_id(kind), "at": utcnow()} for lb in fresh])
        stats["other"] += len(fresh)
        if tried and spent["rub"] < cap:                 # на потолке трат раскладка могла оборваться — вернёмся
            seen[str(kind)] = len(in_other(kind))

    def due(kind: int) -> bool:
        """Есть новые описания или «прочее» типа выросло настолько, что его пора пересмотреть."""
        return bool(pend.get(kind)) or len(in_other(kind)) - int(seen.get(str(kind), 0)) >= PROPOSE_MIN

    await asyncio.gather(*(do_kind(k) for k in sorted(KINDS)
                           if k not in BACKGROUND and due(k) and not (kinds and k not in kinds)))
    if seen != (store.kv_get("motifs:more_seen") or {}):
        store.kv_set("motifs:more_seen", seen)
    res = {"motifs": len([m for m in ms.values() if not m["motif_id"].endswith("00")]), "labels": len(labels),
           "rub": round(spent["rub"], 2), "calls": spent["calls"], **dict(stats)}
    log.info("почерки: %s", res)
    return res
