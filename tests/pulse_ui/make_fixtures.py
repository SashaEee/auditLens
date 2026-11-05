"""Сборка фикстур стенда «Пульса» v2: tests/fixtures/pulse_v2/*.json.

Запуск из корня репозитория (перезаписывает JSON-файлы):

    DATABASE_URL=sqlite:///:memory: PYTHONPATH=src python -m tests.pulse_ui.make_fixtures

Форма каждого ответа повторяет код сервера: telemetry.metrics() / users_directory() /
user_card() / reports_all() / complaints(), inbox.admin_list() / admin_get() / brief(),
org_reach.report(), cloud_costs.summary(), outcomes.report() (сам расчёт — настоящим
outcomes._build), agent_eval.history(), review_slices.status(), banner.state().
Новые поля из CONTRACT §6 (health, dau[].off, features.ai_total_prev / reports_prev,
ai_feedback.by_day, retention.seg_of, у людей act14 / act_days / act_off) — уже здесь.

Сюжет — как в макете docs/design/pulse/pulse-v2.html: четверг 5 ноября 2026, 10:03 МСК,
64 человека, сегодня 23, вопросов ИИ-помощнику 41; три сбоя (выпуск без раздела «Тарифы
и ставка», Отзовик просел, две ошибки сервера) и четыре дела (3 обращения без ответа,
4 жалобы на ответы ИИ, двое из ядра перестали заходить, 2 заявки на источники).
Все имена вымышлены. Правила очереди (puGuardItems) дают ровно эти 3 + 4 пункта —
остальные пороги в данных намеренно не задеты (см. reviews_pipeline, reviews_search).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "fixtures" / "pulse_v2"

MSK = dt.timezone(dt.timedelta(hours=3))
NOW = dt.datetime(2026, 11, 5, 10, 3, tzinfo=MSK)
TODAY = NOW.date()
DAYS14 = [(TODAY - dt.timedelta(days=13 - i)).isoformat() for i in range(14)]
HOLIDAYS = {"2026-11-04"}
OFF14 = [i for i, d in enumerate(DAYS14)
         if dt.date.fromisoformat(d).weekday() >= 5 or d in HOLIDAYS]          # [1, 2, 8, 9, 12]

OWNER = ("oaleksandrov", "Олег Александров")       # владелец, помечен служебным
PULSE_USER = ("ngromova", "Надежда Громова")       # коллега с доступом к «Пульсу»
SERVICE = [("svc-monitor", "Мониторинг доступности"), ("qa-demo", "Демо-стенд")]


def iso(d: dt.datetime) -> str:
    return d.isoformat()


def at(day: str, hm: str) -> dt.datetime:
    h, m = map(int, hm.split(":"))
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(h, m), MSK)


def ddmm(d: dt.datetime) -> str:
    return d.strftime("%d.%m %H:%M")


def is_off(day: str) -> bool:
    return dt.date.fromisoformat(day).weekday() >= 5 or day in HOLIDAYS


# ── люди: как buildPeople() макета, счётчики сходятся с брифом ────────────────
# (логин, имя, сегмент 1 ядро/2 регулярные/3 разовые/4 ушли/5 давно не заходят,
#  активность 14 дней 0–3, «был(а)», вопросов ИИ, отчётов, оценки, почта, профиль %,
#  новичок, перестал заходить)
NAMED = [
    ("akovaleva", "Анна Ковалёва", 1, "30032332003303", "онлайн", 22, 5, "+3", "Sigma", 82),
    ("dorlov", "Дмитрий Орлов", 1, "20033233103313", "онлайн", 28, 6, "+2 −1", "Sigma", 74),
    ("svkuznetsova", "Светлана Кузнецова", 1, "20032232002303", "онлайн", 19, 4, "+2", "Sigma", 68),
    ("esmirnova", "Елена Смирнова", 1, "30033323003301", "сегодня, 08:47", 29, 3, "+1", "—", 91),
    ("ilebedev", "Игорь Лебедев", 1, "20023320003202", "сегодня, 09:41", 24, 6, "+1 −1", "Sigma", 58),
    ("afedorov", "Алексей Фёдоров", 1, "31033332003300", "3 ноября", 32, 4, "−1", "личная", 71),
    ("nbelova", "Наталья Белова", 1, "20032333003000", "3 ноября", 39, 5, "+4 −1", "Sigma", 77),
    ("pmorozov", "Павел Морозов", 1, "30000000000000", "23 октября", 14, 4, "—", "Sigma", 63, 0, 1),
    (PULSE_USER[0], PULSE_USER[1], 1, "20022221002202", "сегодня, 09:30", 11, 2, "+1", "Sigma", 54),
    ("kgromov", "Кирилл Громов", 2, "00011011001202", "онлайн", 6, 4, "—", "Sigma", 44),
    ("msokolova", "Мария Соколова", 2, "10020221002302", "сегодня, 09:54", 12, 2, "+3", "личная", 65),
    ("ovolkova", "Ольга Волкова", 2, "10011111001100", "3 ноября", 0, 0, "−1", "Sigma", 30),
    ("rtikhonov", "Роман Тихонов", 2, "20022120002000", "2 ноября", 8, 1, "—", "—", 39),
    ("tzaytseva", "Татьяна Зайцева", 2, "20023000000000", "27 октября", 3, 1, "—", "—", 35, 0, 1),
    ("yershova", "Юлия Ершова", 3, "10010110001101", "сегодня, 09:05", 0, 0, "+2", "личная", 22),
    ("apopov", "Андрей Попов", 3, "00000000002313", "сегодня, 09:12", 5, 0, "—", "—", 12, 1),
    ("knikitin", "Сергей Никитин", 4, "00000000000000", "25 сентября", 0, 0, "—", "—", 18),
]
SEGN = {1: 14, 2: 18, 3: 12, 4: 6, 5: 14}
SEG_KEY = {1: "core", 2: "regular", 3: "once", 4: "churned"}
FN = [("Виктор", 0), ("Ирина", 1), ("Олег", 0), ("Галина", 1), ("Максим", 0), ("Людмила", 1),
      ("Денис", 0), ("Вера", 1), ("Артём", 0), ("Марина", 1), ("Глеб", 0), ("Ксения", 1),
      ("Борис", 0), ("Дарья", 1), ("Степан", 0), ("Полина", 1), ("Тимур", 0), ("Нина", 1),
      ("Георгий", 0), ("Алла", 1), ("Илья", 0), ("Жанна", 1), ("Фёдор", 0), ("Лариса", 1)]
LN = ["Григорьев", "Мельников", "Захаров", "Карпов", "Белов", "Осипов", "Савин", "Королёв",
      "Яковлев", "Широков", "Медведев", "Павлов", "Панов", "Ефимов", "Комаров", "Жуков",
      "Андреев", "Титов", "Голубев", "Абрамов", "Носов", "Кудрявцев", "Макаров", "Пономарёв"]
TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
              ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o",
               "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e",
               "yu", "ya"]))


def translit(s: str) -> str:
    return "".join(TR.get(c, c) for c in s.lower())


def fem(last: str) -> str:
    if last.endswith("ёв"):
        return last + "а"
    return last + "а" if last[-2:] in ("ов", "ев", "ин") else last


LAST_AT = {"3 ноября": ("2026-11-03", "17:40"), "2 ноября": ("2026-11-02", "16:10"),
           "29 октября": ("2026-10-29", "15:00"), "27 октября": ("2026-10-27", "12:20"),
           "23 октября": ("2026-10-23", "18:05"), "25 сентября": ("2026-09-25", "11:30"),
           "15 октября": ("2026-10-15", "10:45"), "в сентябре": ("2026-09-16", "14:00"),
           "давно": ("2026-06-12", "09:30")}


def build_people() -> list[dict]:
    out = []
    for p in NAMED:
        out.append({"login": p[0], "name": p[1], "seg": p[2], "act": p[3], "last": p[4],
                    "ai": p[5], "rep": p[6], "rt": p[7], "mail": p[8], "prof": p[9],
                    "new": bool(p[10]) if len(p) > 10 else False,
                    "risk": bool(p[11]) if len(p) > 11 else False})
    have: dict[int, int] = {}
    for p in out:
        have[p["seg"]] = have.get(p["seg"], 0) + 1
    k = 0
    used = {p["login"] for p in out}
    for sg in (1, 2, 3, 4, 5):
        for i in range(SEGN[sg] - have.get(sg, 0)):
            f, female = FN[k % len(FN)]
            # сдвиг на k // len(FN): у макета имена повторялись через 24 человека
            last = LN[(k * 7 + 3 + k // len(FN)) % len(LN)]
            name = f"{f} {fem(last) if female else last}"
            login = translit(f)[0] + translit(fem(last) if female else last)
            while login in used:
                login += "2"
            used.add(login)
            act, lst, ai, rep = "00000000000000", "давно", 0, 0
            tm = f"сегодня, {8 + k % 2:02d}:{(k * 17) % 60:02d}"
            if sg == 1:
                act, lst, ai, rep = "20032321002302", tm, 8 + (k * 5) % 17, k % 4
            if sg == 2:
                today = i < 8
                act = "10010110001201" if today else "10010210002000"
                lst = tm if today else ("3 ноября" if i % 2 else "2 ноября")
                ai = 2 + k % 6 if i < 5 else 0
                rep = k % 2 if ai else 0
            if sg == 3:
                on = i < 7
                act = ("00010000001000" if i % 2 else "00000010000100") if on else "00000000000000"
                lst = ("2 ноября" if i % 2 else "29 октября") if on else "15 октября"
            if sg == 4:
                lst = "в сентябре"
            out.append({"login": login, "name": name, "seg": sg, "act": act, "last": lst,
                        "ai": ai, "rep": rep, "rt": "—",
                        "mail": "Sigma" if k % 4 == 0 else "личная" if k % 7 == 3 else "—",
                        "prof": 15 + (k * 13) % 60, "new": False, "risk": False})
            k += 1
    for p in out:
        p["on"] = any(c != "0" for c in p["act"])
        p["is_online"] = p["last"] == "онлайн"
        p["is_today"] = p["is_online"] or p["last"].startswith("сегодня")
        if p["is_online"]:
            p["last_dt"] = NOW - dt.timedelta(minutes=2)
        elif p["is_today"]:
            p["last_dt"] = at(TODAY.isoformat(), p["last"].split(", ")[1])
        else:
            p["last_dt"] = at(*LAST_AT[p["last"]])
    return out


PEOPLE = build_people()
BY_LOGIN = {p["login"]: p for p in PEOPLE}
assert len(PEOPLE) == 64
assert sum(p["is_today"] for p in PEOPLE) == 23
assert sum(p["is_online"] for p in PEOPLE) == 4

LV_VIEWS = {0: 0, 1: 4, 2: 14, 3: 38}
LV_TIME = {0: 0, 1: 180, 2: 1100, 3: 3300}
TOP = ["overview", "reviews", "ai", "loophole", "foryou", "market", "knowledge"]


def rating(rt: str) -> tuple[int, int]:
    likes = dislikes = 0
    for part in rt.split():
        if part.startswith("+"):
            likes = int(part[1:])
        elif part.startswith("−"):
            dislikes = int(part[1:])
    return likes, dislikes


def dir_row(p: dict, idx: int) -> dict:
    lv = [int(c) for c in p["act"]]
    extra = {1: 7, 2: 2}.get(p["seg"], 0)
    days_active = sum(1 for x in lv if x) + (extra if p["on"] else 0)
    if not days_active and (NOW - p["last_dt"]).days < 30:
        days_active = 1
    likes, dislikes = rating(p["rt"])
    first = {"apopov": "02.11.2026", "kgromov": "14.09.2026"}.get(p["login"])
    if not first:
        first = (dt.date(2026, 6, 1) + dt.timedelta(days=(idx * 3) % 60)).strftime("%d.%m.%Y")
    today_views = LV_VIEWS[lv[-1]] if p["is_today"] else 0
    errors = 1 if p["login"] in ("ilebedev", "kgromov") else 0
    return {
        "username": p["login"], "name": p["name"], "named": True, "hidden": False,
        "first_seen": first, "last_seen": ddmm(p["last_dt"]),
        "last_seen_ago_s": int((NOW - p["last_dt"]).total_seconds()),
        "has_note": p["prof"] >= 50, "days_active": days_active,
        "views": sum(LV_VIEWS[x] for x in lv) + extra * 20,
        "time_s": sum(LV_TIME[x] for x in lv) + extra * 1500,
        "visits": sum(min(x, 3) for x in lv) + extra, "errors": errors,
        "ai": p["ai"], "deep": p["rep"], "reports": p["rep"], "quick": p["ai"] // 5,
        "shares": p["rep"] // 2, "likes": likes, "dislikes": dislikes,
        "today_views": today_views,
        "top_pages": [TOP[(idx + j) % len(TOP)] for j in range(3)] if p["on"] else [],
        "online": p["is_online"], "today": p["is_today"], "excluded": False,
        "act14": p["act"],
    }


def excluded_rows() -> list[dict]:
    rows = []
    for login, name, act, last, ai in (
            (OWNER[0], OWNER[1], "33333003333303", NOW - dt.timedelta(minutes=1), 61),
            (SERVICE[0][0], SERVICE[0][1], "11111111111111", NOW - dt.timedelta(minutes=5), 0),
            (SERVICE[1][0], SERVICE[1][1], "00000000000100", at("2026-11-03", "12:00"), 3)):
        lv = [int(c) for c in act]
        rows.append({
            "username": login, "name": name, "named": True, "hidden": True,
            "first_seen": "04.08.2026", "last_seen": ddmm(last),
            "last_seen_ago_s": int((NOW - last).total_seconds()), "has_note": False,
            "days_active": sum(1 for x in lv if x) + 10, "views": sum(LV_VIEWS[x] for x in lv),
            "time_s": sum(LV_TIME[x] for x in lv), "visits": sum(lv), "errors": 0, "ai": ai,
            "deep": 0, "reports": 0, "quick": 0, "shares": 0, "likes": 0, "dislikes": 0,
            "today_views": LV_VIEWS[lv[-1]], "top_pages": ["pulse", "overview"] if ai else ["overview"],
            "online": (NOW - last).total_seconds() < 900, "today": bool(lv[-1]),
            "excluded": True, "act14": act})
    return rows


def users_directory() -> dict:
    ppl = [dir_row(p, i) for i, p in enumerate(PEOPLE)]

    def score(r: dict) -> float:     # порядок telemetry.users_directory
        return (r["time_s"] / 60 + r["views"] * 2 + r["ai"] * 15 + r["deep"] * 30
                + r["likes"] * 5 + r["dislikes"] * 5)
    ppl.sort(key=lambda r: (-score(r), r["last_seen_ago_s"]))
    rows = ppl + excluded_rows()
    return {"days": 30, "users": rows, "total": len(ppl), "excluded": len(rows) - len(ppl),
            "active": sum(1 for r in ppl if r["days_active"]),
            "today": sum(1 for r in ppl if r["today"]),
            "online": sum(1 for r in ppl if r["online"]),
            "silent": sum(1 for r in ppl if not r["days_active"]),
            "act_days": DAYS14, "act_off": OFF14}


# ── /api/admin/pulse ──────────────────────────────────────────────────────────
DAU_USERS = [24, 3, 2, 27, 29, 26, 31, 22, 4, 1, 28, 30, 5, 23]
AI_DAY = [28, 3, 2, 31, 35, 29, 33, 22, 4, 1, 34, 43, 6, 41]           # сумма 312
REP_DAY = [4, 0, 0, 5, 5, 4, 6, 3, 0, 0, 5, 7, 1, 7]                   # сумма 47
FB_GOOD = [2, 0, 0, 3, 1, 2, 3, 1, 0, 0, 4, 3, 2, 0]                   # сумма 21
FB_BAD = [0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0]                    # сумма 4
HEAT = [[0, 1, 6, 11, 9, 7, 3, 8, 10, 9, 6, 3, 1], [0, 2, 7, 12, 10, 8, 4, 9, 11, 8, 5, 2, 0],
        [0, 1, 5, 10, 11, 7, 3, 7, 9, 8, 6, 2, 1], [1, 2, 8, 12, 10, 9, 4, 8, 10, 7, 5, 3, 0],
        [0, 1, 6, 9, 8, 6, 2, 6, 7, 5, 3, 1, 0]]
RIBBON = {"server": "oooooooowoooow", "digest": "oooooooooowoob", "reviews": "oooooooooowbbb",
          "news": "oooooooooooooo", "slices": "ooooowoooooooo",
          # поиск по жалобам пишет события с 04.11.2026 — раньше истории нет
          "rsearch": "nnnnnnnnnnnnoo",
          "web": "ooowoooooooooo", "ingest": "oooooooooooooo"}
assert sum(AI_DAY) == 312 and sum(REP_DAY) == 47 and sum(FB_GOOD) == 21 and sum(FB_BAD) == 4


def dau() -> list[dict]:
    views = [round(u * 7.02) for u in DAU_USERS]
    return [{"d": d, "users": u, "views": v, "off": is_off(d)}
            for d, u, v in zip(DAYS14, DAU_USERS, views)]


def feed() -> list[dict]:
    rows = [("10:02", "akovaleva", "ai_query", None, None, True),
            ("09:58", "dorlov", "page_view", "reviews", None, None),
            ("09:54", "msokolova", "share", None, None, None),
            ("09:41", "ilebedev", "api_error", "/api/reviews/search", 504, None),
            ("09:33", "esmirnova", "ai_query", None, None, False),
            ("09:30", PULSE_USER[0], "page_view", "overview", None, None),
            ("09:20", "kgromov", "page_view", "reviews", None, None),
            ("09:12", "apopov", "page_view", "loophole", None, None),
            ("09:12", "kgromov", "api_error", "/api/reviews/search", 504, None),
            ("09:05", "yershova", "page_view", "overview", None, None),
            ("08:58", "svkuznetsova", "report_open", None, None, None),
            ("08:51", "akovaleva", "page_view", "ai", None, None),
            ("08:47", "esmirnova", "page_view", "foryou", None, None),
            ("08:40", "dorlov", "ai_query", None, None, False),
            ("08:33", "svkuznetsova", "page_view", "market", None, None),
            ("08:21", "nbelova", "page_view", "overview", None, None)]
    names = {p["login"]: p["name"] for p in PEOPLE}
    return [{"ts": t, "username": u, "name": names[u], "kind": k, "page": pg, "status": st,
             "deep": deep} for t, u, k, pg, st, deep in rows]


def retention() -> dict:
    seg = {"core": 0, "regular": 0, "once": 0, "churned": 0, "new": 3}
    seg_of = {}
    for p in PEOPLE:
        key = SEG_KEY.get(p["seg"])
        if key:
            seg[key] += 1
            seg_of[p["login"]] = key
    at_risk = [
        {"username": "pmorozov", "name": "Павел Морозов", "last": "2026-10-23", "gap_days": 13,
         "days_60": 21, "page": "ai", "ai": 14},
        {"username": "tzaytseva", "name": "Татьяна Зайцева", "last": "2026-10-27", "gap_days": 9,
         "days_60": 9, "page": "reviews", "ai": 3}]
    churned = []
    for p in PEOPLE:
        if p["seg"] == 4:
            last = p["last_dt"].date()
            churned.append({"username": p["login"], "name": p["name"], "last": last.isoformat(),
                            "gap_days": (TODAY - last).days, "days_60": 2 if p["login"] != "knikitin" else 4,
                            "page": "overview", "ai": 0 if p["login"] != "knikitin" else 2,
                            "days_prev": 4 if p["login"] == "knikitin" else 2})
    churned.sort(key=lambda r: (-r["days_prev"], r["last"]))
    cohorts = [
        {"week": "2026-09-28", "n": 6, "w2": 4, "w4": 3, "w2_ready": 6, "w4_ready": 6},
        {"week": "2026-10-05", "n": 4, "w2": 2, "w4": 2, "w2_ready": 4, "w4_ready": 4},
        {"week": "2026-10-12", "n": 3, "w2": 2, "w4": 0, "w2_ready": 3, "w4_ready": 0},
        {"week": "2026-10-19", "n": 2, "w2": 1, "w4": 0, "w2_ready": 2, "w4_ready": 0},
        {"week": "2026-10-26", "n": 3, "w2": 0, "w4": 0, "w2_ready": 0, "w4_ready": 0}]
    return {"segments": seg, "active_7d": 34, "active_prev_7d": 31, "cohorts": cohorts,
            "at_risk": at_risk, "churned": churned, "churned_total": len(churned),
            "from_mail_7d": 5, "seg_of": seg_of}


QUESTIONS = [
    "Сравни ставки по вкладам ТОП-10 банков на 6 месяцев",
    "Динамика жалоб на навязанные страховки в потребкредитах",
    "Ипотечные программы с господдержкой: условия банков",
    "Тарифы на обслуживание премиальных карт",
    "Комиссии за переводы СБП у ТОП-10 банков",
    "Какие банки подняли комиссию за снятие наличных в чужих банкоматах?",
    "Сколько жалоб на кредитные карты Сбера за октябрь?",
    "Что известно о новых требованиях ЦБ к раскрытию ПСК?",
    "Условия кешбэка по дебетовым картам у крупных банков",
    "Жалобы на блокировки по 161-ФЗ за месяц",
    "Сравни эквайринг для малого бизнеса у четырёх банков",
    "Как меняются ставки по накопительным счетам после решения ЦБ",
    "Детские карты: условия и жалобы родителей",
    "Самозанятые: тарифы и ограничения в банках",
    "Выездное обслуживание: на что жалуются клиенты",
]


def ai_feedback() -> dict:
    items = []
    seq = []
    for i, d in enumerate(DAYS14):
        seq += [(d, -1)] * FB_BAD[i] + [(d, 1)] * FB_GOOD[i]
    names = [p for p in PEOPLE if p["ai"] > 0]
    special = {("2026-11-04", -1): ("dorlov", QUESTIONS[0], "deep", "4187", ["wrong"],
                                    "У ВТБ указана ставка прошлого месяца — 19,5% вместо 18%.")}
    bad_pool = [("nbelova", QUESTIONS[6], "quick", "4179", ["wrong", "shallow"], ""),
                ("afedorov", QUESTIONS[5], "quick", None, ["shallow"], "Часть изменений — ещё летние."),
                ("ilebedev", QUESTIONS[7], "quick", None, ["offtopic"], "")]
    reason_ru = {"offtopic": "не по делу", "shallow": "мало конкретики", "wrong": "ошибка в данных",
                 "long": "слишком длинно"}
    counts: dict[str, int] = {}
    for n, (d, v) in enumerate(reversed(seq)):
        hm = f"{17 - n % 8:02d}:{(n * 11) % 60:02d}"
        if (d, v) in special and not any(x["username"] == "dorlov" and x["verdict"] < 0 for x in items):
            u, q, mode, rid, rs, cm = special[(d, v)]
            hm = "14:22"
        elif v < 0:
            u, q, mode, rid, rs, cm = bad_pool.pop(0)
        else:
            p = names[n % len(names)]
            u, q, mode, rid, rs, cm = p["login"], QUESTIONS[n % len(QUESTIONS)], \
                ("deep" if n % 3 == 0 else "quick"), (str(4100 + n) if n % 3 == 0 else None), [], ""
        for r in rs:
            counts[r] = counts.get(r, 0) + 1
        items.append({"username": u, "name": BY_LOGIN[u]["name"], "verdict": v,
                      "question": q, "comment": cm, "mode": mode, "report_id": rid,
                      "quote": None, "reasons": [reason_ru[x] for x in rs],
                      "created_at": f"{d} {hm}:00+03:00"})
    total_counts = {"wrong": 2, "shallow": 2, "offtopic": 1}
    return {"items": items[:20], "likes": 21, "dislikes": 4, "answers": 312,
            "reasons": sorted(({"key": k, "label": reason_ru[k], "n": v}
                               for k, v in total_counts.items()), key=lambda x: -x["n"]),
            "by_day": [{"d": d, "good": g, "bad": b} for d, g, b in zip(DAYS14, FB_GOOD, FB_BAD)]}


def persona() -> dict:
    parts = [("desc", "Описание зоны ответственности"), ("ratings", "5+ оценок в «Для вас»"),
             ("focus", "3+ темы в фокусе"), ("queries", "5+ вопросов ИИ"),
             ("ai_ratings", "3+ оценки ответов ИИ"), ("note", "ИИ-нарратив собран")]
    users = []
    for i, p in enumerate(PEOPLE):
        s = p["prof"]
        flags = {"desc": s >= 40, "ratings": s >= 70, "focus": s >= 55, "queries": p["ai"] >= 5,
                 "ai_ratings": p["rt"] != "—" and s >= 60, "note": s >= 50}
        users.append({"username": p["login"], "name": p["name"], "score": s,
                      "recent": p["seg"] <= 3, "personal_on": i % 5 != 0, "parts": flags})
    users.sort(key=lambda u: (not u["recent"], u["score"], u["name"]))
    gaps = []
    for k, label in parts:
        miss = sum(1 for u in users if not u["parts"][k])
        if miss:
            gaps.append({"key": k, "label": label, "miss": miss})
    gaps.sort(key=lambda x: -x["miss"])
    scores = sorted(u["score"] for u in users)
    return {"users": users, "median": scores[len(scores) // 2], "gaps": gaps[:3],
            "parts": [{"key": k, "label": l} for k, l in parts]}


def mail_brief() -> dict:
    people = []
    sso_left = 2                     # адрес из учётной записи входа — у двух человек без своей почты
    for i, p in enumerate(PEOPLE):
        if p["mail"] == "—":
            if not (sso_left and i >= len(NAMED) and p["seg"] in (1, 2)):
                continue
            sso_left -= 1
        kind = "sigma" if p["mail"] == "Sigma" else "private" if p["mail"] == "личная" else "sso"
        email = (f"{p['login']}@corp.example" if kind != "private" else f"{p['login']}@mail.example")
        since = dt.date(2026, 10, 6) + dt.timedelta(days=i % 25)
        people.append({"username": p["login"], "name": p["name"], "email": email,
                       "source": "sso" if kind == "sso" else "user", "since": since.strftime("%d.%m.%Y"),
                       "sent": 8 if p["seg"] <= 2 else 2, "failed": 1 if p["login"] == "rtikhonov" else 0,
                       "last_mail": "05.11 11:00" if p["seg"] <= 2 else "04.11 11:00",
                       "excluded": False, "active": p["on"], "kind": kind})
    people.append({"username": OWNER[0], "name": OWNER[1], "email": f"{OWNER[0]}@corp.example",
                   "source": "user", "since": "06.10.2026", "sent": 12, "failed": 0,
                   "last_mail": "05.11 11:00", "excluded": True, "active": True, "kind": "sigma"})
    ppl = [r for r in people if not r["excluded"]]
    return {"connected": len(ppl), "sigma": sum(1 for r in ppl if r["kind"] == "sigma"),
            "private": sum(1 for r in ppl if r["kind"] == "private"),
            "sso": sum(1 for r in ppl if r["kind"] == "sso"),
            "active": 39, "active_connected": sum(1 for r in ppl if r["active"]), "pending": 3,
            "sent": 214, "codes": 31, "failed": 1,
            "promo": {"shown": 38, "connect": 21, "later": 9}, "people": people}


def news_quality() -> dict:
    series = []
    for i, d in enumerate(DAYS14):
        n = 9 if is_off(d) else 14
        series.append({"d": d, "n_items": n, "junk": 1 if i % 4 == 0 else 0, "borderline": 3,
                       "relevant": n - 4, "avg": 6.4 + (i % 3) * 0.2, "head": 4 if i % 5 else 5,
                       "strong": 4 if not is_off(d) else 2, "missed": 1 if i % 6 == 0 else 0,
                       "rubric": "повод для проверки аудитора розницы"})
    series[-1]["head"] = 4
    srcs = [("rss_cbr", "Банк России"), ("rss_frankrg", "Frank Media"), ("tg_banksta", "Banksta"),
            ("rss_banki", "Banki.ru — новости"), ("rss_rbc_fin", "РБК Финансы"), ("search_ya", "Поиск Яндекса")]
    return {
        "series": series, "today": series[-1],
        "clicks": [{"d": d, "n": 0 if is_off(d) else 9 + i % 5, "users": 0 if is_off(d) else 6}
                   for i, d in enumerate(DAYS14)],
        "top_clicked": [
            {"url": "https://news.example/deposits-rates", "title": "Банки снижают ставки по вкладам вслед за ключевой", "n": 14},
            {"url": "https://news.example/psk-rules", "title": "ЦБ уточнил правила раскрытия ПСК", "n": 9},
            {"url": "https://news.example/sbp-fees", "title": "Комиссии за переводы через СБП: что изменится с января", "n": 7}],
        "cards": {"useful": 44, "noise": 6, "users": 17},
        "stream": {
            "sources": [{"source": s, "last_ok_at": iso(NOW - dt.timedelta(minutes=12 + j)),
                         "last_error": None, "items_24h": 40 + j * 13, "label": l}
                        for j, (s, l) in enumerate(srcs)],
            "items_24h": 412, "relevant_24h": 118, "strong_24h": 11, "events_24h": 260,
            "yield_14d": [{"source": s, "items": 300 + j * 90, "relevant": 80 + j * 20,
                           "strong": 9 + j * 3, "published": 6 + j, "label": l}
                          for j, (s, l) in enumerate(srcs)],
            "search_14d": {"events": 140, "only_search": 31, "only_search_strong": 6,
                           "only_search_published": 4}},
    }


def review_sources() -> dict:
    def run(d: str, hm: str, st: str = "ok", err: str | None = None) -> tuple:
        return iso(at(d, hm)), st, err
    rows = [
        ("bankiru", "banki.ru — корпус", 1210, 1180, "норма", True, "2026-11-03", None),
        ("banki_reviews", "banki.ru — наш сбор", 390, 370, "норма", False, "2026-11-05", run("2026-11-05", "06:10")),
        ("sravni_reviews", "sravni.ru", 260, 240, "норма", False, "2026-11-04", run("2026-11-05", "06:25")),
        ("finuslugi_reviews", "finuslugi.ru", 51, 48, "норма", False, "2026-11-04", run("2026-11-05", "06:40")),
        ("otzovik_reviews", "Отзовик", 42, 180, "просел", False, "2026-11-02",
         run("2026-11-04", "06:55", "failed", "сайт ответил 403 — сбор остановлен на второй странице")),
    ]
    out = []
    for src, label, week, norm, status, ext, last_item, r in rows:
        out.append({"source": src, "label": label, "week": week, "norm": norm, "status": status,
                    "external": ext, "last_item": last_item,
                    "last_run": r[0] if r else None, "last_run_status": r[1] if r else None,
                    "last_error": r[2] if r else None})
    return {"sources": out, "gone_banks": [], "week_end": "2026-11-04"}


def slice_canary() -> list[dict]:
    from bank_audit.rag import review_slices as rs
    runs = []
    for j, d in enumerate(("2026-11-02", "2026-10-26", "2026-10-19", "2026-10-12")):
        metrics = {k: {"precision": round(0.9 - 0.02 * ((i + j) % 3), 2),
                       "recall": round(0.93 - 0.03 * ((i + 2 * j) % 2), 2),
                       "tp": 18 + i, "fp": 2, "fn": 1 + (i + j) % 2}
                   for i, k in enumerate(rs.KEYS)}
        runs.append({"run_at": iso(at(d, "04:10")), "schema": "s7", "n": 120, "metrics": metrics,
                     "rub": 182.4 - j * 3, "note": "", "alert": False, "reasons": []})
    return runs


def pulse() -> dict:
    users_total = 64
    act = [p for p in PEOPLE if p["on"]]
    researchers = sum(1 for p in act if p["ai"] > 0)
    readers = sum(1 for p in act if not p["ai"] and p["seg"] in (2, 3) and p["login"] != "apopov") // 2
    pages = [("overview", 612, 37, 29520), ("reviews", 488, 29, 23400), ("ai", 301, 24, 14400),
             ("loophole", 174, 12, 8280), ("foryou", 120, 18, 5760), ("market", 64, 9, 3060),
             ("knowledge", 22, 5, 1080), ("banks", 9, 3, 300)]
    hm = []
    for r, row in enumerate(HEAT):
        for c, v in enumerate(row):
            if v:
                hm.append({"dow": r + 1, "hour": 8 + c, "n": v * 4})
    hm += [{"dow": 6, "hour": 12, "n": 3}, {"dow": 7, "hour": 19, "n": 2}]
    return {
        "days": 14, "excluded": 3,
        "today": {"active": 23, "views": 161, "ai": 41, "errors": 2, "online": 4,
                  "users_total": users_total},
        "dau": dau(),
        "new_users": [{"d": "2026-10-27", "n": 1}, {"d": "2026-10-29", "n": 1},
                      {"d": "2026-11-02", "n": 1}],
        "pages": [{"page": p, "views": v, "users": u, "total_s": t} for p, v, u, t in pages],
        "ai_per_day": [{"d": d, "n": n} for d, n in zip(DAYS14, AI_DAY)],
        "features": {
            "reports": 47, "quick_saved": 58, "shares": 23, "ai_total": 312, "ai_deep": 61,
            "runs_ok": 288, "runs_failed": 9, "runs_stopped": 6,
            "deep_p50_s": 142, "deep_p95_s": 268, "quick_p50_s": 34,
            "deep_ok": 52, "deep_failed": 5, "report_opens": 64,
            "fb_likes": 44, "fb_dislikes": 6, "ai_likes": 21, "ai_dislikes": 4, "profiles": 38,
            "ratings_last": [{"kind": "ai_answer", "d": "2026-11-04", "age_days": 0},
                             {"kind": "news", "d": "2026-11-04", "age_days": 0},
                             {"kind": "digest_card", "d": "2026-11-03", "age_days": 1},
                             {"kind": "for_you", "d": "2026-10-30", "age_days": 5}],
            "ai_total_prev": 274, "reports_prev": 41,
        },
        "heatmap": hm,
        "latency": [
            {"path": "/api/reviews/search", "n": 1840, "p50": 420, "p95": 3400, "errs": 2},
            {"path": "/api/overview/digest", "n": 1210, "p50": 95, "p95": 310, "errs": 0},
            {"path": "/api/reviews/feed", "n": 980, "p50": 180, "p95": 640, "errs": 0},
            {"path": "/api/chat/sessions", "n": 640, "p50": 60, "p95": 190, "errs": 0},
            {"path": "/api/market/atlas", "n": 210, "p50": 520, "p95": 1900, "errs": 1},
            {"path": "/api/me", "n": 2100, "p50": 35, "p95": 120, "errs": 0}],
        "errors_recent": [
            {"kind": "api_error", "page": "/api/reviews/search", "status": 504,
             "msg": "Gateway Timeout", "n": 2, "users": 2, "ts": "05.11 09:41"},
            {"kind": "api_error", "page": "/api/market/atlas", "status": 500,
             "msg": "KeyError: 'rate_6m'", "n": 1, "users": 1, "ts": "31.10 12:40"}],
        "errors_total": 3,
        "errors_per_day": [{"d": "2026-10-31", "n": 1}, {"d": "2026-11-05", "n": 2}],
        "tokens": [{"d": d, "tin": 182000 + i * 900, "tout": 21000 + i * 150}
                   for i, d in enumerate(DAYS14)],
        "digest": [
            {"section": "headline", "status": "ok", "d": "2026-11-05", "at": "07:41", "gen_ms": 41200, "error": None},
            {"section": "news", "status": "ok", "d": "2026-11-05", "at": "07:38", "gen_ms": 52800, "error": None},
            {"section": "reviews_pulse", "status": "ok", "d": "2026-11-05", "at": "07:45", "gen_ms": 33100, "error": None},
            {"section": "tariff_moves", "status": "failed", "d": "2026-11-05", "at": "07:45", "gen_ms": 60000,
             "error": "модель не ответила за 60 секунд"}],
        "feed": feed(),
        "reports_per_day": [{"d": d, "n": n} for d, n in zip(DAYS14, REP_DAY) if n],
        "segments": {"researchers": researchers, "readers": readers,
                     "casual": len(act) - researchers - readers,
                     "sleepers": users_total - len(act), "active": len(act)},
        "ai_feedback": ai_feedback(),
        "persona": persona(),
        "retention": retention(),
        "proposals": {"pending": 2, "oldest_days": 6, "items": [
            {"proposal_id": 31, "purpose": "digest", "domain": "frankrg.com", "title": "Frank Media — новости банков",
             "author": "Елена Смирнова", "created_at": "2026-10-30 11:20:00+03:00", "age_days": 6},
            {"proposal_id": 33, "purpose": "digest", "domain": "cbr.ru/press", "title": "Пресс-релизы Банка России",
             "author": "Дмитрий Орлов", "created_at": "2026-11-03 15:05:00+03:00", "age_days": 1}]},
        "ingest": {"queue": {"enqueued": 940, "done": 912, "failed": 6, "dropped": 0, "duplicate": 22,
                             "depth": 0, "inflight": 0, "workers": 2},
                   "per_day": [{"d": d, "n": 20 if is_off(d) else 70 + i, "empty": 3,
                                "p50": 2100, "p95": 6400} for i, d in enumerate(DAYS14)]},
        "collect": {"ok": 860, "hard": 12, "soft": 64,
                    "reasons": [{"key": "ok", "label": "проиндексировано", "n": 860, "hard": False},
                                {"key": "duplicate", "label": "уже было", "n": 51, "hard": False},
                                {"key": "no_chunks", "label": "нечего индексировать", "n": 13, "hard": False},
                                {"key": "captcha", "label": "капча", "n": 7, "hard": True},
                                {"key": "fetch_failed", "label": "не загрузилось", "n": 5, "hard": True}],
                    "domains": [{"domain": "www.banki.ru", "n": 6}, {"domain": "www.sravni.ru", "n": 4},
                                {"domain": "frankrg.com", "n": 2}]},
        "search": {"backends": [
            {"kind": "web_search", "backend": "yandex", "total": 1260,
             "by_status": {"ok": 1190, "empty": 48, "limited": 22}, "p50_ok": 780},
            {"kind": "web_search_chain", "backend": "yandex_gw", "total": 1180,
             "by_status": {"ok": 1180}, "p50_ok": 820},
            {"kind": "web_search_chain", "backend": "fleet", "total": 41, "by_status": {"ok": 41}, "p50_ok": 2400},
            {"kind": "web_search_chain", "backend": "none", "total": 9, "by_status": {"empty": 9}, "p50_ok": None},
            {"kind": "web_read", "backend": "yandex_copy", "total": 112,
             "by_status": {"ok": 104, "error": 8}, "p50_ok": 1300}],
            "gateway": {"enabled": True, "primary": "yandex", "breaker_open": False,
                        "breaker_reason": None, "copies_breaker_open": False, "rps": 4}},
        # 0 сбоев: иначе правило «поиск по жалобам не отработал» добавило бы четвёртый сбой
        "reviews_search": {"n": 1840, "users": 31, "p50": 400, "p90": 1300, "no_words": 212,
                           "failed": 0, "unchecked": 40,
                           "zero_queries": ["навязали страховку при рефинансировании", "кэшбек не пришёл за такси",
                                            "закрыли счёт самозанятого"]},
        # пороги сторожа не задеты: хвост разметки 12 (> 50 — сбой), отметкам 1,5 дня (≥ 3 — дело)
        "reviews_pipeline": {"pending_old": 12, "slices_not_ready": [], "flags_open": 4,
                             "flags_oldest_days": 1.5, "canary_age_days": 3.3,
                             "dedup_age_h": 6.2, "stories_age_h": 20.5},
        "news_quality": news_quality(),
        "review_sources": review_sources(),
        "signal_journal": {"days": 180, "episodes": 41, "rated": 18, "confirmed": 13, "false": 5,
                           "precision": 72, "by_bank": [
                               {"bank": "Сбербанк", "episodes": 14, "rated": 8, "precision": 75},
                               {"bank": "ВТБ", "episodes": 9, "rated": 4, "precision": 50},
                               {"bank": "Т-Банк", "episodes": 6, "rated": 3, "precision": 100}]},
        "personalization": {"ctr": 18.4, "users": [
            {"username": p["login"], "name": p["name"], "score": p["prof"],
             "views": 6 if p["seg"] == 1 else 2, "clicks": 1 if p["seg"] == 1 else 0,
             "clicks_issue": 2 if p["seg"] == 1 else 0, "fb": 3 if p["rt"] != "—" else 0}
            for p in sorted(PEOPLE, key=lambda x: x["last_dt"], reverse=True)[:20]]},
        "topics": {"banks": [{"slug": "sberbank", "name": "Сбербанк", "n": 38},
                             {"slug": "vtb", "name": "ВТБ", "n": 21},
                             {"slug": "alfabank", "name": "Альфа-Банк", "n": 17},
                             {"slug": "tbank", "name": "Т-Банк", "n": 15},
                             {"slug": "gazprombank", "name": "Газпромбанк", "n": 8},
                             {"slug": "raiffeisen", "name": "Райффайзенбанк", "n": 5}]},
        "eval": {"hermes": {"score": 81.5, "model": "oss", "d": "2026-11-02", "age_days": 3,
                            "trigger": "gate", "prev": 84.0, "delta": -2.5},
                 "deep": {"score": 77.0, "model": "default", "d": "2026-10-30", "age_days": 6,
                          "trigger": "gate", "prev": 76.0, "delta": 1.0}},
        "inbox": inbox_brief(),
        "mail": mail_brief(),
        "slice_canary": slice_canary(),
        "health": {"days": DAYS14, "off": OFF14,
                   **{k: list(v) for k, v in RIBBON.items()}},
        "with_me": False, "hidden_n": 2, "is_owner": True,
    }


# ── обращения ────────────────────────────────────────────────────────────────
KINDS = {"idea": "Идея", "bug": "Ошибка", "numbers": "Неверные цифры", "howto": "Вопрос", "other": "Другое"}
STATUSES = {"new": "Новое", "accepted": "Принято", "in_progress": "В работе", "done": "Сделано",
            "wontfix": "Не будем делать", "exists": "Уже есть"}
OPEN = ("new", "accepted", "in_progress")
# (№, логин, вид, раздел, подпись раздела, текст, статус, создано, обновлено, файлов, сообщений,
#  не прочитано командой, первый ответ)
TICKETS = [
    (122, "ovolkova", "howto", "overview", "Новостные обзоры",
     "Почему в сегодняшнем выпуске нет раздела про ставки?", "new",
     at("2026-11-05", "08:05"), at("2026-11-05", "08:05"), 0, 1, True, None),
    (121, "ilebedev", "idea", "market", "Рынок · позиция",
     "Добавить выгрузку сравнения тарифов в Excel — для приложения к акту проверки.", "new",
     at("2026-11-05", "01:10"), at("2026-11-05", "01:10"), 0, 1, True, None),
    (118, "msokolova", "bug", "reviews", "Аудит отзывов",
     ("В «Аудите отзывов» фильтр по продукту сбрасывается, когда перехожу на вторую страницу "
      "списка жалоб. Приходится выбирать «Кредитные карты» заново на каждой странице."), "new",
     at("2026-11-03", "08:12"), at("2026-11-03", "08:12"), 2, 1, True, None),
    (115, "pmorozov", "bug", "ai", "ИИ-помощник",
     "Отчёт по ипотечным программам оборвался на середине, хотя прогон показал «готово».",
     "in_progress", at("2026-10-22", "16:40"), at("2026-10-23", "11:15"), 1, 3, False,
     at("2026-10-22", "19:02")),
    (109, "esmirnova", "idea", "reviews", "Аудит отзывов",
     "Подписка на сюжеты по конкретному продукту, а не по всему банку.", "done",
     at("2026-10-14", "10:20"), at("2026-10-28", "12:00"), 0, 4, False, at("2026-10-14", "14:30")),
]
_OLD = [("akovaleva", "numbers", "market", "Рынок · позиция", "Ставка ВТБ по вкладу на 3 месяца не совпадает с сайтом банка.", "done"),
        ("dorlov", "idea", "reviews", "Аудит отзывов", "Фильтр по региону в карте жалоб.", "done"),
        ("nbelova", "howto", "ai", "ИИ-помощник", "Как заказать отчёт по нескольким банкам сразу?", "done"),
        ("svkuznetsova", "bug", "overview", "Новостные обзоры", "Ссылка в карточке новости ведёт на главную источника.", "done"),
        ("afedorov", "idea", "loophole", "Аудит уязвимостей", "Выгрузка находок в Excel с комментариями.", "exists"),
        ("kgromov", "other", None, None, "Спасибо за подборку по эквайрингу — пригодилась в проверке.", "done"),
        ("rtikhonov", "bug", "reviews", "Аудит отзывов", "Не открывается жалоба из сюжета на телефоне.", "done"),
        ("msokolova", "idea", "overview", "Новостные обзоры", "Присылать выпуск в 9:00, а не в 11:00.", "wontfix"),
        ("ilebedev", "numbers", "market", "Рынок · позиция", "Комиссия за перевод у Т-Банка устарела.", "done"),
        ("esmirnova", "howto", "reviews", "Аудит отзывов", "Как сохранить срез жалоб в аудит-дело?", "done"),
        ("yershova", "idea", "foryou", "Для вас", "Скрывать новости, которые уже открывал.", "exists"),
        ("dorlov", "bug", "ai", "ИИ-помощник", "Отчёт не скачивается в PDF.", "done"),
        ("akovaleva", "idea", "market", "Рынок · позиция", "Сравнение тарифов за прошлый квартал.", "done")]
for _i, (_u, _k, _s, _sl, _b, _st) in enumerate(_OLD):
    _c = at("2026-10-12", "10:00") - dt.timedelta(days=3 * _i)
    TICKETS.append((107 - _i * 2, _u, _k, _s, _sl, _b, _st, _c, _c + dt.timedelta(days=2), 0, 3, False,
                    _c + dt.timedelta(hours=4 + _i % 5)))
assert len(TICKETS) == 18


def ticket_row(t) -> dict:
    tid, u, kind, sec, secl, body, st, cr, up, nf, nm, unread, ans = t
    return {"ticket_id": tid, "username": u, "name": BY_LOGIN[u]["name"], "kind": kind,
            "section": sec, "section_label": secl, "body": body[:300], "status": st,
            "confirmed": True if st == "done" and tid == 109 else None,
            "created_at": iso(cr), "updated_at": iso(up), "n_files": nf, "n_msgs": nm,
            "unread": unread, "answered_at": iso(ans) if ans else None,
            "status_label": STATUSES[st], "kind_label": KINDS[kind]}


def inbox_brief() -> dict:
    return {"new": sum(1 for t in TICKETS if t[6] == "new"),
            "open": sum(1 for t in TICKETS if t[6] in OPEN),
            "unread": sum(1 for t in TICKETS if t[11]), "total": len(TICKETS),
            "oldest_new_days": 2, "unanswered": sum(1 for t in TICKETS if t[6] in OPEN and not t[12]),
            "oldest_unanswered_h": 50, "median_answer_h": 5.0}


def inbox_list(status: str) -> dict:
    rows = [t for t in TICKETS if status == "all" or (status == "new" and t[6] == "new")
            or (status == "open" and t[6] in OPEN)]
    rows = sorted(rows, key=lambda t: (not t[11], -t[8].timestamp()))
    return {"tickets": [ticket_row(t) for t in rows], "counts": inbox_brief(), "kinds": KINDS,
            "statuses": STATUSES}


def ticket_118() -> dict:
    t = next(x for x in TICKETS if x[0] == 118)
    row = ticket_row(t)
    row.pop("n_files"), row.pop("n_msgs"), row.pop("unread"), row.pop("answered_at")
    row["body"] = t[5]
    return {**row, "context": {"url": "#reviews?product=credit_cards&page=2",
                               "version": "5 ноября, 08:10", "browser": "Яндекс Браузер 25.8, Windows 10",
                               "screen": "1920×1080", "theme": "светлая", "errors": []},
            "user_seen_at": iso(at("2026-11-03", "08:12")), "team_seen_at": None,
            "messages": [{"role": "user", "body": t[5], "at": iso(at("2026-11-03", "08:12"))}],
            "files": [{"file_id": 501, "w": 1440, "h": 900}, {"file_id": 502, "w": 1440, "h": 900}]}


# ── отчёты, жалобы, карточки ─────────────────────────────────────────────────
def reports_all() -> dict:
    rows = [
        (4187, "dorlov", QUESTIONS[0], "Ставки по вкладам ТОП-10 банков на 6 месяцев", ["sberbank", "vtb", "alfabank"],
         "deep", 18400, "ok", 151, at("2026-11-04", "14:01"), 0, 1, "У ВТБ указана ставка прошлого месяца — 19,5% вместо 18%.",
         '["wrong"]', 1, 4),
        (4179, "nbelova", QUESTIONS[6], "Жалобы на кредитные карты Сбера за октябрь", ["sberbank"],
         "quick", 2100, None, None, at("2026-11-03", "16:02"), 0, 1, None, '["wrong", "shallow"]', 0, 1),
        (4176, "esmirnova", QUESTIONS[1], "Навязанные страховки в потребкредитах: динамика жалоб", ["sberbank", "vtb"],
         "deep", 24300, "ok", 166, at("2026-11-03", "11:40"), 2, 0, None, None, 2, 7),
        (4121, "pmorozov", QUESTIONS[2], "Ипотека с господдержкой: условия банков", ["sberbank", "vtb", "domrf"],
         "deep", 320, "failed", 95, at("2026-10-23", "17:50"), 0, 0, None, None, 0, 2),
        (4119, "ilebedev", QUESTIONS[3], "Премиальные карты: тарифы на обслуживание", ["sberbank", "tbank", "alfabank"],
         "deep", 21200, "ok", 139, at("2026-10-23", "12:30"), 1, 0, None, None, 1, 3),
    ]
    authors = [p for p in PEOPLE if p["rep"] > 0 or p["ai"] > 0]
    deep_left, quick_left = 47 - 4, 58 - 1
    n = 0
    rid = 4186
    while deep_left or quick_left:
        mode = "deep" if (n % 2 == 0 and deep_left) or not quick_left else "quick"
        if mode == "deep":
            deep_left -= 1
        else:
            quick_left -= 1
        p = authors[n % len(authors)]
        day = DAYS14[13 - (n * 5) % 14]
        if rid in (4179, 4176, 4121, 4119):
            rid -= 1
        when = at(day, f"{9 + n % 8:02d}:{(n * 7) % 60:02d}")
        if when > NOW:
            when = when - dt.timedelta(days=1)
        rows.append((rid, p["login"], QUESTIONS[n % len(QUESTIONS)], QUESTIONS[n % len(QUESTIONS)][:60],
                     ["sberbank"], mode, 12000 + (n * 997) % 14000 if mode == "deep" else 1500 + (n * 131) % 1500,
                     "ok" if mode == "deep" else None, 120 + n % 60 if mode == "deep" else None, when,
                     1 if n % 9 == 0 else 0, 0, None, None, n % 3 == 0, n % 5))
        rid -= 1
        n += 1
    out = []
    for r in rows:
        (rid, u, q, title, banks, mode, blen, st, el, when, lk, dl, cm, rs, sh, op) = r
        out.append({"report_id": rid, "username": u, "name": BY_LOGIN[u]["name"], "question": q,
                    "title": title, "banks": banks, "mode": mode, "body_len": blen, "status": st,
                    "elapsed_s": el, "at": ddmm(when), "likes": lk, "dislikes": dl, "comment": cm,
                    "reasons": rs, "shares": int(sh), "opens": op, "_t": when.timestamp()})
    out.sort(key=lambda r: (not r["dislikes"], -r["_t"]))
    for r in out:
        r.pop("_t")
    return {"reports": out, "total": len(out), "deep": sum(1 for r in out if r["mode"] != "quick"),
            "quick": sum(1 for r in out if r["mode"] == "quick"),
            "bad": sum(1 for r in out if r["dislikes"] > 0)}


def complaints() -> dict:
    items = [
        {"username": "dorlov", "kind": "ai_answer", "item_key": "msg:91234", "at": "04.11 14:22",
         "question": QUESTIONS[0], "comment": "У ВТБ указана ставка прошлого месяца — 19,5% вместо 18%.",
         "title": None, "mode": "deep", "report_id": "4187", "session_id": "8812", "reasons": ["ошибка в данных"]},
        {"username": "nbelova", "kind": "ai_answer", "item_key": "msg:90871", "at": "03.11 16:05",
         "question": QUESTIONS[6], "comment": "", "title": None, "mode": "quick", "report_id": "4179",
         "session_id": "8790", "reasons": ["ошибка в данных", "мало конкретики"]},
        {"username": "afedorov", "kind": "ai_answer", "item_key": "msg:89410", "at": "30.10 11:47",
         "question": QUESTIONS[5], "comment": "Часть изменений — ещё летние.", "title": None, "mode": "quick",
         "report_id": None, "session_id": "8701", "reasons": ["мало конкретики"]},
        {"username": "ilebedev", "kind": "ai_answer", "item_key": "msg:88702", "at": "27.10 10:15",
         "question": QUESTIONS[7], "comment": "", "title": None, "mode": "quick", "report_id": None,
         "session_id": "8655", "reasons": ["не по делу"]},
        {"username": "ovolkova", "kind": "digest_card", "item_key": "https://news.example/deposits-rates",
         "at": "26.10 09:02", "question": None, "comment": "Это общий фон, а не повод для проверки.",
         "title": "Банки снижают ставки по вкладам вслед за ключевой", "mode": None, "report_id": None,
         "session_id": None, "reasons": ["не по делу"]},
    ]
    for x in items:
        x["name"] = BY_LOGIN[x["username"]]["name"]
        x["verdict"] = -1
    return {"days": 14, "items": items, "total": len(items)}


def card_profile(p: dict) -> dict:
    return {"role_desc": "Аудит розничных продуктов: вклады, карты, ипотека",
            "profile_note": f"{p['name'].split()[0]} проверяет розничные продукты: чаще всего — вклады и "
                            "кредитные карты. Спрашивает про ставки конкурентов и жалобы клиентов.",
            "note_at": "28.10.2026",
            "interests": {"counters": {"products": {"Вклады": 14, "Кредитные карты": 9, "Ипотека": 4},
                                       "banks": {"Сбербанк": 18, "ВТБ": 7}},
                          "pinned": ["Вклады"], "custom": []},
            "timezone": "Europe/Moscow"}


def user_card_detailed(login: str = "akovaleva") -> dict:
    p = BY_LOGIN[login]
    row = next(r for r in users_directory()["users"] if r["username"] == login)
    by_day = [{"d": d, "views": LV_VIEWS[int(c)], "time_s": LV_TIME[int(c)]}
              for d, c in zip(DAYS14, p["act"]) if c != "0"]
    return {
        "user": {k: v for k, v in row.items() if k not in ("excluded", "today", "act14")},
        "profile": card_profile(p),
        "by_day": by_day,
        "pages": [{"page": "ai", "views": 96, "total_s": 14200}, {"page": "reviews", "views": 71, "total_s": 9800},
                  {"page": "overview", "views": 52, "total_s": 4100}, {"page": "market", "views": 18, "total_s": 1500}],
        "questions": [
            {"at": "05.11 10:02", "question": QUESTIONS[4], "mode": "deep", "report_id": None, "answer_len": 0},
            {"at": "04.11 15:31", "question": QUESTIONS[8], "mode": "quick", "report_id": None, "answer_len": 2310},
            {"at": "04.11 11:12", "question": QUESTIONS[9], "mode": "deep", "report_id": "4170", "answer_len": 19800},
            {"at": "03.11 16:40", "question": QUESTIONS[11], "mode": "quick", "report_id": None, "answer_len": 1840}],
        "reports": [
            {"report_id": 4170, "question": QUESTIONS[9], "title": "Блокировки по 161-ФЗ: жалобы за месяц",
             "mode": "deep", "status": "ok", "elapsed_s": 148, "at": "04.11 11:15", "body_len": 19800},
            {"report_id": 4152, "question": QUESTIONS[12], "title": "Детские карты: условия и жалобы",
             "mode": "deep", "status": "ok", "elapsed_s": 131, "at": "29.10 10:05", "body_len": 17600}],
        "ratings": [
            {"kind": "ai_answer", "verdict": 1, "item_key": "msg:91022", "at": "04.11 11:20",
             "question": QUESTIONS[9], "comment": None, "title": None, "report_id": "4170",
             "session_id": "8805", "reasons": []},
            {"kind": "news", "verdict": 1, "item_key": "https://news.example/psk-rules", "at": "03.11 09:14",
             "question": None, "comment": None, "title": "ЦБ уточнил правила раскрытия ПСК",
             "report_id": None, "session_id": None, "reasons": None}],
        "errors": [],
        "trail": [{"at": "05.11 10:02:11", "kind": "page_view", "page": "ai", "dur_ms": None, "status": None},
                  {"at": "05.11 10:01:40", "kind": "page_leave", "page": "reviews", "dur_ms": 412000, "status": None},
                  {"at": "05.11 09:54:48", "kind": "page_view", "page": "reviews", "dur_ms": None, "status": None},
                  {"at": "05.11 09:40:02", "kind": "page_view", "page": "overview", "dur_ms": None, "status": None}],
    }


def user_card_generic() -> dict:
    """Шаблон для любого логина: стенд подставляет user из /api/admin/users и by_day из act14."""
    d = user_card_detailed("akovaleva")
    return {"user": None, "profile": {"role_desc": None, "profile_note": None, "note_at": None,
                                      "interests": {}, "timezone": "Europe/Moscow"},
            "by_day": [], "pages": d["pages"][:2], "questions": d["questions"][1:3],
            "reports": d["reports"][:1], "ratings": [], "errors": [], "trail": d["trail"][:2]}


REPORT_BODY = """# Ставки по вкладам ТОП-10 банков на 6 месяцев

