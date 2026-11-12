"""Долгое ожидание в отделениях: жалобы клиентов на очереди и время до приёма.

Тяжёлые истории (llm_read) очереди намеренно пропускают: это не поступок сотрудника. Но ожидание —
отдельный вопрос аудита сети: где клиенты ждут дольше всего, почему и ушли ли без обслуживания.
Поэтому у него свой этап чтения и свой раздел вкладки.

Отбор в два шага. Слова («очередь», «ждал», «талон», «одно окно»…) находят кандидатов среди жалоб
(оценка 1–3) — по одним словам считать нельзя: в выборке 12.11 по делу оказалось около двух третей
(«взял талон — нахамили» — не про время). Каждого кандидата читает дешёвая модель и отвечает: это
жалоба на ожидание в отделении или нет, сколько клиент ждал по его словам, причина, ушёл ли, цитата.
Ответ лежит в общей таблице чтения (vsp_review_llm) под своей версией — прочитанное не перечитывается.

Запуск: python -m bank_audit.vsp wait [--limit N] [--cap-rub N]; в демоне — шаг после чтения историй.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, select

from ..models import utcnow
from ..storage import Store, branches, meta, reviews, source_branches
from . import llm_read as lr
from .stats import bh, binom_tail

log = logging.getLogger(__name__)
UTC = timezone.utc

VERSION = "w1"
SINCE = os.getenv("VSP_WAIT_SINCE", "2024-01-01")          # читаем жалобы не старше: разделу нужны последние годы
MODEL = os.getenv("VSP_WAIT_MODEL", lr.SCREEN_MODEL)
BATCH = int(os.getenv("VSP_WAIT_BATCH", "20"))
CAP_RUB = float(os.getenv("VSP_WAIT_CAP_RUB", "300"))
TEXT_LIMIT = 2000

# Слова-кандидаты. Широко: модель отсеет лишнее, а пропущенное словами уже никто не прочитает.
# Без границ слов — один и тот же шаблон понимают и Python, и PostgreSQL.
WORDS = ("очеред|ожидан|ожидал|ждал|ждали|ждать|ждем|ждём|жду |прожда|дожда|заждал|просид|просто[яи]|отсид|отстоя"
         "|сижу|сидим|стою |талон|одно окно|одна касса|работает одн|работают дв|медленн|долго|долгое|часами"
         "|по записи")
RX = re.compile(WORDS, re.I)

CAUSES = {
    "мало окон": "Мало окон или сотрудников",
    "электронная очередь": "Талоны и электронная очередь",
    "без очереди": "Других принимают без очереди",
    "медленно обслуживают": "Медленно обслуживают",
    "заняты своим": "Сотрудники заняты не клиентами",
    "касса или проверка": "Касса, инкассация, проверка",
    "много людей": "Много клиентов",
    "не сказано": "Причина не названа",
}

SYSTEM = """Ты — аудитор розничного банка, читаешь отзывы клиентов об отделениях банка. Для каждого отзыва определи,
жалуется ли клиент на долгое ожидание или очередь именно в отделении: ждал приёма, сидел или стоял в очереди, не
дождался, работает мало окон, талоны и электронная очередь не помогают попасть к сотруднику, по записи приняли с
большим опозданием, сотрудники заняты своими делами, пока клиенты ждут, один сотрудник занимался клиентом непомерно
долго («оператор работала очень медленно, пробыл два часа», «обслуживают очень медленно»).

Не считай жалобой на ожидание:
- очередь только к банкомату или терминалу самообслуживания;
- долгие сроки по продукту: карту делают месяц, перевод идёт неделю, заявку или обращение рассматривают долго,
  деньги не возвращают;
- ожидание на телефонной линии, в чате, в приложении, у курьера, очереди в других организациях;
- похвалу и нейтральные слова: «очередей нет», «ждать не пришлось», «ждала недолго», «небольшая очередь»;
- талон или очередь упомянуты без жалобы на время («взял талон, подошёл к окну — нахамили»);
- ожидание до десяти минут, названное мимоходом, когда жалоба совсем о другом;
- отделение закрыто, не работает по графику, закрылось по тревоге, отказали в услуге — если о времени ожидания
  приёма ничего нет.

