"""API вкладки «Аудит сети ВСП» на синтетической сети: доступ, обзор, сигналы, профиль, отзывы, выгрузки."""
from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank_audit.vsp.registry.enrich import enrich_branches
from bank_audit.vsp.signals.run import run
from bank_audit.vsp.storage import Store
from bank_audit.web import vsp_api
from bank_audit.web.auth import CurrentUser, get_current_user

from test_signals import _background, _campaign, _net, _rev  # noqa: E402  (соседний модуль тестов)

UTC = timezone.utc


@pytest.fixture
def client(db_url, monkeypatch):
    st = Store(db_url)
    ids = _net(st)
    enrich_branches(st)
    _background(st, ids, random.Random(3))
    _campaign(st, "3", datetime(2026, 3, 2, 10, tzinfo=UTC))
    st.save_reviews([_rev("g1", "nm", 2, datetime(2026, 4, 1, tzinfo=UTC), "x",
                          text="Менеджер Анна нагрубила и навязала страховку")])
    st.save_reviews([_rev("g6", f"bad{i}", 1, datetime(2026, 2, 3 + 9 * i, 12, tzinfo=UTC), f"badauth{i}", text=t)
                     for i, t in enumerate(["Менеджер Ольга навязала страховку к кредиту",
                                            "Менеджер Ольга навязала подписку, без неё не одобрят",
                                            "Оформили кредитную карту без моего согласия, менеджер Ольга молчала"])])
    run(st)
    monkeypatch.setenv("VSP_DB_URL", db_url)
    monkeypatch.setenv("ADMIN_USERS", "owner")
    monkeypatch.delenv("VSP_USERS", raising=False)
    monkeypatch.setattr(vsp_api, "_ENGINE", None)
    vsp_api._CACHE.update(run=None, at=0.0)
    app = FastAPI()
    app.include_router(vsp_api.router, prefix="/api/vsp")
    who = {"u": "owner"}
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(who["u"], who["u"], True)
    c = TestClient(app)
    c.who = who
    c.ids = ids
    return c


def test_access_only_owner_or_list(client, monkeypatch):
    assert client.get("/api/vsp/access").json() == {"allowed": True}
    client.who["u"] = "colleague"
    assert client.get("/api/vsp/overview").status_code == 403
    monkeypatch.setenv("VSP_USERS", "colleague")
    assert client.get("/api/vsp/overview").status_code == 200


def test_overview_signals_profile_reviews(client):
    ov = client.get("/api/vsp/overview").json()
    assert ov["kpi"]["points"] == 11 and ov["kpi"]["in_focus"] >= 1
    assert f"b{client.ids['3']}" in {c["point"] for c in ov["candidates"]} and ov["tiles"] and ov["calendar"]
    sig = client.get("/api/vsp/signals", params={"period": "all"}).json()
    assert sig["total"] == 1 and sig["items"][0]["place"]["city"]
    card = client.get(f"/api/vsp/signals/{sig['items'][0]['id']}").json()
    assert card["series"] and card["guide"]["check"] and len(card["reviews"]) >= 50
    assert any(d["u5"] >= 5 for d in card["series"]) and card["features"]["why"]
    prof = client.get(f"/api/vsp/branches/b{client.ids['3']}").json()
    assert prof["episodes_full"] and prof["timeline"] and prof["cards_full"]
    bl = client.get("/api/vsp/branches", params={"focus": 1}).json()
    assert bl["total"] >= 1
    # имена сотрудников показываются как есть; фильтр «упомянут сотрудник» их находит
    rv = client.get("/api/vsp/reviews", params={"point": f"b{client.ids['1']}", "flag": "neg", "q": "навязала"}).json()
    assert rv["total"] == 1 and "Анна" in rv["items"][0]["text"]
    rv2 = client.get("/api/vsp/reviews", params={"point": f"b{client.ids['1']}", "flag": "names"}).json()
    assert any("Анна" in x["text"] for x in rv2["items"])
    ep = client.get("/api/vsp/reviews", params={"flag": "episode"}).json()
    assert ep["total"] >= 50 and all(x["in_episode"] for x in ep["items"])
    assert client.get("/api/vsp/links").json()["same_text"] is not None
    assert ov["kpi2"]["focus"] >= 1 and ov["kpi2"]["severe_12m"] >= 1 and ov["top_cases"]
    cs = client.get("/api/vsp/cases", params={"period": "all"}).json()
    assert cs["total"] >= 3 and cs["items"][0]["severity"] == 3 and cs["items"][0]["text"]
    assert {t["key"] for t in cs["types"]} >= {"unauthorized", "mis_selling", "favoritism"}
    pp = client.get("/api/vsp/people").json()
    olga = next(x for x in pp["items"] if "Ольга" in x["name"])
    assert olga["kind"] == "complaints" and olga["authors"] == 3
    assert len(client.get(f"/api/vsp/people/{olga['id']}").json()["reviews"]) == 3
    comp = client.get("/api/vsp/completeness").json()
    assert comp["cards"]["2gis"]["cards"] == 11


