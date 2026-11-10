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


def test_home_dossier_search_feedback_exports(client, monkeypatch):
    from bank_audit.web import telemetry
    monkeypatch.setattr(telemetry, "track_batch", lambda *a, **k: 1)
    h = client.get("/api/vsp/home").json()
    b6 = f"b{client.ids['6']}"
    assert h["source"] == "слова" and isinstance(h["list"], list) and set(h["shelves"]) >= {"cases"}
    assert h["tested"] >= 1 and h["points"] == 11 and "in_scope" in h
    assert client.get("/api/vsp/home", params={"subject": "KGD"}).json()["list"] == []
    d = client.get(f"/api/vsp/dossier/{b6}").json()
    assert d["risk"]["check"] and len(d["bars"]) == 24 and d["stories"] and d["kinds"] and d["hot"] == []
    assert d["risk"]["heavy_clients"] >= 1 and d["risk"]["why"] and "request" not in d["risk"]
    assert all({"what", "where", "period", "text"} <= set(r) for r in d["request"])
    assert len(d["compare"]) == 2 and d["stories"][0]["labels"] and d["stories"][0]["kind_name"]
    s = client.get("/api/vsp/search", params={"q": "навязала"}).json()
    assert s["reviews"] and s["reviews"][0]["snippet"]
    assert client.get("/api/vsp/search", params={"q": "Москва"}).json()["places"]
    assert client.post("/api/vsp/feedback", json={"target": b6, "verdict": "полезно"}).json() == {"ok": True}
    assert client.post("/api/vsp/feedback", json={"target": b6, "verdict": "ерунда"}).status_code == 400
    for url, prm in (("justification.xlsx", {"points": b6}), ("visit.docx", {"point": b6}), ("request.docx", {"point": b6}),
                     ("visit.docx", {"points": f"{b6},b{client.ids['1']}"})):
        r = client.get(f"/api/vsp/export/{url}", params=prm)
        assert r.status_code == 200 and r.content[:2] == b"PK", url
    assert client.get("/api/vsp/export/request.docx").status_code == 404
    for gone in ("schemes", "staff", "trends"):                       # прежние виды заменены волнами и очагами
        assert client.get(f"/api/vsp/{gone}").status_code == 404


def test_stories_table_peers(client):
    b6 = f"b{client.ids['6']}"
    st = client.get("/api/vsp/stories", params={"period": ""}).json()
    assert st["total"] >= 3 and st["items"][0]["place"]["city"] and st["kinds"] and len(st["months"]) == 24
    kind = st["kinds"][0]["kind"]
    st2 = client.get("/api/vsp/stories", params={"period": "", "kind": kind}).json()
    assert 0 < st2["total"] <= st["total"] and all(x["kind"] == kind for x in st2["items"])
    assert st2["kinds"] == st["kinds"]                          # грани считаются без своего фильтра
    assert client.get("/api/vsp/stories", params={"period": "", "q": "Ольга"}).json()["total"] >= 2
    assert client.get("/api/vsp/stories", params={"period": "", "named": True}).json()["total"] >= 2
    sev = client.get("/api/vsp/stories", params={"period": "", "sort": "severe"}).json()["items"]
    assert [x["severity"] for x in sev] == sorted((x["severity"] for x in sev), reverse=True)
    assert client.get("/api/vsp/stories", params={"period": "", "sort": "important"}).json()["total"] == st["total"]
    r = client.get("/api/vsp/export/stories.xlsx", params={"period": ""})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    tb = client.get("/api/vsp/table", params={"period": ""}).json()
    row = next(x for x in tb["items"] if x["point"] == b6)
    assert row["stories"] >= 3 and row["heavy"] >= 1 and row["clients"] >= 1 and row["city"] and "shelf" in row
    assert client.get("/api/vsp/table", params={"period": "", "sort": "last"}).json()["sort"] == "last"
    hf = client.get("/api/vsp/home", params={"kind": kind}).json()
    assert hf["filtered"] and "cases" in hf["shelves"]


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
    r = client.get("/api/vsp/export/justification.xlsx", params={"gosb": "x", "kind": 13})
    assert r.status_code == 200


def test_people_names_in_nominative_and_split():
    from bank_audit.web.vsp_api import _nom, _split_people
    assert [_nom(x) for x in ("Светланой", "Анне Петровне", "Натальей", "Алексей", "Сергей", "Иван")] == \
        ["Светлана", "Анна Петровна", "Наталья", "Алексей", "Сергей", "Иван"]
    assert _split_people("Таласпаева Улбасым, Кух Юлия") == ["Таласпаева Улбасым", "Кух Юлия"]