Пиши только то, что есть в отзыве. Каждый отзыв оценивай отдельно.

Для каждого отзыва верни объект:
{"n": номер отзыва,
 "wait": true|false,
 "minutes": сколько клиент ждал или сколько его обслуживали по его словам, числом в минутах: «полтора часа» → 90,
   «около часа» → 60, «полчаса» → 30, «больше часа» → 60, «более 40 минут» → 40, «20–30 минут» → 30;
   null — если время не названо. Если названо несколько отрезков — самый долгий. Время до закрытия отделения и
   время дня — не ожидание;
 "cause": главная причина по словам клиента, одно значение:
   "мало окон" — работает одно-два окна, не хватает сотрудников, окна закрыты;
   "электронная очередь" — сбой талонов, вызывают не по порядку, талон пропал или не тот, не знают, какой талон брать;
   "без очереди" — других пропускают вперёд, знакомых принимают без талона;
   "медленно обслуживают" — сотрудник долго оформляет, не знает, как сделать операцию;
   "заняты своим" — сотрудники разговаривают между собой, сидят в телефоне, ушли на перерыв, а клиенты ждут;
   "касса или проверка" — касса закрыта на инкассацию, в кассе нет денег, ждали проверку операции или службу
     безопасности;
   "много людей" — наплыв клиентов, отделение одно на район, так всегда;
   "не сказано" — причина не названа;
 "left": true — клиент ушёл, не дождавшись, или его так и не обслужили; иначе false,
 "quote": дословный фрагмент отзыва до 200 символов именно про ожидание — буква в букву, с теми же ошибками и
   знаками, что у клиента}
Если wait=false, остальные поля не заполняй.

