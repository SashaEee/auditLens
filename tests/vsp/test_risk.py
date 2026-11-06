"""«Где стоит проверить» на карточках модели: признаки, уверенность, «почему», карта, схемы, полоса."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from bank_audit.vsp.models import utcnow
from bank_audit.vsp.registry.enrich import enrich_branches
from bank_audit.vsp.signals import llm_read as lr
from bank_audit.vsp.signals import risk as rk
from bank_audit.vsp.signals.run import latest_run, run
from bank_audit.vsp.storage import Store, branch_stats, meta, network

from test_signals import _background, _net, _rev  # noqa: E402  (соседний модуль тестов)

UTC = timezone.utc


def _card(rid, **kw):
    d = {"relevant": True, "severity": 2, "fault": "сотрудник", "actions": ["оформление_без_согласия"],
         "episode": "подключили платную подписку без согласия", "quote": "подключили подписку без моего согласия",
         "quote_ok": True, "employee_name": None, "vulnerability": [], "products": ["подписка"], "money_rub": None,
         "other_vsp": None, **kw}
    return {"review_id": rid, "prompt_version": lr.PROMPT_VERSION, "text_hash": "x", "model": "fake",
            "relevant": 1, "severity": d["severity"], "staff": d["fault"], "summary": d["episode"], "scheme": "",
            "data_json": json.dumps(d, ensure_ascii=False), "cost_rub": 0.0, "at": utcnow()}


def test_risk_list_from_model_cards(db_url):
    st = Store(db_url)
    ids = _net(st)
    enrich_branches(st)
    _background(st, ids, random.Random(5))
    now = utcnow()
    texts = ["Менеджер Ольга подключила подписку без моего согласия, деньги списали",
             "Ольга оформила мне кредитную карту без согласия, сказала что дебетовая",
             "Без моего согласия подключили пакет услуг, сотрудница Ольга даже не объяснила"]
    st.save_reviews([_rev("g6", f"r{i}", 1, now - timedelta(days=20 + 30 * i), f"client{i}", total=7, text=t)
                     for i, t in enumerate(texts)])
    st.save_reviews([_rev("g7", "heavy", 1, now - timedelta(days=3), "victim", total=4,
                          text="Сотрудница взяла телефон мамы и перевела себе 50 тысяч рублей, маме 80 лет")])
    meta.create_all(st.engine, tables=[lr.review_llm])
    cards = [_card(f"2gis:g6-r{i}", employee_name="Ольга") for i in range(3)]
    cards.append(_card("2gis:g7-heavy", severity=3, actions=["доступ_к_телефону_или_аккаунту"], money_rub=50000,
                       vulnerability=["пенсионер/пожилой"], quote="взяла телефон мамы и перевела себе 50 тысяч"))
    with st.engine.begin() as c:
        c.execute(lr.review_llm.insert(), cards)
    out = run(st)
    assert out["story_source"] == "модель" and out["stories"] == 4 and out["risk_listed"] == 2
    rid = latest_run(st)
    with st.engine.connect() as c:
        det = {r.point: json.loads(r.detail_json) for r in c.execute(select(branch_stats).where(branch_stats.c.run_id == rid))}
        net = {r.key: json.loads(r.value_json) for r in c.execute(select(network).where(network.c.run_id == rid))}
    a6 = det[f"b{ids['6']}"]["risk"]
    assert a6["include"] and {s["key"] for s in a6["signals"]} >= {"A", "C"}
    assert a6["confidence"]["level"] == "высокая" and "3 разных клиента" in a6["why"][0]
    assert any("Ольга" in w for w in a6["why"]) and not any(rk.FORBIDDEN.search(w) for w in a6["why"])
    assert a6["benign"]["resolve"] and a6["request"] and a6["check"] and a6["people"][0]["clients"] == 3
    a7 = det[f"b{ids['7']}"]["risk"]
    assert a7["include"] and a7["severity"] == 3 and a7["red"] and a7["n_signals"] < 2      # одна очень тяжёлая
    assert net["risk_react"][0]["point"] == f"b{ids['7']}"
    sch = {x["id"]: x for x in net["schemes_fallback"]}
    assert sch["оформление_без_согласия"]["stories"] == 3 and sch["оформление_без_согласия"]["points"] == 1
    assert net["risk_map"]["subjects"]["MOW"]["listed"] == 2 and det[f"b{ids['6']}"]["stories"][0]["quote"]


def test_rules_fallback_marks_preliminary(db_url):
    st = Store(db_url)
    ids = _net(st)
    enrich_branches(st)
    now = utcnow()
    st.save_reviews([_rev("g2", f"q{i}", 1, now - timedelta(days=10 + i), f"a{i}", total=5,
                          text="Оформили кредитную карту без моего согласия, не объяснили условия")
                     for i in range(2)])
    out = run(st)
    assert out["story_source"] == "слова"
    rid = latest_run(st)
    with st.engine.connect() as c:
        d = json.loads(c.execute(select(branch_stats.c.detail_json).where(
            branch_stats.c.run_id == rid, branch_stats.c.point == f"b{ids['2']}")).scalar())
    assert "модель ещё читает" in d["risk"]["confidence"]["reason"]


def test_same_review_on_two_branches_is_marked_even_with_different_quotes():
    from bank_audit.vsp.signals.risk import _mark_dups
    t = "мою маму пенсионерку обманули на 59 500 продали не ту услугу за которой она пришла деньги не возвращают"
    xs = [{"point": "b1", "text": t, "quote": "Мою маму пенсионерку обманули на 59.500"},
          {"point": "b2", "text": t, "quote": "продали не ту услугу за которой она пришла"},
          {"point": "b3", "text": "совсем другой отзыв про кассу где недодали деньги при размене крупной купюры", "quote": "x"}]
    out = _mark_dups(xs)
    assert [s["dup_points"] for s in out] == [1, 1, 0]
