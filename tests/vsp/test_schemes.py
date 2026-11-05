"""Сборка схем из описаний: группы по смыслу, название моделью, отметка «новая»."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from sqlalchemy import select

from bank_audit.vsp.models import Review, utcnow
from bank_audit.vsp.signals import llm_read as lr
from bank_audit.vsp.signals import schemes as sc
from bank_audit.vsp.storage import Store, meta


class FakeReader:
    def __init__(self, *a, **k):
        self.rub = 0.0

    async def ask(self, user):
        name = "Платная подписка без согласия" if "подписк" in user else "Чужие платежи из наличных"
        return {"name": name, "description": "клиенты пишут", "relevance": 3, "check": ["проверить"],
                "benign": ["акция"]}, 0.0


def test_build_groups_and_novelty(db_url, monkeypatch):
    st = Store(db_url)
    now = utcnow()
    st.save_reviews([Review("2gis", f"r{i}", "g1", 1, "текст", now - timedelta(days=10 if i < 4 else 400))
                     for i in range(8)])
    meta.create_all(st.engine, tables=[lr.review_llm])
    rows = []
    for i in range(8):
        summ = "подключили подписку без согласия" if i < 4 else "в чеке чужой платёж из наличных"
        rows.append({"review_id": f"2gis:r{i}", "prompt_version": lr.PROMPT_VERSION, "text_hash": "x", "model": "f",
                     "relevant": 1, "severity": 2, "staff": "сотрудник", "summary": summ, "scheme": summ,
                     "data_json": "{}", "cost_rub": 0, "at": now})
    with st.engine.begin() as c:
        c.execute(lr.review_llm.insert(), rows)
    monkeypatch.setattr(sc, "_embed", lambda texts: [[1.0, 0.0] if "подписк" in t else [0.0, 1.0] for t in texts])
    monkeypatch.setattr(sc, "Reader", FakeReader)
    res = asyncio.run(sc.build(st, k=2, min_relevance_rows=4))
    assert res["groups"] == 2 and res["novel"] == 1
    with st.engine.connect() as c:
        g = {r.name: r for r in c.execute(select(sc.scheme_t))}
    assert g["Платная подписка без согласия"].novel == 1 and g["Чужие платежи из наличных"].novel == 0
    assert json.loads(g["Платная подписка без согласия"].check) == ["проверить"]
