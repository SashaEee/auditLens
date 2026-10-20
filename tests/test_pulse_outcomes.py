"""«Пульс» → «Итоги»: правила разметки запросов, направления жалоб в делах,
сборка отчёта из строк и права на эндпоинты. Postgres и модель не нужны."""
from __future__ import annotations

import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.web import outcomes as O  # noqa: E402

MSK = O.MSK


def test_merge_label_check_overrides_first():
    first = {"dirs": ["mortgage", "nonsense", "mortgage"], "quality": "vague", "why": "общий", "model": "fast"}
    lab = O.merge_label("ставки по ипотеке у Сбера и ВТБ", first, {"quality": "ok", "why": "x", "model": "strong"})
    assert lab == {"dirs": ["mortgage"], "quality": "ok", "why": None, "model": "strong"}
    # без проверки — как решила первая; непонятная пригодность — «годный»
    assert O.merge_label("x", {"dirs": [], "quality": "vague", "why": "нет предмета"}, None)["quality"] == "vague"
    assert O.merge_label("x", {"dirs": [], "quality": "???"}, None)["quality"] == "ok"
    assert O.merge_label("x", None, None) is None


def test_old_start_cards_are_templates():
    card = "Какие основные жалобы у клиентов Сбербанка? Где подводные камни?"
    lab = O.merge_label(card + "  ", {"dirs": ["other"], "quality": "vague", "why": "общий"},
                        {"quality": "vague", "why": "общий"})
    assert lab["quality"] == "template" and lab["why"] is None


def test_query_dirs():
    assert O.query_dirs(["youth", "bad"], "ok") == ["youth"]
    assert O.query_dirs([], "ok") == ["other"]
    assert O.query_dirs(None, "vague") == ["other"]
    assert O.query_dirs([], "followup") == [] and O.query_dirs([], "test") == []


def test_review_dirs_prefer_case_topic():
    # тема дела «Дети 6–13 лет» важнее продукта жалобы (дебетовая карта)
    assert O.review_dirs(["youth"], "Дебетовая карта", None, None) == ["youth"]
    # тема ни о чём («сбермобайл» → other) — по самой жалобе
    assert O.review_dirs(["other"], "Ипотека", ["us"], ["minor"]) == ["atm", "mortgage", "youth"]
    assert O.review_dirs(None, "Вклад", None, None) == ["other"]
    assert O.review_dirs(None, None, None, ["svo"]) == ["svo"]


def test_period():
    today = dt.date(2026, 10, 20)
    assert O.period(0, today) == (None, None, None)
    since, until, prev = O.period(7, today)
    assert since == dt.datetime(2026, 10, 14, tzinfo=MSK) and until is None
    assert prev == dt.datetime(2026, 10, 7, tzinfo=MSK)


def _raw():
    t = dt.datetime(2026, 10, 15, 12, tzinfo=MSK)
    lab = lambda src, u, q, quality, dirs, why=None: {  # noqa: E731
        "src": src, "username": u, "asked_at": t, "question": q, "dirs": dirs, "quality": quality, "why": why}
    return {
        "labels": [lab("ai", "a", "ипотека с господдержкой", "ok", ["mortgage"]),
                   lab("ai", "b", "ипотека и кредитки", "ok", ["mortgage", "credit_card"]),
                   lab("ai", "a", "да, хочу", "followup", []),
                   lab("ai", "c", "сравни банки", "vague", [], "нет предмета"),
                   lab("ai", "d", "Сравни банки ", "vague", [], "нет предмета"),
                   lab("vuln_chat", "a", "Все о частном лице", "personal", ["other"], "частное лицо"),
                   lab("vuln_search", "b", "лазейки по детским картам", "ok", ["youth"])],
        "titles": {1: ["field"], 2: ["other"]},
        "case_reviews": [{"case_id": 1, "username": "a", "product": "Дебетовая карта", "slices": None, "vulnerable": None},
                         {"case_id": 2, "username": "b", "product": "Инвестиции", "slices": None, "vulnerable": None}],
        "review_ui": [{"username": "a", "act": "reviews_product", "product": "Ипотека", "d": dt.date(2026, 10, 21)},
                      {"username": "a", "act": "reviews_product", "product": "Ипотека", "d": dt.date(2026, 10, 21)},
                      {"username": "a", "act": "reviews_export", "product": "Кредитная карта", "d": dt.date(2026, 10, 21)}],
        "review_view_since": dt.datetime(2026, 10, 21, 10, tzinfo=MSK),
        "subs": [{"username": "c", "product": "Зарплатные проекты"}],
        "cases": [{"case_id": 1, "username": "a", "title": "Доставка", "status": "collect", "analysed": False, "items": 1},
                  {"case_id": 2, "username": "b", "title": "сбермобайл", "status": "work", "analysed": True, "items": 0}],
        "case_items": {"review": (2, 2)},
        "ai": {"n": 5, "u": 4, "deep": 1}, "runs": {"ok": 3, "stopped": 1},
        "vuln": {"n": 2, "u": 2}, "reports": {"n": 3, "u": 2}, "reopened": 1, "shares": {"n": 2, "r": 1},
        "exports": {"n": 1, "u": 1}, "loophole": {"verdicts": 4, "exports": 1, "u": 2},
        "ratings": {"ai_answer/1": 2},
        "tickets": [{"ticket_id": 2, "created_at": t, "kind": "idea", "section_label": "Аудит отзывов",
                     "body": "добавить УС", "status": "done"},
                    {"ticket_id": 1, "created_at": t - dt.timedelta(days=30), "kind": "other",
                     "section_label": "Аудит уязвимостей", "body": "новый форум", "status": "new"}],
        "proposals": [{"proposal_id": 1, "created_at": t, "purpose": "reviews", "domain": "2gis.ru", "title": "2ГИС",
                       "status": "pending", "review_note": None},
                      {"proposal_id": 2, "created_at": t, "purpose": "reviews", "domain": "x.ru", "title": "тест",
                       "status": "rejected", "review_note": "тест"}],
    }


