"""Ежедневное письмо раздела «Аудит сети ВСП»: что нового за сутки.

Подписку человек включает сам — кнопкой «Письма» в шапке раздела или в настройках колокольчика.
Настройка лежит в профиле (app_user.prefs.vsp_mail): включено ли, по каким регионам, что присылать.

Письмо уходит в рабочие дни после 9:00 МСК (в понедельник — за выходные) и только если есть новое:
• тяжёлые истории клиентов — до пяти самых важных, остальные числом;
• жалобы на долгое ожидание — числа и до пяти примеров;
• способы действия, которые появились или стали расти.
Раздел открыт не всем, поэтому доступ проверяется при каждой отправке: закрыли раздел — письма
прекращаются. На некорпоративный адрес уходят только числа и ссылка (как во всех письмах AuditLens).

«Новое» считается выпусками (таблица vsp_mail_item, миграция 114): раз в рабочий день всё, чего ещё не
было в письмах, записывается в выпуск этого дня; подписчик получает выпуски после своего прошлого
письма. Так история не приходит дважды и не теряется, если модель прочитала отзыв позже.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from ..vsp.regions import BY_CODE, subject_name
from . import mail_templates as T
from . import vsp_api as V
from .auth import CurrentUser, get_current_user

log = logging.getLogger(__name__)
UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")
router = V.router

HOUR = int(os.getenv("VSP_MAIL_HOUR_MSK", "9"))
LAST_HOUR = int(os.getenv("VSP_MAIL_LAST_HOUR_MSK", "14"))   # позже утреннее письмо уже не шлём — войдёт в завтрашнее
FRESH_D = 14          # отзыв старше — в письмо как «новое» не идёт (собран или прочитан с опозданием)
FIRST_D = 3           # в самый первый выпуск попадает только самое свежее, а не всё за две недели
KEEP_D = 120
WINDOW_D = 7          # письмо не тянет выпуски старше недели
TOP = 5
TESTS_PER_DAY = 3
SEED = date(2000, 1, 1)
KIND = "vsp_daily"
NOTE = "Отзывы — слова клиентов, а не установленные факты. Письмо показывает, на что посмотреть, а не кто виноват."
PRIVATE_NOTE = ("Письмо пришло не на корпоративную почту, поэтому в нём только числа. Адреса отделений, "
                "слова клиентов и пересказы — в AuditLens.")

_ISSUE: dict = {"day": None}
_LOCK = threading.Lock()


def _md():
    from . import mail_delivery
    return mail_delivery


# ── настройка подписки ───────────────────────────────────────────────────────

def prefs_of(prefs: dict | None) -> dict:
    p = (prefs or {}).get("vsp_mail") or {}
    if not isinstance(p, dict):
        p = {}
    subs = [s for s in (p.get("subjects") or []) if isinstance(s, str) and s in BY_CODE]
    heavy, wait = p.get("heavy", True) is not False, p.get("wait", True) is not False
    return {"on": bool(p.get("on")) and (heavy or wait), "subjects": sorted(set(subs))[:100], "heavy": heavy,
            "wait": wait, "since": p.get("since") if isinstance(p.get("since"), str) else None}


def _user(username: str) -> dict:
    return _md()._one("SELECT prefs, display_name FROM app_user WHERE username = :u", {"u": username}) or {}


def state(username: str) -> dict:
    """Что показать в окне «Письма»: настройка, куда пойдёт письмо, когда было последнее."""
    md = _md()
    from . import mailer
    p = prefs_of(_user(username).get("prefs"))
    addr = md.address(username)
    last = None
    try:
        last = md._scalar("""SELECT max(sent_at) FROM app_mail_log WHERE username = :u AND kind = :k AND ok
                              AND coalesce(n_items, 0) > 0""", {"u": username, "k": KIND})
    except Exception:  # noqa: BLE001 — журнала писем может не быть (локальная база)
        pass
    return {**p, "email": addr["email"] if addr else None,
            "corporate": bool(addr) and md.is_corporate(addr["email"]),
            "active": bool(addr) and md._sendable(addr["source"]) and mailer.configured() and not mailer.paused(),
            "hour": HOUR, "last": last.isoformat() if isinstance(last, datetime) else None}


def save(username: str, patch: dict) -> dict:
    from . import userdata
    cur = prefs_of(_user(username).get("prefs"))
    new = {**cur}
    for k in ("on", "heavy", "wait"):
        if patch.get(k) is not None:
            new[k] = bool(patch[k])
    if patch.get("subjects") is not None:
        new["subjects"] = sorted({s for s in patch["subjects"] if isinstance(s, str) and s in BY_CODE})[:100]
    if not (new["heavy"] or new["wait"]):
        new["on"] = False
    if new["on"] and not cur["on"]:
        new["since"] = datetime.now(UTC).isoformat()
    userdata.update_prefs(username, {"vsp_mail": new})
    return state(username)


# ── выпуск дня ───────────────────────────────────────────────────────────────

def _place(p: dict | None) -> dict:
    p = p or {}
    return {k: p.get(k) for k in ("city", "address", "name", "gosb", "subject")}


_PICT = re.compile("[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def plain(s: str | None) -> str:
    """Текст для письма: без пиктограмм — почтовые программы рисуют их по-разному, а смысла они не несут."""
    return " ".join(_PICT.sub(" ", s or "").split())


def candidates(now: datetime) -> list[dict]:
    """Свежее, что может войти в выпуск: тяжёлые истории, жалобы на ожидание, способы действия."""
    from ..vsp.signals.risk import case_score
    from . import vsp_wait
    cut, first = now - timedelta(days=FRESH_D), now - timedelta(days=FIRST_D)
    out: list[dict] = []
    run = V._run()
    pts = run["points"]
    for x in V._stories(run):
        if x["_at"] < cut:
            break                                           # истории идут от свежих к старым
        if (x.get("severity") or 0) < 2 or x.get("copy_of") or x.get("dup_of") or x.get("elsewhere") or x.get("hq"):
            continue
        p = pts.get(x["point"]) or {}
        what = x.get("motif_name") or (x.get("kind_name") if x.get("kind_name") != "Другое" else None)
        money = x.get("money")
        out.append({"kind": "story", "rid": x["rid"], "recent": x["_at"] >= first,
                    "subject": x.get("subject") or p.get("subject"), "gosb": x.get("gosb") or p.get("gosb"),
                    "point": x["point"],
                    "payload": {**_place(p), "date": x["_at"].date().isoformat(), "severity": x.get("severity"),
                                "episode": plain(x.get("episode")), "what": what, "inside": bool(x.get("inside")),
                                "vulnerable": bool(x.get("vulnerable")),
                                "money": int(money) if isinstance(money, (int, float)) and 0 < money < V.MONEY_CAP
                                else None,
                                "score": case_score({**x, "at": x["_at"]}, now)[0]}})
    for h in vsp_wait.data()["hits"]:
        if h["at"] < cut:
            break
        p = pts.get(h["point"]) or {}
        out.append({"kind": "wait", "rid": h["rid"], "recent": h["at"] >= first, "subject": p.get("subject"),
                    "gosb": p.get("gosb"), "point": h["point"],
                    "payload": {**_place(p), "date": h["at"].date().isoformat(), "minutes": h["minutes"],
                                "cause": h["cause"], "left": bool(h["left"]),
                                "quote": T._clip(plain(h["excerpt"]), 220)}})
    for w in V._waves(run):
        if w["status"] not in ("new", "rising"):
            continue
        t = V._wave_text(w)
        out.append({"kind": "wave", "rid": w["key"], "recent": False, "subject": None, "gosb": None, "point": None,
                    "payload": {"id": w["id"], "name": w["name"], "status": w["status"], "text": t["text"],
                                "accent": t["accent"]}})
    return out


def plan_issue(cands: list[dict], known: set[tuple[str, str]], day: date) -> list[dict]:
    """Какие записи добавить и в какой выпуск. Первый выпуск по виду записей берёт только самое свежее:
    остальное помечается «уже было», иначе первое письмо пришло бы с двумя неделями разом."""
    seen_kinds = {k for k, _ in known}
    rows = []
    for c in cands:
        if (c["kind"], c["rid"]) in known:
            continue
        first = c["kind"] not in seen_kinds
        rows.append({**c, "day": day if (not first or c["recent"]) else SEED})
    return rows


def build_issue(now: datetime | None = None) -> dict:
    """Собрать выпуск сегодняшнего дня (один раз в день; повтор безопасен)."""
    now = now or datetime.now(UTC)
    day = now.astimezone(MSK).date()
    with _LOCK:
        if _ISSUE["day"] == day:
            return {"day": day, "added": 0, "cached": True}
        md = _md()
        cands = candidates(now)
        known = {(r["kind"], r["rid"]) for r in md._rows(
            "SELECT kind, rid FROM vsp_mail_item WHERE created_at > now() - make_interval(days => :d)", {"d": KEEP_D})}
        rows = plan_issue(cands, known, day)
        if rows:
            from sqlalchemy import text
            from .. import db
            with db.session() as s:
                s.execute(text("""INSERT INTO vsp_mail_item (kind, rid, day, subject, gosb, point, payload)
                                  VALUES (:kind, :rid, :day, :subject, :gosb, :point, CAST(:payload AS jsonb))
                                  ON CONFLICT (kind, rid) DO NOTHING"""),
                          [{"kind": r["kind"], "rid": r["rid"], "day": r["day"], "subject": r["subject"],
                            "gosb": r["gosb"], "point": r["point"],
                            "payload": json.dumps(r["payload"], ensure_ascii=False)} for r in rows])
        md._exec("DELETE FROM vsp_mail_item WHERE created_at < now() - make_interval(days => :d)", {"d": KEEP_D})
        _ISSUE["day"] = day
        added = sum(1 for r in rows if r["day"] == day)
        if added:
            log.info("[mail] выпуск ВСП %s: %d записей", day, added)
        return {"day": day, "added": added, "cached": False}


def issue_rows(after: date, day: date) -> list[dict]:
    rows = _md()._rows("""SELECT kind, rid, day, subject, gosb, point, payload FROM vsp_mail_item
                           WHERE day > :a AND day <= :d ORDER BY day DESC""",
                       {"a": max(after, day - timedelta(days=WINDOW_D)), "d": day})
    for r in rows:
        if isinstance(r["payload"], str):
            r["payload"] = json.loads(r["payload"])
    return rows


def _scope(subjects: list[str]) -> str:
    if not subjects:
        return "вся сеть"
    names = [subject_name(s) for s in subjects]
    return ", ".join(names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")


def _period(days: set, day: date) -> str:
    """За какое время письмо — по выпускам, которые в него вошли (а не по тому, давно ли было прошлое письмо)."""
    days = {d for d in days if isinstance(d, date)}
    if not days or days == {day}:
        return "за выходные" if day.weekday() == 0 else "за сутки"
    gap = (day - min(days)).days + 1
    return f"за {gap} {T._plural(gap, 'день', 'дня', 'дней')}"


def compose(rows: list[dict], p: dict, after: date, day: date) -> dict:
    """Содержание письма одного подписчика из записей выпусков: его регионы и то, что он просил."""
    subs = set(p["subjects"])
    mine = [r for r in rows if r["kind"] == "wave" or not subs or r.get("subject") in subs]
    stories = sorted((r for r in mine if r["kind"] == "story" and p["heavy"]),
                     key=lambda r: (-(r["payload"].get("score") or 0), -_ord(r["payload"].get("date"))))
    wait = [r for r in mine if r["kind"] == "wait" and p["wait"]]
    wait.sort(key=lambda r: (-int(bool(r["payload"].get("left"))), -(r["payload"].get("minutes") or 0),
                             -_ord(r["payload"].get("date"))))
    waves = [r for r in mine if r["kind"] == "wave" and p["heavy"]]
    by_pt = Counter(r["point"] for r in wait if r.get("point"))
    place = {r["point"]: r["payload"] for r in wait if r.get("point")}
    repeat = [{"point": pt, "n": n, **{k: place[pt].get(k) for k in ("city", "address", "name", "gosb")}}
              for pt, n in by_pt.most_common(3) if n >= 2]
    used = [r for r in mine if (r["kind"] == "wait" and p["wait"]) or (r["kind"] != "wait" and p["heavy"])]
    return {"day": day, "after": after, "period": _period({r.get("day") for r in used}, day),
            "scope": _scope(p["subjects"]),
            "stories": [{"rid": r["rid"], "point": r["point"], **r["payload"]} for r in stories[:TOP]],
            "n_stories": len(stories),
            "wait": [{"rid": r["rid"], "point": r["point"], **r["payload"]} for r in wait[:TOP]],
            "n_wait": len(wait), "n_wait_long": sum(1 for r in wait if (r["payload"].get("minutes") or 0) >= 60),
            "n_wait_left": sum(1 for r in wait if r["payload"].get("left")), "wait_repeat": repeat,
            "waves": [r["payload"] for r in waves[:3]], "n_waves": len(waves),
            "total": len(stories) + len(wait) + len(waves)}


def _ord(d: str | None) -> int:
    try:
        return date.fromisoformat(str(d)[:10]).toordinal()
    except (TypeError, ValueError):
        return 0


# ── шаблон письма ────────────────────────────────────────────────────────────

def _n(x) -> str:
    return f"{int(x):,}".replace(",", " ")


def _where(x: dict) -> str:
    """«Казань, ул. Баумана, 5 · ВСП 8610/0123 · ГОСБ 8610»."""
    addr = ", ".join(t for t in (x.get("city"), _addr(x.get("address"))) if t) or (x.get("name") or "Отделение")
    code = _code(x.get("name"))
    return " · ".join(t for t in (addr, code, f"ГОСБ {x['gosb']}" if x.get("gosb") else "") if t)


_STREET = re.compile(r"^(УЛ|ПР-?Т|ПР-Д|ПРОСП|Ш|ПЕР|Б-Р|БУЛЬВАР|НАБ|ПЛ|МКР|МИКРОРАЙОН|КВ-Л|ТУПИК|ПРОЕЗД|ТРАКТ|АЛЛЕЯ|ЛИН|ТЕР)"
                     r"(?![А-ЯЁа-яё])", re.I)
_ABBR = {"пр-т", "пр-д", "б-р", "кв-л", "мкр", "стр", "корп", "лит", "пом", "влд", "зд"}


def _addr(a: str | None) -> str:
    """Адрес реестра «100000, КРАСНОДАРСКИЙ КРАЙ, Г. КРАСНОДАР, УЛ. ЛЕНИНА, Д. 5» → «ул. Ленина, д. 5»:
    без индекса, региона и города (город стоит отдельно), обычным регистром — как в разделе."""
    parts = [x.strip() for x in (a or "").split("#")[0].split(",")]
    parts = [x for x in parts if x and not re.fullmatch(r"\d{6}", x)]
    st = next((i for i, x in enumerate(parts) if _STREET.match(x)), -1)
    t = ", ".join(parts[st:] if st >= 0 else parts[-2:])
    if t != t.upper():
        return t
    t = re.sub(r"\.(?=[А-ЯЁ])", ". ", t)                   # «УЛ.ЛЕНИНА» → «УЛ. ЛЕНИНА»: иначе слово не распознать

    def cap(w: str) -> str:
        m = re.fullmatch(r"([а-яё][а-яё-]*)(,?)", w)
        return w[0].upper() + w[1:] if m and len(m.group(1)) >= 3 and m.group(1) not in _ABBR else w
    return " ".join(cap(w) for w in t.lower().split(" "))


def _code(name: str | None) -> str:
    m = re.match(r"^(?:ВСП\s*)?№?\s*(\d{2,4}\s*/\s*\d{1,5})", (name or "").strip())
    return "ВСП " + m.group(1).replace(" ", "") if m else ""


def _minutes(m) -> str:
    if not m:
        return ""
    if m < 60:
        return f"{m} мин"
    h, r = divmod(int(m), 60)
    return f"{h} ч {r} мин" if r else f"{h} ч"


def _day(d: str | None) -> str:
    try:
        x = date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return ""
    return f"{x.day} {T._MON_FULL[x.month - 1]}"


def _item(head: str, meta: str, text_: str, url: str, cta: str, quote: bool = False) -> str:
    """Строка письма: место, что известно, слова или пересказ, ссылка."""
    body = (f'<div style="margin:6px 0 0;padding:10px 14px;background:{T.SOFT};border-left:3px solid {T.HAIR};'
            f'border-radius:0 10px 10px 0;{T._font(14, 21, T.INK)}">«{T._e(text_)}»</div>' if quote
            else f'<div style="margin:5px 0 0;{T._font(15, 22, T.INK)}">{T._e(text_)}</div>') if text_ else ""
    return (f'<tr><td style="padding:16px 0;border-top:1px solid {T.HAIR}">'
            f'<div style="{T._font(13, 18, T.INK, 600)}">{T._e(head)}</div>'
            + (f'<div style="margin:2px 0 0;{T._font(12, 17, T.INK3)}">{T._e(meta)}</div>' if meta else "")
            + body
            + f'<div style="margin:8px 0 0"><a href="{T._e(url)}" target="_blank" '
              f'style="{T._font(13, 18, T.LINK, 500, "text-decoration:none")}">{T._e(cta)}&nbsp;&rarr;</a></div>'
            + '</td></tr>')


def _more(text_: str, url: str) -> str:
    return T._p(f'<a href="{T._e(url)}" target="_blank" style="color:{T.LINK};text-decoration:none">{T._e(text_)}'
                f'&nbsp;&rarr;</a>', size=14, top=12)


def subject_line(d: dict, sample: bool = False) -> str:
    parts = []
    if d["n_stories"]:
        parts.append(f"{d['n_stories']} {T._plural(d['n_stories'], 'тяжёлая история', 'тяжёлые истории', 'тяжёлых историй')}")
    if d["n_wait"]:
        parts.append(f"{d['n_wait']} {T._plural(d['n_wait'], 'жалоба', 'жалобы', 'жалоб')} на ожидание")
    if d["n_waves"] and not parts:
        parts.append(f"{d['n_waves']} {T._plural(d['n_waves'], 'способ действия', 'способа действия', 'способов действия')} "
                     f"{T._plural(d['n_waves'], 'растёт', 'растут', 'растут')}")
    s = f"Сеть ВСП {d['period']}: " + (", ".join(parts) if parts else "нового нет")
    return ("Пример письма · " if sample else "") + s


def render(d: dict, now: datetime | None = None, name: str = "", private: bool = False,
           sample: bool = False) -> dict:
    """Письмо из собранного содержания. private — некорпоративный адрес: только числа и ссылка."""
    now = now or datetime.now(UTC)
    url = T.go_url("vsp", "vsp")
    off = T.go_url("vsp?mail=off", "vsp")
    setup = T.go_url("vsp?mail=1", "vsp")
    fn = T.first_name(name)
    subject = subject_line(d, sample)
    head = f"Что нового {d['period']}"
    sub = f"{T._weekday_title(now)} · {d['scope']}"
    kpis = []                                               # только то, что есть: «0 историй» в письме не нужно
    if d["n_stories"]:
        kpis.append((_n(d["n_stories"]), T._plural(d["n_stories"], "тяжёлая история", "тяжёлые истории",
                                                   "тяжёлых историй")))
    if d["n_wait"]:
        kpis.append((_n(d["n_wait"]), T._plural(d["n_wait"], "жалоба на долгое ожидание", "жалобы на долгое ожидание",
                                                "жалоб на долгое ожидание")))
    if d["n_wait_long"]:
        kpis.append((_n(d["n_wait_long"]), "ждали час и дольше, по словам клиентов"))
    if d["n_waves"] and len(kpis) < 3:
        kpis.append((_n(d["n_waves"]), T._plural(d["n_waves"], "способ действия появился или растёт",
                                                 "способа действия появились или растут",
                                                 "способов действия появились или растут")))
    body = (T._eyebrow(("Пример письма · " if sample else "") + "Аудит сети ВСП · ежедневное письмо", T.BRAND)
            + T._h1(head, sub))
    hello = (f"{fn}, доброе утро." if fn else "Доброе утро.") + " Это новое в отзывах об отделениях с прошлого письма."
    body += T._p(T._e(hello), color=T.INK3, size=14, top=12)
    if kpis:
        body += T._kpis(kpis)
    lines = [hello, "", f"{d['scope'][:1].upper() + d['scope'][1:]}.",
             *(f"{n} — {label}" for n, label in kpis), ""]
    if not private:
        if d["waves"]:
            body += T._rubric("Что идёт по сети")
            for w in d["waves"]:
                body += T._tbl(_item(w["text"], "", w["accent"][:1].upper() + w["accent"][1:],
                                     T.go_url(f"vsp?s=schemes&scheme={w['id']}", "vsp"), "Карточка способа действия")
                               ).replace("border-top:1px solid " + T.HAIR, "border-top:0", 1)
                lines += [f"• {w['text']} — {w['accent']}"]
            lines.append("")
        if d["stories"]:
            body += T._rubric(f"Тяжёлые истории · {d['n_stories']}")
            rows = ""
            for x in d["stories"]:
                tags = [t for t in (("очень тяжёлая" if (x.get("severity") or 0) >= 3 else "тяжёлая"),
                                    (x.get("what") or "").lower() or None,
                                    "по словам клиента, выгода — сотруднику или третьим лицам" if x.get("inside") else None,
                                    "пожилой или уязвимый клиент" if x.get("vulnerable") else None,
                                    f"{_n(x['money'])} ₽ — по словам клиента" if x.get("money") else None) if t]
                rows += _item(_where(x), " · ".join([_day(x.get("date"))] + tags), T._clip(x.get("episode"), 320),
                              T.go_url(f"vsp?story={x['rid']}", "vsp"), "Полный отзыв и проверка")
                lines += [f"• {_where(x)} — {_day(x.get('date'))}", f"  {T._clip(x.get('episode'), 320)}"]
            body += T._tbl(rows, "margin:0").replace("border-top:1px solid " + T.HAIR, "border-top:0", 1)
            if d["n_stories"] > len(d["stories"]):
                rest = d["n_stories"] - len(d["stories"])
                body += _more(f"Ещё {rest} {T._plural(rest, 'история', 'истории', 'историй')} — в разделе «Истории»",
                              T.go_url("vsp?s=stories&per=90", "vsp"))
            lines.append("")
        if d["wait"]:
            body += T._rubric(f"Долгое ожидание · {d['n_wait']}")
            facts = [t for t in (
                f"час и дольше ждали {d['n_wait_long']}" if d["n_wait_long"] else "",
                f"ушли, не дождавшись, — {d['n_wait_left']}" if d["n_wait_left"] else "") if t]
            if facts:
                t = "По словам клиентов: " + "; ".join(facts) + "."
                body += T._p(T._e(t), size=14, top=12)
                lines.append(t)
            rows = ""
            for x in d["wait"]:
                from ..vsp.signals.wait import CAUSES
                meta = [_day(x.get("date")),
                        f"ждал {_minutes(x.get('minutes'))}, по словам клиента" if x.get("minutes") else None,
                        CAUSES.get(x.get("cause") or "", "").lower() if x.get("cause") != "не сказано" else None,
                        "ушёл, не дождавшись" if x.get("left") else None]
                rows += _item(_where(x), " · ".join(t for t in meta if t), x.get("quote") or "",
                              T.go_url(f"vsp?s=wait&point={x['point']}", "vsp"), "Досье отделения", quote=True)
                lines += [f"• {_where(x)} — {' · '.join(t for t in meta if t)}", f"  «{x.get('quote') or ''}»"]
            body += T._tbl(rows, "margin:0").replace("border-top:1px solid " + T.HAIR, "border-top:0", 1)
            for r in d["wait_repeat"]:
                t = f"{_where(r)}: {r['n']} {T._plural(r['n'], 'жалоба', 'жалобы', 'жалоб')} на ожидание {d['period']}"
                body += T._p(T._e(t), color=T.INK2, size=13, top=10)
                lines.append(t)
            if d["n_wait"] > len(d["wait"]):
                rest = d["n_wait"] - len(d["wait"])
                body += _more(f"Ещё {rest} {T._plural(rest, 'жалоба', 'жалобы', 'жалоб')} — в разделе «Ожидание»",
                              T.go_url("vsp?s=wait&per=90", "vsp"))
            lines.append("")
        body += T._p(T._e(NOTE), color=T.INK3, size=13, top=24)
        lines.append(NOTE)
    else:
        body += T._p(T._e(PRIVATE_NOTE), color=T.INK2, size=14, top=22)
        lines.append(PRIVATE_NOTE)
    body += T._btn("Открыть «Аудит сети ВСП»", url, top=28)
    reason = ("Вы получили это письмо, потому что запросили пример в разделе «Аудит сети ВСП»." if sample else
              f"Вы подписаны на ежедневное письмо раздела «Аудит сети ВСП». Оно приходит по будням после {HOUR}:00 "
              f"по Москве, только если есть новое.")
    pre = (T._clip(d["stories"][0]["episode"], 140) if d["stories"] and not private
           else f"{d['scope'][:1].upper() + d['scope'][1:]}: подробности в AuditLens")
    return {"subject": subject, "preheader": pre, "url": url,
            "html": T.layout(preheader=pre, title=subject, body=body, reason=reason, section="Аудит сети ВСП",
                             links=[("Настроить письмо", setup), ("Отписаться", off)]),
            "text": T._text("Аудит сети ВСП", head, lines, "Открыть раздел", url, reason)
            + f"Отписаться от этого письма: {off}\n",
            "thread": None}


# ── отправка ─────────────────────────────────────────────────────────────────

def due(now: datetime) -> bool:
    md = _md()
    msk = now.astimezone(MSK)
    return md._workday(msk.date()) and HOUR <= msk.hour < LAST_HOUR


def _last_day(username: str, day: date, skip_id: int | None = None, since: str | None = None) -> date:
    """После какого выпуска собирать письмо: после прошлого письма (и пустого «нечего отправлять» тоже),
    а если писем ещё не было — со дня подписки. Так день, когда письмо не ушло из-за сбоя почты, не теряется."""
    md = _md()
    last = md._scalar("""SELECT max(day) FROM app_mail_log WHERE username = :u AND kind = :k AND ok
                          AND day < :d AND id <> :s""", {"u": username, "k": KIND, "d": day, "s": skip_id or 0})
    if isinstance(last, date):
        return last
    try:
        start = datetime.fromisoformat(since).astimezone(MSK).date() if since else day
    except ValueError:
        start = day
    return min(start, day) - timedelta(days=1)


def daily(r: dict, now: datetime) -> int:
    """Письмо одному подписчику: раз в день, место в журнале занимаем до отправки."""
    md = _md()
    p = prefs_of(r.get("prefs"))
    if not p["on"] or not V.can_vsp(r["username"], r.get("display_name")):
        return 0                                             # раздел закрыли или подписку сняли — писем нет
    day = now.astimezone(MSK).date()
    slot = md._scalar("""INSERT INTO app_mail_log (username, kind, to_addr, day, ok, error)
                         VALUES (:u, :k, :e, :d, false, 'собирается')
                         ON CONFLICT (username, day) WHERE kind = 'vsp_daily' DO NOTHING
                         RETURNING id""", {"u": r["username"], "k": KIND, "e": r["email"], "d": day})
    if not slot:
        return 0
    try:
        build_issue(now)
        after = _last_day(r["username"], day, slot, p.get("since"))
        d = compose(issue_rows(after, day), p, after, day)
    except Exception:
        md._exec("DELETE FROM app_mail_log WHERE id = :id", {"id": slot})      # следующий тик попробует снова
        raise
    if not d["total"]:
        md._exec("UPDATE app_mail_log SET ok = true, error = 'нечего отправлять' WHERE id = :id", {"id": slot})
        return 0
    mail = render(d, now, r.get("display_name") or "", private=not md.is_corporate(r["email"]))
    try:
        md.deliver(r["username"], KIND, r["email"], mail, consented=r["source"] == "user", bulk=True, log_row=False)
    except Exception:
        md._exec("DELETE FROM app_mail_log WHERE id = :id", {"id": slot})
        raise
    md._exec("UPDATE app_mail_log SET ok = true, error = NULL, n_items = :n, sent_at = now() WHERE id = :id",
             {"n": d["total"], "id": slot})
    return 1


def sample_data(p: dict, now: datetime) -> dict:
    """Содержание для примера: последние выпуски, а если их ещё нет — свежее прямо из раздела."""
    day = now.astimezone(MSK).date()
    after = day - timedelta(days=4)
    rows: list[dict] = []
    try:
        rows = issue_rows(after, day)
    except Exception:  # noqa: BLE001 — таблицы выпусков ещё нет
        rows = []
    d = compose(rows, p, after, day)
    if not d["total"]:
        rows = [{**c, "day": day} for c in candidates(now) if c["recent"] or c["kind"] == "wave"]
        d = compose(rows, p, day - timedelta(days=FIRST_D), day)
    return d


def send_test(username: str, name: str = "") -> dict:
    """«Прислать пример»: то же письмо на свой адрес сейчас — не чаще трёх раз в день."""
    md = _md()
    addr = md.address(username)
    if not addr or not md._sendable(addr["source"]):
        raise md.MailUserError(400, "Сначала подключите почту: колокольчик → «Что присылать» → «На почту»")
    n = int(md._scalar("""SELECT count(*) FROM app_mail_log WHERE username = :u AND kind = 'vsp_test'
                           AND sent_at > now() - interval '1 day'""", {"u": username}) or 0)
    if n >= TESTS_PER_DAY:
        raise md.MailUserError(429, "Сегодня пример уже отправляли три раза — попробуйте завтра")
    now = datetime.now(UTC)
    p = {**prefs_of(_user(username).get("prefs")), "on": True}
    d = sample_data(p, now)
    mail = render(d, now, name, private=not md.is_corporate(addr["email"]), sample=True)
    from . import mailer
    try:
        md.deliver(username, "vsp_test", addr["email"], mail, consented=addr["source"] == "user", n=d["total"])
    except mailer.MailError as e:
        raise md.MailUserError(502, "Письмо не ушло — попробуйте ещё раз через минуту") from e
    return {"ok": True, "to": addr["email"], "items": d["total"]}


# ── маршруты ─────────────────────────────────────────────────────────────────

class MailIn(BaseModel):
    on: Optional[bool] = None
    subjects: Optional[list[str]] = None
    heavy: Optional[bool] = None
    wait: Optional[bool] = None


@router.get("/mail")
def mail_get(user: CurrentUser = Depends(get_current_user)):
    V._need(user)
    return state(user.username)


@router.put("/mail")
def mail_put(body: MailIn, user: CurrentUser = Depends(get_current_user)):
    V._need(user)
    return save(user.username, body.model_dump(exclude_none=True))


@router.post("/mail/test")
def mail_test(user: CurrentUser = Depends(get_current_user)):
    V._need(user)
    md = _md()
    try:
        row = _user(user.username)
        return send_test(user.username, row.get("display_name") or "")
    except md.MailUserError as e:
        raise HTTPException(e.status, str(e))
