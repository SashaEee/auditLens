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