def test_build_report():
    rep = O._build(_raw(), since=dt.datetime(2026, 10, 14, tzinfo=MSK))
    d = {x["code"]: x for x in rep["dirs"]}
    assert d["mortgage"]["ai"] == 2 and d["mortgage"]["ai_users"] == 2
    assert d["credit_card"]["ai"] == 1 and d["other"]["ai"] == 2          # «сравни банки» ×2 без направления
    assert d["youth"]["vuln"] == 1 and d["other"]["vuln"] == 1
    # жалоба в деле «Доставка» → выездное; дело «сбермобайл» → по продукту жалобы
    assert d["field"]["rev_parts"] == {"case": 1} and d["invest"]["rev_parts"] == {"case": 1}
    # просмотр одного продукта за день — один раз; выгрузка и подписка — по продукту
    assert d["mortgage"]["rev_parts"] == {"view": 1} and d["credit_card"]["rev_parts"] == {"export": 1}
    assert d["payroll"]["rev"] == 1 and d["payroll"]["rev_users"] == 1
    assert d["field"]["cases"] == 1 and d["other"]["cases"] == 1
    q = rep["quality"]["ai"]
    assert q["total"] == 5 and q["bad"] == 2 and q["good"] == 3 and q["good_share"] == 0.6
    # одинаковые запросы с разным регистром — одна строка с числом повторов
    vague = [b for b in rep["bad_examples"] if b["quality"] == "vague"]
    assert len(vague) == 1 and vague[0]["n"] == 2
    assert rep["bad_examples"][0]["section"] in ("ИИ-помощник", "Аудит уязвимостей")
    f = rep["funnel"]
    assert f["cases"] == 2 and f["cases_collect"] == 1 and f["cases_work"] == 1 and f["cases_analysed"] == 1
    assert f["cases_empty"] == 1 and f["case_reviews"] == 2 and f["subs"] == 1
    assert rep["tickets"]["total"] == 2 and rep["tickets"]["done"] == 1 and rep["tickets"]["new_in_period"] == 1
    # отклонённые источники в список не попадают, но в счёт — да
    assert rep["sources"]["rejected"] == 1 and [x["domain"] for x in rep["sources"]["items"]] == ["2gis.ru"]
    assert rep["review_view_since"].startswith("2026-10-21")


def test_endpoints_rights(monkeypatch):
    from fastapi.testclient import TestClient

    from bank_audit.web import app as A
    monkeypatch.setenv("ADMIN_USERS", "owner-1")
    monkeypatch.setenv("PULSE_USERS", "viewer-1")
    seen = {}
    monkeypatch.setattr(O, "report", lambda v, wm, days: seen.update(v=v, days=days) or {"ok": 1})
    monkeypatch.setattr(O, "pending_count", lambda: 7)

    async def fake_label(trigger="night", limit=None):
        seen["trigger"] = trigger
        return {"labeled": 0}
    monkeypatch.setattr(O, "label_new", fake_label)
    c = TestClient(A.app)
    h = lambda u: {"X-Authentik-Username": u, "X-Authentik-Name": u}  # noqa: E731
    assert c.get("/api/admin/pulse/outcomes", headers=h("user-1")).status_code == 403
    assert c.get("/api/admin/pulse/outcomes?days=7", headers=h("viewer-1")).json() == {"ok": 1}
    assert seen == {"v": "viewer-1", "days": 7}
    assert c.post("/api/admin/pulse/outcomes/label", headers=h("viewer-1")).status_code == 403
    r = c.post("/api/admin/pulse/outcomes/label", headers=h("owner-1"))
    assert r.status_code == 200 and r.json()["pending"] == 7