Сравнение максимальных ставок по вкладам на 6 месяцев на 4 ноября 2026 года.

| Банк | Ставка, % | Источник |
|---|---|---|
| Сбербанк | 18,0 | [1] |
| ВТБ | 18,0 | [2] |
| Альфа-Банк | 18,5 | [3] |

## Вывод

Сбербанк держит ставку на уровне ВТБ; Альфа-Банк выше на 0,5 п. п.

[1] sberbank.example/deposits
[2] vtb.example/deposits
[3] alfabank.example/deposits
"""


def report_view() -> dict:
    return {"report_id": 4187, "owner": "dorlov", "session_id": 8812, "question": QUESTIONS[0],
            "title": "Ставки по вкладам ТОП-10 банков на 6 месяцев", "body": REPORT_BODY,
            "payload": {"mode": "deep", "status": "ok", "elapsed_s": 151, "viz": []},
            "banks": ["sberbank", "vtb", "alfabank"], "created_at": iso(at("2026-11-04", "14:01")),
            "owner_name": "Дмитрий Орлов", "admin_view": True}


def session_view() -> dict:
    return {"session": {"session_id": 8790, "title": "Жалобы на кредитные карты Сбера",
                        "username": "nbelova", "name": "Наталья Белова", "at": "03.11.2026 16:01"},
            "messages": [
                {"role": "user", "content": QUESTIONS[6], "meta": {}, "at": "03.11 16:01"},
                {"role": "assistant", "content": "За октябрь 2026 года на кредитные карты Сбербанка — **412 жалоб** "
                 "на площадках отзывов. Чаще всего пишут о неожиданной комиссии за снятие наличных.",
                 "meta": {"mode": "quick"}, "at": "03.11 16:02"}]}


# ── качество ИИ, срезы, охват, траты, итоги ──────────────────────────────────
HERMES_RUNS = [  # новые первыми: дата, модель, запуск, итог, зачёт, частично, провал, медиана
    ("2026-11-02", "oss", "gate", 81.5, 11, 3, 1, 31.0), ("2026-10-26", "oss", "gate", 84.0, 12, 2, 1, 29.0),
    ("2026-10-22", "dsflash", "compare", 82.5, 11, 3, 1, 24.0), ("2026-10-19", "oss", "gate-rollback", 84.0, 12, 2, 1, 30.0),
    ("2026-10-12", "oss", "gate", 82.0, 11, 3, 1, 30.5), ("2026-10-05", "oss", "gate", 83.0, 12, 1, 2, 31.5),
    ("2026-09-28", "oss", "gate", 80.5, 11, 2, 2, 33.0), ("2026-09-21", "oss", "admin", 79.0, 10, 4, 1, 34.0)]
DEEP_RUNS = [
    ("2026-10-30", "default", "gate", 77.0, 8, 3, 1, 151.0), ("2026-10-23", "default", "gate", 76.0, 8, 2, 2, 158.0),
    ("2026-10-16", "default", "gate", 74.0, 7, 4, 1, 162.0), ("2026-10-09", "default", "admin", 75.5, 8, 2, 2, 149.0),
    ("2026-10-02", "default", "gate", 74.0, 7, 3, 2, 171.0), ("2026-09-25", "default", "gate", 72.0, 7, 2, 3, 166.0)]
CASE_NOTES = {"T1": ("fail", "число «жалоб за неделю» = 388", "число 412 против эталона 388 при допуске 2%"),
              "P2": ("partial", "по теме (3/5)", "2 из 10 ставок — прошлого месяца"),
              "P6": ("partial", "ссылки на источники", "нет ссылки на источник"),
              "P7": ("partial", "по теме (2/4)", "не названы два растущих продукта"),
              "D2": ("partial", "ссылки на источники", "нет ссылки на программу ДОМ.РФ"),
              "D7": ("partial", "по теме (3/5)", "пропущены жалобы на льготный период"),
              "D9": ("partial", "по теме (2/4)", "не разобраны схемы с кешбэком"),
              "D10": ("fail", "есть ответ", "ответ оборвался на середине")}


def agent_eval(engine: str) -> dict:
    from bank_audit.ai import agent_eval as A
    runs_src = HERMES_RUNS if engine == "hermes" else DEEP_RUNS
    runs = []
    for i, (d, model, trig, score, p, pa, f, med) in enumerate(runs_src):
        st = at(d, "04:00")
        runs.append({"run_id": (300 if engine == "hermes" else 200) - i, "started_at": iso(st),
                     "finished_at": iso(st + dt.timedelta(minutes=7 if engine == "hermes" else 24)),
                     "model": model, "trigger": trig, "score": score, "n_pass": p, "n_partial": pa,
                     "n_fail": f, "median_s": med, "note": None})
    pool = A.CASES if engine == "hermes" else A.DEEP_CASES
    cases = []
    for c in pool:
        verdict, check, why = CASE_NOTES.get(c.id, ("pass", "по теме (4/4)", ""))
        checks = [{"check": "есть ответ", "ok": verdict != "fail" or c.id != "D10", "hard": True},
                  {"check": check, "ok": verdict == "pass", "hard": False}]
        cases.append({"id": c.id, "tab": c.tab, "title": c.title,
                      "question": f"Проверочный вопрос: {c.title}", "verdict": verdict,
                      "seconds": 28.4 if engine == "hermes" else 152.0, "tools": [], "error": None,
                      "checks": checks,
                      "judge": {"score": 9 if verdict == "pass" else 6 if verdict == "partial" else 3,
                                "correct": verdict != "fail", "key_fact": verdict == "pass",
                                "hallucination": False, "issues": [why] if why else []},
                      "answer": f"Ответ агента на вопрос «{c.title}».\n\n- пункт первый\n- пункт второй",
                      "empty_attempts": None})
    return {"runs": runs, "last_cases": cases, "running": False}


def slices() -> dict:
    from bank_audit.rag import review_slices as rs
    keys = []
    flags = {"payroll": 2, "us": 1, "field": 1}
    for i, k in enumerate(rs.KEYS):
        g = rs.GOLD_0411.get(k)
        cand = [1840, 3120, 960, 410, 280, 150][i]
        keys.append({"key": k, "label": rs.LABELS[k], "schema": "s7",
                     "switched_at": iso(at("2026-11-01", "12:00")), "note": None,
                     "candidates": cand, "tagged": cand - [12, 9, 4, 2, 1, 0][i],
                     "share": round((cand - [12, 9, 4, 2, 1, 0][i]) / cand, 4), "ready": True,
                     "open_flags": flags.get(k, 0),
                     "gold": {"precision": g[0], "recall": g[1], "precision_s6": g[2]} if g else None})
    return {"keys": keys, "spend": {"s6": {"rows": 38200, "rub": 9150}, "s7": {"rows": 41800, "rub": 11320}},
            "schema": "s7", "sieve": {"sieved": 12400, "flagged": 380, "rub": 2140},
            "canary": slice_canary()}


def slice_flags() -> dict:
    from bank_audit.rag import review_slices as rs
    rows = [(71, "payroll", "msokolova", "Зарплата тут ни при чём — жалоба на блокировку карты", "Сбербанк"),
            (72, "payroll", "esmirnova", None, "ВТБ"),
            (74, "us", "dorlov", "Банкомат упомянут вскользь", "Альфа-Банк"),
            (75, "field", "akovaleva", None, "Т-Банк")]
    out = []
    for j, (fid, key, u, note, bank) in enumerate(rows):
        url = f"https://reviews.example/r/{55100 + fid}"
        out.append({"id": fid, "url": url, "key": key, "username": u, "note": note, "bank": bank,
                    "summary": "Клиент жалуется, что карту заблокировали после зачисления перевода.",
                    "label": rs.LABELS[key], "created_at": f"2026-11-0{3 + j % 2} 1{j}:20:00+03:00",
                    "date": "2026-10-29",
                    "evidence": {"key": key, "label": rs.LABELS[key], "clause": 2,
                                 "quote": "зарплата пришла, а карту заблокировали", "schema": "s7",
                                 "flagged": True}})
    return {"items": out}


def reach() -> dict:
    units = [("38", "Московский банк", 64, 9, 8, 7, 61, 6, 41.5), ("55", "СЗБ", 41, 7, 6, 5, 44, 4, 30.2),
             ("42", "Среднерусский", 38, 6, 5, 4, 37, 3, 22.8), ("54", "Поволжский", 36, 5, 4, 3, 21, 2, 15.1),
             ("44", "Волго-Вятский", 33, 4, 4, 3, 18, 2, 12.4), ("16", "Уральский", 35, 4, 3, 3, 26, 2, 11.9),
             ("67", "Сибирский", 31, 3, 3, 2, 12, 1, 8.3), ("70", "Дальневосточный", 22, 2, 2, 1, 6, 1, 4.0),
             ("18", "Байкальский", 20, 2, 1, 1, 3, 0, 2.1), ("52", "Юго-Западный", 30, 2, 2, 1, 9, 1, 3.6),
             ("-", "Центр", 62, 7, 6, 6, 74, 5, 46.8)]
    rows = []
    for code, name, head, users, a30, aiu, ai, reg, hours in units:
        r = {"code": code, "name": name, "head": head, "users": users, "active30": a30, "ai_users": aiu,
             "ai": ai, "regular": reg, "hours": hours, "head_roster": head}
        if code == "38":
            r["head_roster"], r["head_set"] = 61, True
        rows.append(r)
    rows.sort(key=lambda x: (-(x["users"] / x["head"]), -x["users"]))
    for r in rows:
        r["reach"] = round(r["users"] / r["head"], 4)
    head = sum(r["head"] for r in rows)
    users_n = sum(r["users"] for r in rows)
    roles = [("Ведущий аудитор", 140, 21, 168), ("Старший аудитор", 121, 15, 97),
             ("Главный аудитор", 61, 9, 44), ("Руководитель направления", 52, 6, 21), ("Прочие", 38, 0, 0)]
    return {"loaded": True, "loaded_at": iso(at("2026-10-28", "15:12")), "label": "ТБ",
            "head": head, "users": users_n, "reach": round(users_n / head, 4),
            "active30": sum(r["active30"] for r in rows), "matched": {"tab": 38, "name": 13},
            "other": {"users": 13, "active30": 9, "ai": 41}, "head_set": 1, "units": rows,
            "roles": [{"role": k, "head": h, "users": u, "ai": a, "reach": round(u / h, 4)}
                      for k, h, u, a in roles]}


COST_14 = [(8400, 3100), (2100, 600), (1900, 500), (9800, 3900), (10200, 4100), (9100, 3500), (11300, 4200),
           (8800, 2900), (1600, 400), (1400, 300), (11900, 4300), (12600, 4600), (2300, 700), (12400, 4900)]


def costs() -> dict:
    days = []
    d0 = dt.date(2026, 10, 1)
    for i in range(22):                            # 1–22 октября: тот же недельный ритм
        d = d0 + dt.timedelta(days=i)
        dept, app = (1800, 450) if d.weekday() >= 5 else (9200 + (i * 370) % 2400, 3300 + (i * 130) % 900)
        days.append({"day": d.isoformat(), "app": app, "dept": dept})
    for d, (dept, app) in zip(DAYS14, COST_14):
        days.append({"day": d, "app": app, "dept": dept})
    return {"configured": True, "department": "Внутренний аудит розницы",
            "as_of": iso(at("2026-11-05", "09:40")), "synced_at": iso(at("2026-11-05", "09:45")),
            "days_stored": 96,
            "month": {"period": "2026-11", "dept": 41200, "app": 14800, "threshold": 300000.0,
                      "pace": 9300.0, "forecast": 247000, "app_forecast": 88800,
                      "util": round(41200 / 300000, 4), "util_forecast": round(247000 / 300000, 4),
                      "app_share": round(14800 / 41200, 4), "app_median_day": 2900},
            "days": days,
            "months": [{"month": "2026-08", "app": 52100, "dept": 168400},
                       {"month": "2026-09", "app": 61800, "dept": 191300},
                       {"month": "2026-10", "app": 70400, "dept": 214900},
                       {"month": "2026-11", "app": 14800, "dept": 41200}],
            "models": [{"model": "gpt-oss-120b", "month": 9100, "prev": 44800},
                       {"model": "DeepSeek-V4-Flash", "month": 3200, "prev": 14100},
                       {"model": "gemini-3-flash-preview", "month": 1700, "prev": 8300},
                       {"model": "bge-m3", "month": 800, "prev": 3200}]}


QUALITY_COUNTS = {"ok": 214, "followup": 31, "reference": 22, "vague": 28, "internal": 9,
                  "offtopic": 4, "personal": 3, "test": 1}
BAD_TEXTS = {
    "vague": [("Что нового?", 6), ("Расскажи про вклады", 3), ("Что с ипотекой", 2),
              ("Какие новости по картам", 2), ("Что по рынку", 2), ("Покажи жалобы", 2),
              ("Сделай отчёт", 2), ("Расскажи про Сбер", 1), ("Что там у ВТБ", 1), ("Какие ставки", 1),
              ("Что важно сегодня", 1), ("Посмотри отзывы", 1), ("Сравни банки", 1), ("Дай сводку", 1),
              ("Помоги с проверкой", 1), ("Что с тарифами", 1)],
    "internal": [("Сколько жалоб в CRM по отделению 8611 за неделю?", 2),
                 ("Выгрузи обращения клиентов из СБОЛ за октябрь", 2),
                 ("Какой план продаж кредитных карт у Московского банка", 1),
                 ("Покажи внутренний тариф для сотрудников", 1),
                 ("Сколько клиентов закрыли вклады в отделении на Тверской", 1),
                 ("Данные учётной системы по возвратам комиссий", 1),
                 ("Какие лимиты у филиала на выдачу ипотеки", 1)],
    "personal": [("Покажи обращения клиента по фамилии", 1), ("Найди все жалобы конкретного клиента из Казани", 1),
                 ("Номер телефона клиента из отзыва", 1)],
    "offtopic": [("Напиши поздравление коллеге с юбилеем", 1), ("Какая погода в Сочи на выходных", 1),
                 ("Посоветуй книгу про лидерство", 1), ("Переведи письмо на английский", 1)],
    "test": [("тест", 1)]}
WHY = {"vague": "нет продукта, банка или цели", "internal": "нужны закрытые данные банка",
       "personal": "персональные данные", "offtopic": "вне темы банковского аудита",
       "test": "проверка работы", "reference": "справка без аудиторской задачи"}
for _q, _t in BAD_TEXTS.items():
    assert sum(k for _, k in _t) == QUALITY_COUNTS[_q], _q


def outcomes() -> dict:
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from bank_audit.web import outcomes as O
    since = dt.datetime.combine(TODAY - dt.timedelta(days=13), dt.time(), MSK)
    dirs = [c for c, _ in O.DIRS if c != "other"]
    labels = []
    n = 0
    askers = [p["login"] for p in PEOPLE if p["ai"] > 0]
    for q, cnt in QUALITY_COUNTS.items():
        pool = [t for t, k in BAD_TEXTS.get(q, []) for _ in range(k)]
        for i in range(cnt):
            asked = since + dt.timedelta(hours=9 + (n * 7) % (13 * 24))
            labels.append({"src": "ai", "username": askers[n % len(askers)], "asked_at": asked,
                           "question": pool[i] if pool else QUESTIONS[n % len(QUESTIONS)],
                           "dirs": [dirs[n % len(dirs)]] if q in ("ok", "reference") else [],
                           "quality": q, "why": WHY.get(q)})
            n += 1
    for i in range(40):
        labels.append({"src": "vuln_chat", "username": askers[i % len(askers)],
                       "asked_at": since + dt.timedelta(hours=10 + i * 7),
                       "question": "Схемы обхода комиссии за переводы", "dirs": ["credit_card"],
                       "quality": "ok" if i % 8 else "vague", "why": None})
    cases = [(51, "akovaleva", "Ипотека: досрочное погашение", "work", True, 14),
             (52, "dorlov", "Вклады: ставки конкурентов", "collect", False, 9),
             (53, "esmirnova", "Навязанные страховки в потребкредитах", "done", True, 22),
             (54, "nbelova", "Кредитные карты: комиссии", "work", False, 11),
             (55, "kgromov", "Эквайринг для малого бизнеса", "collect", False, 6),
             (56, "ilebedev", "Премиальные карты", "done", True, 8),
             (57, "svkuznetsova", "Детские карты", "collect", False, 0),
             (58, "afedorov", "Блокировки по 161-ФЗ", "work", True, 17),
             (59, "msokolova", "Самозанятые: тарифы", "collect", False, 4)]
    raw = {
        "labels": labels, "titles": {51: ["mortgage"], 54: ["credit_card"], 55: ["acquiring"], 57: ["youth"],
                                     59: ["selfemp"]},
        "case_reviews": [{"case_id": 51, "username": "akovaleva", "product": "Ипотека", "slices": [], "vulnerable": None},
                         {"case_id": 54, "username": "nbelova", "product": "Кредитные карты", "slices": [], "vulnerable": None},
                         {"case_id": 59, "username": "msokolova", "product": None, "slices": ["selfemp"], "vulnerable": None}],
        "review_ui": [{"username": "dorlov", "act": "reviews_product", "product": "Кредитные карты",
                       "d": dt.date(2026, 11, 5)},
                      {"username": "esmirnova", "act": "reviews_export", "product": "Ипотека",
                       "d": dt.date(2026, 11, 3)}],
        "review_view_since": dt.datetime(2026, 10, 20, 9, 0, tzinfo=MSK),
        "subs": [{"username": "esmirnova", "product": "Ипотека"}, {"username": "akovaleva", "product": "Вклады"}],
        "cases": [{"case_id": c, "username": u, "title": t, "status": s, "analysed": a, "items": it}
                  for c, u, t, s, a, it in cases],
        "case_items": {"report": (14, 6), "review": (86, 7), "document": (5, 3)},
        "ai": {"n": 312, "u": len(askers), "deep": 61},
        "runs": {"ok": 288, "failed": 9, "stopped": 6},
        "vuln": {"n": 174, "u": 14},
        "reports": {"n": 105, "u": 29},
        "reopened": 64,
        "shares": {"n": 23, "r": 17},
        "exports": {"n": 31, "u": 12},
        "loophole": {"verdicts": 57, "exports": 9, "u": 8},
        "ratings": {"ai_answer/1": 21, "ai_answer/-1": 4, "news/1": 44, "news/-1": 6},
        "tickets": [{"ticket_id": t[0], "created_at": t[7], "kind": t[2], "section_label": t[4],
                     "body": t[5], "status": t[6]} for t in TICKETS],
        "proposals": [
            {"proposal_id": 33, "created_at": at("2026-11-03", "15:05"), "purpose": "digest", "domain": "cbr.ru/press",
             "title": "Пресс-релизы Банка России", "status": "pending", "review_note": None},
            {"proposal_id": 31, "created_at": at("2026-10-30", "11:20"), "purpose": "digest", "domain": "frankrg.com",
             "title": "Frank Media — новости банков", "status": "pending", "review_note": None},
            {"proposal_id": 27, "created_at": at("2026-10-14", "10:00"), "purpose": "reviews", "domain": "otzovik.com",
             "title": "Отзовик — отзывы о банках", "status": "approved", "review_note": "подключили 20.10"},
            {"proposal_id": 22, "created_at": at("2026-09-30", "12:00"), "purpose": "tariffs", "domain": "vbr.ru",
             "title": "Выберу.ру — тарифы", "status": "rejected", "review_note": "агрегатор без первоисточника"}],
    }
    res = O._build(raw, since)
    res["period"] = {"days": 14, "since": since.isoformat()}
    res["prev"] = {"questions": 274, "askers": 35, "vuln_queries": 150, "cases": 7, "case_items": 90,
                   "good_share": 0.77}
    res["labels"] = {"last_run": {"started_at": iso(at("2026-11-05", "03:00")),
                                  "finished_at": iso(at("2026-11-05", "03:04")), "trigger": "night",
                                  "labeled": 41, "error": None},
                     "pending": 12, "hour_msk": 3}
    return res


def me(kind: str) -> dict:
    login, name = OWNER if kind == "owner" else PULSE_USER
    owner = kind == "owner"
    return {"username": login, "has_email": True, "name": name, "timezone": "Europe/Moscow",
            # onboarded и mail_promo=again — без заметки «настройте под себя» и приглашения почты
            "prefs": {"onboarded": True, "mail_promo": "again", "mail_promo_at": "2026-10-20",
                      **({"pulse_hidden": True} if owner else {})},
            "created_at": "2026-10-02T09:00:00+03:00" if owner else "2026-10-06T10:30:00+03:00",
            "interests": {"banks": ["Сбербанк"], "products": ["Вклады"], "pinned": [], "muted": [], "custom": []},
            "recommendations": ["Кредитные карты"], "profile_note": None, "profile_note_at": None,
            "personalization": {"score": 46, "parts": [
                {"key": "desc", "label": "Описание зоны ответственности", "earned": 25, "max": 25,
                 "done": True, "cta": "Опишите, что вы проверяете в Сбере", "target": "profile"}],
                "counts": {"queries": 12, "content_ratings": 3, "ai_ratings": 1}},
            "is_admin": owner, "can_pulse": True, "can_ingest": owner, "authenticated": True}


def banner() -> dict:
    return {"id": "turbulence", "on": False, "active": False, "until": None, "title": None,
            "body": None, "v": None}


def build() -> dict[str, object]:
    return {
        "me.owner.json": me("owner"),
        "me.pulse_user.json": me("pulse_user"),
        "admin_pulse.json": pulse(),
        "admin_users.json": users_directory(),
        "admin_user_card.akovaleva.json": user_card_detailed("akovaleva"),
        "admin_user_card.json": user_card_generic(),
        "admin_reports.json": reports_all(),
        "admin_complaints.json": complaints(),
        "admin_inbox.new.json": inbox_list("new"),
        "admin_inbox.open.json": inbox_list("open"),
        "admin_inbox.all.json": inbox_list("all"),
        "admin_inbox_ticket.118.json": ticket_118(),
        "admin_agent_eval.hermes.json": agent_eval("hermes"),
        "admin_agent_eval.deep.json": agent_eval("deep"),
        "admin_pulse_slices.json": slices(),
        "admin_pulse_reach.json": reach(),
        "admin_pulse_costs.json": costs(),
        "admin_pulse_outcomes.json": outcomes(),
        "reviews_slice_flags.json": slice_flags(),
        "admin_banner.json": banner(),
        "report.json": report_view(),
        "admin_session.json": session_view(),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(name)


if __name__ == "__main__":
    main()