def test_brief_and_map_without_model(client):
    b = client.get("/api/vsp/brief").json()
    assert b["headline"]["text"] and b["headline"]["kind"] in ("count", "wave", "hot")
    assert {"clients_2y", "clients_year", "clients_prev", "points_year", "read"} <= set(b["kpi"])
    assert b["kpi"]["clients_2y"] >= 1 and isinstance(b["waves"], list) and isinstance(b["hotspots"], list)
    assert set(b["shelves"]) >= {"sure", "pattern", "rows", "tested"} and "watch" in b
    m = client.get("/api/vsp/map").json()
    assert m["regions"] and m["gosbs"] and m["key"] is None and m["period_days"] == 730
    r = next(x for x in m["regions"] if x["points"])
    assert {"n", "expected", "ratio", "sig", "few", "col", "row", "read", "listed"} <= set(r)
    assert all(not x["sig"] for x in m["regions"] if x["few"])          # мало отзывов — никогда не «значимо»
    assert client.get("/api/vsp/map", params={"key": "k999"}).json()["key"] is None    # нет такого способа — вся карта
    assert client.get("/api/vsp/waves/k999").status_code == 404
    assert client.get("/api/vsp/hotspots/нет").status_code == 404


@pytest.fixture
def scene(db_url, monkeypatch):
    """Сеть с карточками модели, почерками и выводом по отделению — всё, что показывает вкладка."""
    import asyncio
    from bank_audit.vsp.signals import branch_llm, motifs, risk
    from test_motifs_branch import Editor, Reader
    from test_risk import Scene
    monkeypatch.setattr(risk, "HQ_CODES", {"9038/1005"})
    monkeypatch.setattr(motifs, "PROPOSE_MIN", 1)
    st = Store(db_url)
    sc = Scene(st).build()
    asyncio.run(motifs.build(st, reader=Editor()))
    run(st)
    asyncio.run(branch_llm.run(st, reader=Reader()))
    run(st)
    monkeypatch.setenv("VSP_DB_URL", db_url)
    monkeypatch.setenv("ADMIN_USERS", "owner")
    monkeypatch.delenv("VSP_USERS", raising=False)
    monkeypatch.setattr(vsp_api, "_ENGINE", None)
    monkeypatch.setattr(vsp_api, "_notify_waves", lambda run_: None)
    vsp_api._CACHE.update(run=None, at=0.0)
    vsp_api._WATCH.update(v=None, at=0.0)
    vsp_api._PROG.update(v=None, at=0.0)
    app = FastAPI()
    app.include_router(vsp_api.router, prefix="/api/vsp")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("owner", "owner", True)
    c = TestClient(app)
    c.sc = sc
    return c