Примеры:
«Из 15 окошек работает 4, снять деньги ждать полтора часа» → wait=true, minutes=90, cause="мало окон", left=false.
«Просидел в общей очереди с пенсионерами, так и не дождавшись, ушёл» → wait=true, minutes=null, cause="не сказано",
left=true.
«Ожидание более 40 минут, талоны, которые взяли позже, вызывают раньше» → wait=true, minutes=40,
cause="электронная очередь".
«Сотрудница сама вызывала, кого захотела, даже тех, кто пришёл после нас» → wait=true, cause="без очереди".
«Менеджер мило болтает с клиентом, пока другие ждут в очереди» → wait=true, cause="заняты своим".
«Ждали 40 минут кассира, чтобы положить деньги на счёт: инкассация» → wait=true, minutes=40,
cause="касса или проверка".
«Взял талон, пригласили к окну, а там сказали, что без паспорта не обслужат» → wait=false.
«Просидела в очереди минут десять, столько же у менеджера. Не пустили в туалет!» → wait=false.
«Карту жду уже третью неделю, в отделении разводят руками» → wait=false.
Отвечай строго JSON."""

TAIL = ('\n\nВерни JSON: {"items":[{"n":1,"wait":true|false,"minutes":…,"cause":"…","left":true|false,"quote":"…"}, '
        '...]} — по объекту на каждый отзыв, поле n обязательно.')


def card(x: dict, n: int) -> str:
    return f"[{n}] Оценка {x['rating']}★, {str(x['date'])[:10]}.\nОтзыв:\n<<<{(x['text'] or '')[:TEXT_LIMIT]}>>>"


def _minutes(v) -> int | None:
    if isinstance(v, bool) or v in (None, "", "null", "None"):
        return None
    try:
        m = int(round(float(str(v).replace(",", ".").strip())))
    except (TypeError, ValueError):
        return None
    return m if 1 <= m <= 24 * 60 else None


def normalize(d) -> dict | None:
    """Ответ модели → поля. Значения вне списков сбрасываются; без «wait» ответа нет — отзыв прочитают заново."""
    if not isinstance(d, dict) or "wait" not in d:
        return None
    w = lr._b(d.get("wait"))
    if not w:
        return {"wait": 0, "minutes": None, "cause": None, "left": 0, "quote": None}
    cause = str(d.get("cause") or "").strip().lower()
    return {"wait": 1, "minutes": _minutes(d.get("minutes")), "cause": cause if cause in CAUSES else "не сказано",
            "left": lr._b(d.get("left")), "quote": (str(d.get("quote") or "").strip()[:300] or None)}


def _since() -> datetime:
    return datetime.fromisoformat(SINCE).replace(tzinfo=UTC)


def candidates(store: Store, *, ids: list[str] | None = None) -> list[dict]:
    """Жалобы со словами об ожидании, которые модель ещё не читала. Одинаковые тексты — один раз,
    аккаунты-рассыльщики (десятки отзывов о банке) — мимо. Уже прочитанное отсеивает база."""
    meta.create_all(store.engine, tables=[lr.review_llm])
    t = lr.review_llm.alias()
    q = select(
        reviews.c.review_id, reviews.c.rating, reviews.c.review_date.label("date"),
        reviews.c.review_text.label("text"), reviews.c.author_ref,
    ).where(reviews.c.review_text.isnot(None), reviews.c.rating <= 3, reviews.c.review_date >= _since(),
            ~exists().where(t.c.review_id == reviews.c.review_id, t.c.prompt_version == VERSION))
    if ids is not None and len(ids) <= 5000:
        q = q.where(reviews.c.review_id.in_(list(ids)))
    if store.engine.dialect.name == "postgresql":
        q = q.where(reviews.c.review_text.op("~*")(WORDS))           # слова отсеивает база: тексты не тянем зря
    want = set(ids) if ids is not None else None
    with store.engine.connect() as c:
        rows = [dict(r) for r in c.execute(q).mappings()]
        rows = [r for r in rows if (want is None or r["review_id"] in want) and RX.search(r["text"] or "")
                and lr.long_enough(r["text"], r["rating"])]
        spam: set = set()
        authors = sorted({r["author_ref"] for r in rows if r["author_ref"]})
        for i in range(0, len(authors), 1000):
            spam |= {a for (a,) in c.execute(select(reviews.c.author_ref).where(
                reviews.c.author_ref.in_(authors[i:i + 1000]), reviews.c.review_text.isnot(None))
                .group_by(reviews.c.author_ref).having(func.count() >= lr.SPAM_AUTHOR_MIN))}
    out, seen = [], {}
    for r in rows:
        if r["author_ref"] in spam:
            continue
        h = lr._hash(lr._norm_text(r["text"]))
        if h in seen:
            seen[h]["_dups"].append(r["review_id"])
            continue
        r["_dups"] = []
        seen[h] = r
        out.append(r)
    out.sort(key=lambda r: str(r["date"] or ""), reverse=True)        # свежие — первыми
    return out


def unread(store: Store) -> int:
    return len(candidates(store))


async def run(store: Store, *, limit: int = 2000, ids: list[str] | None = None, model: str | None = None,
              cap_rub: float | None = None) -> dict:
    """Прочитать кандидатов моделью и сохранить ответы. Продолжает с места остановки."""
    found = candidates(store, ids=ids)
    items = found[:limit]
    if not items:
        return {"stage": "wait", "read_unique": 0, "waiting": 0, "saved": 0, "hits": 0, "rub": 0.0, "calls": 0,
                "errors": 0, "minutes": 0.0, "model": model or MODEL, "version": VERSION}
    rd = lr.Reader(model or MODEL, SYSTEM)
    cap = CAP_RUB if cap_rub is None else cap_rub
    t0 = time.monotonic()
    saved = hits = 0

    async def one(b):
        nonlocal saved, hits
        if rd.rub >= cap:
            return
        got = await rd.read_batch(b, tail=TAIL, norm=normalize, fmt=card)
        rows = []
        for x in b:
            v = got.get(x["review_id"])
            if not v:
                continue
            data = {k: v[k] for k in ("wait", "minutes", "cause", "left", "quote")}
            data["quote_ok"] = bool(v["wait"]) and lr.quote_ok(v.get("quote") or "", x["text"])
            hits += v["wait"]
            for rid in [x["review_id"], *x["_dups"]]:
                rows.append({"review_id": rid, "prompt_version": VERSION, "text_hash": lr._hash(x["text"]),
                             "model": rd.model, "relevant": v["wait"], "severity": None, "staff": v["cause"],
                             "summary": v["quote"], "scheme": str(v["minutes"]) if v["minutes"] else None,
                             "data_json": json.dumps(data, ensure_ascii=False),
                             "cost_rub": round(v["_cost"], 5) if rid == x["review_id"] else 0.0, "at": utcnow()})
        if rows:
            with store.engine.begin() as c:
                for r in rows:
                    c.execute(lr.review_llm.delete().where(lr.review_llm.c.review_id == r["review_id"],
                                                           lr.review_llm.c.prompt_version == VERSION))
                c.execute(lr.review_llm.insert(), rows)
            saved += len(rows)
            if saved // 2000 != (saved - len(rows)) // 2000:
                log.info("ожидание: прочитано %d отзывов, %.1f ₽", saved, rd.rub)
    await asyncio.gather(*(one(items[i:i + BATCH]) for i in range(0, len(items), BATCH)))
    res = {"stage": "wait", "read_unique": len(items), "waiting": max(0, len(found) - len(items)), "saved": saved,
           "hits": hits, "rub": round(rd.rub, 2), "calls": rd.calls, "errors": rd.errors,
           "minutes": round((time.monotonic() - t0) / 60, 1), "model": rd.model, "version": VERSION,
           "dropped_params": sorted(rd.drop)}
    log.info("ожидание: %s", res)
    return res


# ── дословная цитата ─────────────────────────────────────────────────────────

_AN = re.compile(r"[0-9a-zа-я]")


def _letters(t: str) -> tuple[str, list[int]]:
    """Только буквы и цифры (ё → е, нижний регистр) и место каждой в исходной строке."""
    out, pos = [], []
    for i, ch in enumerate(t or ""):
        c = ch.lower().replace("ё", "е")
        if _AN.match(c):
            out.append(c)
            pos.append(i)
    return "".join(out), pos


def excerpt(text: str, quote: str | None, limit: int = 280) -> str:
    """Фрагмент отзыва про ожидание — всегда из самого текста, буква в букву.

    Модель иногда «причёсывает» цитату: ставит пробел после запятой, правит регистр. Поэтому её строка —
    только указатель: то же место ищется в отзыве по буквам и цифрам, а показывается исходный текст.
    Место не нашлось — берётся предложение (или окно) вокруг первого слова об ожидании."""
    text = text or ""
    tl, pos = _letters(text)
    span = None
    for part in sorted(re.split(r"\s*(?:\.{3}|…)\s*", quote or ""), key=len, reverse=True):
        ql, _ = _letters(part)
        if len(ql) < 8:
            continue
        i = tl.find(ql)
        if i >= 0:
            span = (pos[i], pos[i + len(ql) - 1] + 1)
            break
    if span is None:
        m = RX.search(text)
        span = (m.start(), m.end()) if m else (0, 0)
    a, b = span
    b = min(b, a + limit)
    # до границ предложения, если помещается; иначе — окно вокруг найденного места
    left = max(text.rfind(c, 0, a) for c in ".!?\n") + 1
    ends = [x for x in (text.find(c, b) for c in ".!?\n") if x >= 0]
    right = min(ends) + 1 if ends else len(text)
    while right < len(text) and text[right] in ".!?…)»\"”":      # «…более часа (!)» — не обрывать на «(!»
        right += 1
    if right - left <= limit:
        a, b = left, right
    elif b - a < limit // 2:
        a, b = max(left, a - limit // 3), min(right, a + 2 * limit // 3)
    return _snap(text, a, b)


def _snap(text: str, a: int, b: int) -> str:
    """Не резать слово; многоточие — только там, где предложение оборвано."""
    while a > 0 and text[a - 1].isalnum():
        a -= 1
    while b < len(text) and text[b].isalnum():
        b += 1
    frag = " ".join(text[a:b].split())
    head = text[:a].rstrip()
    lead = "" if not head or head[-1] in ".!?…" else "… "
    trail = "" if not text[b:].strip() or frag.rstrip(")»\"”")[-1:] in ".!?…" else " …"
    return lead + frag + trail


# ── данные раздела: жалобы на ожидание и знаменатель ─────────────────────────

def point_of(branch_id, source: str, sbid: str) -> str:
    return f"b{branch_id}" if branch_id else f"{source}:{sbid}"


def _aware(d):
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    return d.replace(tzinfo=UTC) if isinstance(d, datetime) and d.tzinfo is None else d


def load(engine, since: datetime | None = None) -> dict:
    """Всё для раздела одним заходом: жалобы на ожидание (с текстом) и число жалоб по отделениям и месяцам.

    Знаменатель — жалобы с текстом (оценка 1–3), а не все отзывы: на площадках в разы выросло число
    пятёрок, и доля от всех отзывов падала бы сама собой. «Доля жалоб на ожидание среди жалоб отделения»
    от этого не зависит."""
    since = since or _since()
    bid = func.coalesce(reviews.c.branch_id, source_branches.c.branch_id)
    join = reviews.outerjoin(source_branches, (source_branches.c.source == reviews.c.source)
                             & (source_branches.c.source_branch_id == reviews.c.source_branch_id))
    month = (func.to_char(reviews.c.review_date, "YYYY-MM") if engine.dialect.name == "postgresql"
             else func.strftime("%Y-%m", reviews.c.review_date))
    with engine.connect() as c:
        hits = []
        q = select(reviews.c.review_id, reviews.c.source, reviews.c.source_branch_id, bid.label("bid"),
                   reviews.c.rating, reviews.c.review_date, reviews.c.review_text, reviews.c.source_url,
                   reviews.c.first_seen_at, lr.review_llm.c.data_json, lr.review_llm.c.at.label("read_at")
                   ).select_from(join.join(lr.review_llm, (lr.review_llm.c.review_id == reviews.c.review_id)
                                           & (lr.review_llm.c.prompt_version == VERSION))
                                 ).where(lr.review_llm.c.relevant == 1, reviews.c.review_date >= since)
        for r in c.execute(q).mappings():
            try:
                d = json.loads(r["data_json"] or "{}") or {}
            except ValueError:
                d = {}
            at = _aware(r["review_date"])
            hits.append({"rid": r["review_id"], "src": r["source"], "point": point_of(r["bid"], r["source"],
                                                                                    r["source_branch_id"]),
                         "rating": int(r["rating"]) if r["rating"] is not None else None, "at": at,
                         "text": r["review_text"] or "", "url": r["source_url"],
                         "seen_at": _aware(r["first_seen_at"]), "read_at": _aware(r["read_at"]),
                         "minutes": _minutes(d.get("minutes")), "cause": d.get("cause") if d.get("cause") in CAUSES
                         else "не сказано", "left": bool(d.get("left")), "quote": d.get("quote") or "",
                         "quote_ok": bool(d.get("quote_ok"))})
        neg: dict[str, Counter] = defaultdict(Counter)
        qn = select(reviews.c.source, reviews.c.source_branch_id, bid.label("bid"), month.label("m"),
                    func.count().label("n")).select_from(join).where(
            reviews.c.review_text.isnot(None), reviews.c.rating <= 3, reviews.c.review_date >= since
        ).group_by(reviews.c.source, reviews.c.source_branch_id, bid, month)
        for r in c.execute(qn).mappings():
            neg[point_of(r["bid"], r["source"], r["source_branch_id"])][r["m"]] += int(r["n"])
        unread_q = [reviews.c.review_text.isnot(None), reviews.c.rating <= 3, reviews.c.review_date >= since,
                    ~exists().where(lr.review_llm.c.review_id == reviews.c.review_id,
                                    lr.review_llm.c.prompt_version == VERSION)]
        if engine.dialect.name == "postgresql":
            waiting = c.execute(select(func.count()).select_from(reviews).where(
                *unread_q, reviews.c.review_text.op("~*")(WORDS))).scalar() or 0
        else:                                               # без серверных шаблонов слова проверяем сами
            waiting = sum(1 for (t,) in c.execute(select(reviews.c.review_text).where(*unread_q))
                          if RX.search(t or "") and lr.long_enough(t, 1))
        read = c.execute(select(func.count()).select_from(lr.review_llm).where(
            lr.review_llm.c.prompt_version == VERSION)).scalar() or 0
    # один клиент мог оставить тот же текст на двух площадках одного отделения — считаем один раз
    seen: set = set()
    uniq = []
    for h in sorted(hits, key=lambda x: x["at"]):
        k = (h["point"], lr._hash(lr._norm_text(h["text"])))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(h)
    uniq.sort(key=lambda x: x["at"], reverse=True)
    return {"hits": uniq, "neg": neg, "waiting": int(waiting), "read": int(read), "since": since,
            "loaded_at": datetime.now(UTC)}


def ym(d: datetime) -> str:
    return d.strftime("%Y-%m")


def month_from(now: datetime, days: int) -> str:
    """Первый месяц периода «за N дней»: счёт идёт целыми месяцами — так же, как знаменатель."""
    return ym(now - timedelta(days=days))


SIG_Q = 0.05          # доля ложных находок среди отмеченных отделений
SIG_MIN = 3           # меньше жалоб — отметку не ставим, даже если доля высокая
SIG_RATIO = 1.5       # и доля должна быть заметно выше сети


def in_period(hits: list[dict], m0: str | None) -> list[dict]:
    return [h for h in hits if m0 is None or ym(h["at"]) >= m0]


def neg_by_point(neg: dict[str, Counter], m0: str | None) -> dict[str, int]:
    out = {}
    for p, ms in neg.items():
        n = sum(v for m, v in ms.items() if m0 is None or m >= m0)
        if n:
            out[p] = n
    return out


def branch_table(hits: list[dict], neg: dict[str, Counter], *, m0: str | None, q: float = SIG_Q) -> dict:
    """Отделения за период (с месяца m0): жалобы на ожидание, жалобы всего, доля, отметка «значимо чаще сети».

    Отметка — точный биномиальный тест: из n жалоб отделения k про ожидание при доле p по остальной сети,
    с поправкой на число сравнений по всем отделениям, у которых за период была хоть одна жалоба
    (а не только по тем, где ожидание уже набралось, — иначе заметная часть отметок была бы шумом)."""
    k_pt: Counter = Counter()
    extra: dict[str, dict] = defaultdict(lambda: {"long": 0, "left": 0, "mins": [], "last": None, "causes": Counter()})
    for h in in_period(hits, m0):
        p = h["point"]
        k_pt[p] += 1
        e = extra[p]
        e["long"] += (h["minutes"] or 0) >= 60
        e["left"] += bool(h["left"])
        e["causes"][h["cause"]] += 1
        if h["minutes"]:
            e["mins"].append(h["minutes"])
        if e["last"] is None or h["at"] > e["last"]:
            e["last"] = h["at"]
    n_pt = neg_by_point(neg, m0)
    for p, k in k_pt.items():
        n_pt[p] = max(n_pt.get(p, 0), k)
    K, N = sum(k_pt.values()), sum(n_pt.values())
    share = K / N if N else 0.0
    pvals = {}
    for p, k in k_pt.items():
        n = n_pt[p]
        rest = (K - k) / (N - n) if N > n else share            # доля по остальной сети, без самого отделения
        pvals[p] = binom_tail(k, n, rest)
    sig = {p for p in bh(pvals, q, m=len(n_pt))
           if k_pt[p] >= SIG_MIN and share and k_pt[p] / n_pt[p] >= SIG_RATIO * share}
    rows = []
    for p, k in k_pt.items():
        e, n = extra[p], n_pt[p]
        mins = sorted(e["mins"])
        rows.append({"point": p, "n": k, "neg": n, "share": k / n if n else None,
                     "ratio": (k / n) / share if n and share else None, "sig": p in sig,
                     "long": e["long"], "left": e["left"], "median": mins[len(mins) // 2] if mins else None,
                     "last": e["last"], "cause": e["causes"].most_common(1)[0][0] if e["causes"] else None})
    rows.sort(key=lambda r: (-r["n"], -(r["share"] or 0)))
    return {"rows": rows, "total": K, "neg": N, "share": share, "tested": len(n_pt), "sig": len(sig)}
