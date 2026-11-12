"""Раздел «Ожидание»: сводка, список жалоб, отделения, досье, выгрузка — на синтетической сети."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank_audit.vsp.models import utcnow
from bank_audit.vsp.registry.enrich import enrich_branches
from bank_audit.vsp.signals import llm_read as lr
from bank_audit.vsp.signals import wait as W
from bank_audit.vsp.signals.run import run
from bank_audit.vsp.storage import Store, meta
from bank_audit.web import vsp_api, vsp_wait
from bank_audit.web.auth import CurrentUser, get_current_user

from test_signals import _net, _rev  # noqa: E402  (соседний модуль тестов)

UTC = timezone.utc
NOW = datetime.now(UTC)


def _mark(st: Store, rid: str, text: str, *, wait=1, minutes=None, cause="не сказано", left=0, quote=None):
    data = {"wait": wait, "minutes": minutes, "cause": cause, "left": left, "quote": quote, "quote_ok": bool(quote)}
    with st.engine.begin() as c:
        c.execute(lr.review_llm.insert().values(
            review_id=rid, prompt_version=W.VERSION, text_hash=lr._hash(text), model="fake", relevant=wait,
            staff=cause, summary=quote, data_json=json.dumps(data, ensure_ascii=False), cost_rub=0.0, at=utcnow()))


@pytest.fixture
def client(db_url, monkeypatch):
    st = Store(db_url)
    ids = _net(st)
    enrich_branches(st)
    meta.create_all(st.engine, tables=[lr.review_llm])
    rnd = random.Random(5)
    items, marks = [], []
    k = 0
    for ref in ids:                                          # фон: у каждого отделения 12 жалоб, из них 3 про ожидание
        for i in range(12):
            at = NOW - timedelta(days=rnd.randint(5, 300))
            wait = i < 3
            text = (f"Ждали в очереди {20 + i} минут, работает одно окно. Номер {k}" if wait
                    else f"Нахамили и отказали без объяснения причин, обращение номер {k}")
            items.append(_rev(f"g{ref}", f"n{k}", 1, at, f"a{k}", text=text))
            if wait:
                marks.append((f"2gis:g{ref}-n{k}", text, dict(minutes=20 + i, cause="мало окон",
                                                             quote=f"Ждали в очереди {20 + i} минут")))
            k += 1
    for i in range(14):                                      # отделение 3: ждут почти все, подолгу, уходят
        at = NOW - timedelta(days=3 + i * 9)
        text = f"Просидела {70 + i} минут,так и не приняли — ушла. Сотрудники болтают между собой. Случай {i}"
        items.append(_rev("g3", f"w{i}", 2, at, f"w{i}", text=text))
        marks.append((f"2gis:g3-w{i}", text, dict(minutes=70 + i, cause="заняты своим", left=1,
                                                 quote=f"Просидела {70 + i} минут, так и не приняли")))
    old = NOW - timedelta(days=500)
    items.append(_rev("g1", "old", 1, old, "old", text="Очередь была на два часа, ужас, давно это было"))
    marks.append(("2gis:g1-old", "Очередь была на два часа, ужас, давно это было", dict(minutes=120)))
    items.append(_rev("g1", "no", 1, NOW - timedelta(days=9), "no", text="Взял талон, а в окне нахамили и отказали"))
    st.save_reviews(items)
    for rid, text, kw in marks:
        _mark(st, rid, text, **kw)
    _mark(st, "2gis:g1-no", "Взял талон, а в окне нахамили и отказали", wait=0)
    run(st)
    monkeypatch.setenv("VSP_DB_URL", db_url)
    monkeypatch.setenv("VSP_WAIT_SINCE", "2024-01-01")
    monkeypatch.setenv("ADMIN_USERS", "owner")
    monkeypatch.delenv("VSP_USERS", raising=False)
    monkeypatch.setattr(vsp_api, "_ENGINE", None)
    vsp_api._CACHE.update(run=None, at=0.0)
    vsp_wait.reset()
    app = FastAPI()
    app.include_router(vsp_api.router, prefix="/api/vsp")
    who = {"u": "owner"}
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(who["u"], who["u"], True)
    c = TestClient(app)
    c.who, c.ids = who, ids
    yield c
    vsp_wait.reset()


def test_summary_counts_shares_and_marks(client):
    d = client.get("/api/vsp/wait").json()
    k = d["kpi"]
    assert k["n"] == 11 * 3 + 14 and k["points"] == 11 and k["neg"] == 11 * 12 + 14 + 1
    assert 0.3 < k["share"] < 0.34 and k["long"] == 14 and k["left"] == 14 and k["sig"] == 1
    assert d["headline"]["text"] == "За год — 47 жалоб на долгое ожидание: это 32\u00a0% всех жалоб на отделения"
    assert "значимо чаще" in d["headline"]["accent"]
    top = d["branches"][0]
    assert top["point"] == f"b{client.ids['3']}" and top["n"] == 17 and top["sig"] and top["left"] == 14
    assert top["cause_label"] == "Сотрудники заняты не клиентами" and top["city"]
    assert sum(1 for b in d["branches"] if b["sig"]) == 1
    assert {c["key"]: c["n"] for c in d["causes"]} == {"мало окон": 33, "заняты своим": 14}
    assert sum(b["n"] for b in d["durations"]) == k["n"] and d["durations"][2]["n"] == 14
    assert len(d["months"]) >= 12 and sum(m["n"] for m in d["months"] if m["month"] >= d["since_month"]) == k["n"]
    assert sum(m["n"] for m in d["months"]) == k["n"] + 1               # в ряду по месяцам видна и старая жалоба
    assert d["progress"]["waiting"] == 0 and d["progress"]["read"] == 49
    assert {r["code"] for r in d["regions"]} >= {"MOW", "KGD"} and d["gosbs"]


def test_filters_scope_period_cause(client):
    kgd = client.get("/api/vsp/wait", params={"subject": "KGD"}).json()
    assert kgd["kpi"]["n"] == 9 and kgd["kpi"]["points"] == 3 and kgd["scoped"]
    assert kgd["kpi"]["net_share"] > kgd["kpi"]["share"]              # в Калининграде без «плохого» отделения — ниже сети
    left = client.get("/api/vsp/wait", params={"left": 1}).json()
    assert left["kpi"]["n"] == 14 and left["kpi"]["points"] == 1 and left["filtered"]
    assert left["branches"][0]["n"] == 14 and left["branches"][0]["n_all"] == 17
    assert client.get("/api/vsp/wait", params={"cause": "мало окон"}).json()["kpi"]["n"] == 33
    assert client.get("/api/vsp/wait", params={"per": "730"}).json()["kpi"]["n"] == 48   # добавилась старая жалоба
    assert client.get("/api/vsp/wait", params={"cause": "выдумка"}).json()["kpi"]["n"] == 47
    quarter = client.get("/api/vsp/wait", params={"per": "90"}).json()
    assert 0 < quarter["kpi"]["n"] < 47


def test_list_is_verbatim_and_sorted(client):
    b3 = f"b{client.ids['3']}"
    ls = client.get("/api/vsp/wait/list", params={"point": b3, "left": 1, "sort": "long"}).json()
    assert ls["total"] == 14 and ls["items"][0]["minutes"] == 83 and ls["items"][0]["minutes_label"] == "1 ч 23 мин"
    it = ls["items"][0]
    assert it["quote"] in it["text"] and "минут,так и не приняли" in it["quote"]     # из текста, а не из ответа модели
    assert it["place"]["point"] == b3 and it["cause_label"] and it["left"] is True
    assert client.get("/api/vsp/wait/list", params={"q": "болтают"}).json()["total"] == 14
    allp = client.get("/api/vsp/wait/list", params={"per": "all", "limit": 5}).json()
    assert allp["total"] == 48 and len(allp["items"]) == 5
    assert allp["items"][0]["date"] >= allp["items"][1]["date"]
    assert client.get("/api/vsp/wait/list", params={"offset": 45}).json()["items"].__len__() == 2


def test_branches_dossier_export_access(client):
    br = client.get("/api/vsp/wait/branches", params={"sort": "share"}).json()
    assert br["total"] == 11 and br["sig"] == 1 and br["items"][0]["point"] == f"b{client.ids['3']}"
    last = client.get("/api/vsp/wait/branches", params={"sort": "last", "limit": 3}).json()
    assert len(last["items"]) == 3 and last["items"][0]["last"] >= last["items"][1]["last"]
    d = client.get(f"/api/vsp/dossier/b{client.ids['3']}").json()
    w = d["wait"]
    assert w["n"] == 17 and w["sig"] and w["long"] == 14 and len(w["items"]) == 3 and w["ratio"] > 1.5
    assert w["items"][0]["quote"]
    calm = client.get(f"/api/vsp/dossier/b{client.ids['1']}").json()["wait"]
    assert calm["n"] == 3 and calm["n_all"] == 4 and not calm["sig"]
    r = client.get("/api/vsp/export/wait.xlsx", params={"subject": "MOW"})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    client.who["u"] = "stranger"
    for url in ("/api/vsp/wait", "/api/vsp/wait/list", "/api/vsp/wait/branches", "/api/vsp/export/wait.xlsx"):
        assert client.get(url).status_code == 403


def test_evidence_layer_shows_wait_complaint(client):
    rid = client.get("/api/vsp/wait/list", params={"left": 1, "sort": "long"}).json()["items"][0]["rid"]
    e = client.get(f"/api/vsp/evidence/{rid}").json()
    w = e["wait"]
    assert w["minutes"] == 83 and w["minutes_label"] == "1 ч 23 мин" and w["left"] is True
    assert w["cause_label"] == "Сотрудники заняты не клиентами" and w["quote"] in e["review"]["text"]
    assert e["quote"] == w["quote"] and e["point"] == f"b{client.ids['3']}"
    assert any("долгое ожидание" in c["label"] for c in e["checks"])
    plain = client.get("/api/vsp/evidence/2gis:g1-no").json()            # талон без жалобы на время
    assert plain["wait"] is None