def test_waves_hotspots_shelves_queue_on_model_cards(scene):
    c, ids = scene, scene.sc.ids
    b6 = f"b{ids['6']}"
    m = c.get("/api/vsp/meta").json()
    assert m["nav"]["up"] >= 1 and m["nav"]["hot"] >= 1 and m["nav"]["pattern"] >= 1 and m["nav"]["sure"] == 0
    assert m["watch"]["state"] == "off" and m["kinds"][0]["name"] and m["typed_since"] == "2021"
    # «Главное»: заголовок — новая волна, герой и «то же в других отделениях» — её истории
    b = c.get("/api/vsp/brief").json()
    lead = next(x for x in b["waves"] if x["id"] == b["headline"]["wave_id"])
    assert b["headline"]["kind"] == "wave" and b["headline"]["text"].startswith("Новое:") and lead["status"] == "new"
    assert lead["level"] == "motif" and b["hero"]["kind"] == lead["kind"] and b["same_total"] == lead["year"]
    assert b["request"]["kind"] == lead["kind"] and "Прошу предоставить" in b["request"]["text"]
    assert not [x for x in b["waves"] if x["level"] == "kind" and x["status"] == "new" and x["kind"] == lead["kind"]]
    assert b["hotspots"] and b["shelves"]["pattern"] >= 1 and b["shelves"]["rows"][0]["why"]
    assert b["cases"][0]["rid"] == c.sc.inside_rid and b["inside_total"] >= 1 and b["cases_fresh"] >= 1
    assert {x["label"] for x in b["benefit"]} >= {"план продаж", "сотрудник лично"}
    kgd = c.get("/api/vsp/brief", params={"subject": "KGD"}).json()         # в регионе заголовок — его очаг
    assert kgd["scoped"] and kgd["headline"]["kind"] == "hot" and "Калининград" in kgd["headline"]["text"]
    # «Схемы»: волны — типы и почерки, карточка способа
    w = c.get("/api/vsp/waves").json()
    assert w["has_motifs"] and {x["level"] for x in w["waves"]} == {"kind", "motif"} and w["counts"]["kind"]["new"] >= 1
    bio = next(x for x in w["waves"] if x["level"] == "motif" and x["kind"] == 15)
    assert bio["hot"] == 1 and bio["text"] and bio["accent"] and len(bio["series"]) == 48 and bio["status_label"]
    card = c.get(f"/api/vsp/waves/{bio['id']}").json()
    assert card["hotspots"] and card["examples"] and card["profile"]["n"] == 12 and card["ops"] == "платежи за биометрию"
    assert card["request"]["what"] == "Платежи за биометрию" and card["request"]["metric"] == "доля возвратов"
    assert card["siblings"] == [] and card["parent"]["id"] == "k15" and card["branches"][0]["n"] >= 2
    assert "k8" in {x["id"] for x in c.get("/api/vsp/waves", params={"level": "kind", "status": "new"}).json()["waves"]}
    kcard = c.get("/api/vsp/waves/k15").json()
    assert [s_["id"] for s_ in kcard["siblings"]] == [bio["id"]]             # у типа — его почерки
    # очаги: список и карточка с историями, отделениями и запросом
    hs = c.get("/api/vsp/hotspots").json()
    h = hs["hotspots"][0]
    assert hs["counts"]["strong"] >= 1 and h["wave_id"] == bio["id"] and h["recent"] and h["points_n"] == 3
    assert "Калининград" in h["text"] or "8626" in h["text"]
    hc = c.get(f"/api/vsp/hotspots/{h['id']}").json()
    assert len(hc["stories"]) == 7 and len(hc["places"]) == 3 and hc["wave"]["id"] == bio["id"] and hc["limits"]
    assert "8626/" in hc["request"]["where"] and hc["request"]["period"].startswith("с ")
    assert c.get("/api/vsp/hotspots", params={"subject": "MOW"}).json()["hotspots"] == []
    # карта по способу действия
    mp = c.get("/api/vsp/map", params={"key": bio["id"]}).json()
    assert mp["key"] == bio["id"] and next(r for r in mp["regions"] if r["code"] == "KGD")["sig"]
    assert any(k["id"] == bio["id"] for k in c.get("/api/vsp/map").json()["keys"])
    # полки и очередь случаев
    hm = c.get("/api/vsp/home").json()
    row = next(x for x in hm["list"] if x["point"] == b6)
    assert row["shelf"] == "pattern" and row["shelf_label"] and row["sig"][0]["text"] and row["conclusion"]["pattern"]
    assert "rids" not in row["patterns"][0] and row["lead"]["rid"]
    pg = c.get("/api/vsp/home", params={"shelf": "pattern", "limit": 1}).json()
    assert len(pg["list"]) == 1 and pg["list_total"] == hm["shelves"]["pattern"] and pg["shelves"] == hm["shelves"]
    assert c.get("/api/vsp/home", params={"shelf": "sure"}).json()["list"] == []
    q = c.get("/api/vsp/queue").json()
    assert q["items"][0]["rid"] == c.sc.inside_rid and q["items"][0]["why"] and q["inside"] >= 1 and "src" in q["items"][0]
    assert c.get("/api/vsp/queue", params={"inside": True}).json()["total"] == q["inside"]
    assert c.get("/api/vsp/queue", params={"subject": "KGD", "kind": 9}).json()["total"] == 0
    # досье: почерк, вывод модели, в каких очагах, запрос с периодом причины
    d = c.get(f"/api/vsp/dossier/{b6}").json()
    assert d["shelf_label"] and d["risk"]["conclusion"]["pattern_name"] and sum(s_["in_pattern"] for s_ in d["stories"]) >= 4
    assert d["request"][0]["kind"] == 7 and "9038/1006" in d["request"][0]["where"]
    d100 = c.get(f"/api/vsp/dossier/b{ids['100']}").json()
    assert d100["hot"] and d100["hot"][0]["id"] == h["id"]
    assert c.get(f"/api/vsp/dossier/b{ids['5']}").json()["hq"] is True
    # проверка истории: уточнение модели, перенос по коду, копия, волны
    ev = c.get(f"/api/vsp/evidence/{c.sc.inside_rid}").json()
    assert ev["detail"]["benefit"] == "сотрудник лично" and ev["detail"]["inside"] and ev["detail"]["intent"]
    assert ev["waves"] == []                                              # единичная история — не волна
    bv = c.get("/api/vsp/evidence/2gis:g100-bio0").json()
    assert {x["id"] for x in bv["waves"]} == {"k15", bio["id"]} and bv["detail"]["motif"] == bio["name"]
    mv = c.get(f"/api/vsp/evidence/{c.sc.moved_rid}").json()
    assert mv["point"] == f"b{ids['2']}" and any("другого отделения" in x["label"] for x in mv["checks"])
    cp = c.get("/api/vsp/evidence/2gis:g6-p0copy").json()
    assert any("в подсчётах он один" in x["label"] for x in cp["checks"]) or \
        any("в подсчётах он один" in x["label"] for x in c.get("/api/vsp/evidence/2gis:g6-p0").json()["checks"])
    # истории: по способу, «изнутри», без копий в счёте клиентов
    ks = c.get("/api/vsp/stories", params={"period": "", "key": bio["id"], "severity": 2}).json()
    assert ks["total"] == 12 and ks["wave"]["id"] == bio["id"] and all(x["motif_name"] for x in ks["items"])
    ins = c.get("/api/vsp/stories", params={"period": "", "inside": True}).json()
    assert ins["total"] >= 1 and all(x["inside"] for x in ins["items"])
    b6s = c.get("/api/vsp/stories", params={"period": "", "point": b6}).json()
    assert b6s["total"] == 5 and b6s["clients"] == 4
    tb = c.get("/api/vsp/table", params={"period": ""}).json()
    assert next(x for x in tb["items"] if x["point"] == b6)["clients"] == 4
    # выгрузки
    for url, prm in (("request.docx", {"hotspot": h["id"]}), ("request.docx", {"wave": bio["id"]}),
                     ("justification.xlsx", {"shelf": "pattern"}), ("stories.xlsx", {"key": bio["id"], "period": ""}),
                     ("visit.docx", {"point": b6})):
        r = c.get(f"/api/vsp/export/{url}", params=prm)
        assert r.status_code == 200 and r.content[:2] == b"PK", url