def test_exports(client):
    r = client.get("/api/vsp/export/candidates.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = client.get("/api/vsp/export/reviews.xlsx", params={"flag": "episode"})
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_home_dossier_schemes_search_feedback_exports(client, monkeypatch):
    from bank_audit.web import telemetry
    monkeypatch.setattr(telemetry, "track_batch", lambda *a, **k: 1)
    h = client.get("/api/vsp/home").json()
    b6 = f"b{client.ids['6']}"
    assert h["source"] == "слова" and h["tiles"] and any(x["point"] == b6 for x in h["list"])
    row = next(x for x in h["list"] if x["point"] == b6)
    assert row["why"] and row["quote"]["text"] and row["confidence"]["level"]
    assert client.get("/api/vsp/home", params={"subject": "KGD"}).json()["list"] == []
    d = client.get(f"/api/vsp/dossier/{b6}").json()
    assert d["risk"]["request"] and d["risk"]["check"] and len(d["bars"]) == 24 and d["stories"]
    sc = client.get("/api/vsp/schemes").json()
    assert sc["kind"] == "действия" and sc["all"]
    card = client.get(f"/api/vsp/schemes/{sc['all'][0]['id']}").json()
    assert card["examples"] and card["examples"][0]["place"]["city"]
    s = client.get("/api/vsp/search", params={"q": "навязала"}).json()
    assert s["reviews"] and s["reviews"][0]["snippet"]
    assert client.get("/api/vsp/search", params={"q": "Москва"}).json()["places"]
    assert client.post("/api/vsp/feedback", json={"target": b6, "verdict": "полезно"}).json() == {"ok": True}
    assert client.post("/api/vsp/feedback", json={"target": b6, "verdict": "ерунда"}).status_code == 400
    r = client.get("/api/vsp/export/justification.xlsx", params={"points": b6})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = client.get("/api/vsp/export/visit.docx", params={"point": b6})
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_stories_table_staff_trends_peers(client):
    b6 = f"b{client.ids['6']}"
    st = client.get("/api/vsp/stories", params={"period": ""}).json()
    assert st["total"] >= 3 and st["items"][0]["place"]["city"] and st["actions"] and len(st["months"]) == 24
    key = st["actions"][0]["key"]
    st2 = client.get("/api/vsp/stories", params={"period": "", "action": key}).json()
    assert 0 < st2["total"] <= st["total"] and all(key in x["actions"] for x in st2["items"])
    assert st2["actions"] == st["actions"]                      # грани считаются без своего фильтра
    assert client.get("/api/vsp/stories", params={"period": "", "q": "Ольга"}).json()["total"] >= 2
    assert client.get("/api/vsp/stories", params={"period": "", "named": True}).json()["total"] >= 2
    sev = client.get("/api/vsp/stories", params={"period": "", "sort": "severe"}).json()["items"]
    assert [x["severity"] for x in sev] == sorted((x["severity"] for x in sev), reverse=True)
    r = client.get("/api/vsp/export/stories.xlsx", params={"period": ""})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    tb = client.get("/api/vsp/table", params={"period": ""}).json()
    row = next(x for x in tb["items"] if x["point"] == b6)
    assert row["stories"] >= 3 and row["heavy"] >= 1 and row["actions"] and row["city"]
    assert client.get("/api/vsp/table", params={"period": "", "sort": "last"}).json()["sort"] == "last"
    sf = client.get("/api/vsp/staff").json()
    assert any("Ольга" in x["name"] for x in sf["complaints"]) and "middlemen" in sf and "praise" in sf
    tr = client.get("/api/vsp/trends").json()
    assert len(tr["months"]) == 24 and "gosbs" in tr and "actions" in tr
    h = client.get("/api/vsp/home").json()
    assert h["summary"]["actions"] is not None and h["gosb_table"] and "in_scope" in h
    act = st["actions"][0]["key"]
    hf = client.get("/api/vsp/home", params={"action": act}).json()
    assert hf["filtered"] and len(hf["list"]) <= len(h["list"])
    d = client.get(f"/api/vsp/dossier/{b6}").json()
    assert len(d["compare"]) == 3 and d["actions"] and d["stories"][0]["labels"]
    r = client.get("/api/vsp/export/visit.docx", params={"points": f"{b6},b{client.ids['1']}"})
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_evidence_passport_links_meta(client):
    b6 = f"b{client.ids['6']}"
    st = client.get("/api/vsp/stories", params={"period": "", "point": b6}).json()
    rid = st["items"][0]["rid"]
    assert "src" in st["items"][0]                                  # площадка у каждой истории
    ev = client.get(f"/api/vsp/evidence/{rid}").json()
    assert ev["review"]["text"] and ev["quote"] and ev["point"] == b6 and ev["checks"]
    assert ev["checks"][0]["ok"] is True                           # цитата из правил — дословно в тексте
    assert client.get("/api/vsp/evidence/нет-такого").status_code == 404
    d = client.get(f"/api/vsp/dossier/{b6}").json()
    assert d["passport"]["cards"] and d["passport"]["cards"][0]["source"] == "2gis"
    m = client.get("/api/vsp/meta").json()
    assert m["tiles"] and m["run"]["id"] and m["progress"]["reviews"]["2gis"] > 0 and m["progress"]["cards"]["2gis"] == 11
    t1 = client.get("/api/vsp/table", params={"period": "", "limit": 1}).json()
    t2 = client.get("/api/vsp/table", params={"period": "", "limit": 1, "offset": 1}).json()
    assert t1["total"] >= 2 and t1["items"][0]["point"] != t2["items"][0]["point"]
    r = client.get("/api/vsp/export/justification.xlsx", params={"gosb": "x", "action": "посредник"})
    assert r.status_code == 200


def test_people_names_in_nominative_and_split():
    from bank_audit.web.vsp_api import _nom, _split_people
    assert [_nom(x) for x in ("Светланой", "Анне Петровне", "Натальей", "Алексей", "Сергей", "Иван")] == \
        ["Светлана", "Анна Петровна", "Наталья", "Алексей", "Сергей", "Иван"]
    assert _split_people("Таласпаева Улбасым, Кух Юлия") == ["Таласпаева Улбасым", "Кух Юлия"]