def test_words_helpers():
    from bank_audit.web.vsp_api import _period, _pl, _wave_id, _wave_key, _x
    assert [_pl(n, "отделение", "отделения", "отделений") for n in (1, 3, 5, 11, 21, 104)] == \
        ["отделение", "отделения", "отделений", "отделений", "отделение", "отделения"]
    assert _x(0.94) == "0,9" and _x(3.0) == "3" and _x(12) == "12" and _x(None) == "—" and _x(120.4) == "120"
    assert _period("2025-03-10T00:00:00+00:00", "2025-09-02T00:00:00+00:00") == "с марта по сентябрь 2025"
    assert _period("2024-11-10T00:00:00+00:00", "2025-05-02T00:00:00+00:00") == "с ноября 2024 по май 2025"
    assert _period("2025-05-01T00:00:00+00:00", "2025-05-28T00:00:00+00:00") == "в мае 2025"
    assert _wave_key("k5") == "k:5" and _wave_key("m0503") == "m:m0503" and _wave_key("x") is None
    assert _wave_id("k:5") == "k5" and _wave_id("m:m0503") == "m0503"


def test_beta_access_from_settings(client, monkeypatch):
    from bank_audit.web import vsp_api
    monkeypatch.setattr(vsp_api, "beta_users", lambda: ["colleague"])
    client.who["u"] = "colleague"
    assert client.get("/api/vsp/access").json() == {"allowed": True}
    m = client.get("/api/vsp/meta").json()
    assert m["access"]["owner"] is False and m["access"]["beta"] is None
    assert client.get("/api/vsp/beta").status_code == 403                 # управлять бетой — только владельцу
    client.who["u"] = "stranger"
    assert client.get("/api/vsp/meta").status_code == 403
    client.who["u"] = "owner"
    assert client.get("/api/vsp/meta").json()["access"] == {"owner": True, "beta": 1}


def test_beta_invite_letter():
    from bank_audit.web import vsp_invite
    m = vsp_invite.render("Светлана Фукс", {"read": 468466, "stories": 33687, "reviews": 774618, "up": 3})
    assert m["subject"].startswith("Вы в бета-тесте") and "Светлана, вы в бета-тесте" in m["html"]
    assert "468 466" in m["html"] and "#vsp" in m["html"] and "Обратная связь" in m["text"]
    assert "Фукс" not in m["html"]                                          # обращение только по имени
    assert "способов действия растут" in m["html"] and "срочно" not in m["html"] + m["text"]
    assert "None" not in vsp_invite.render(None, {"read": 1, "stories": 1, "reviews": 1})["text"]
