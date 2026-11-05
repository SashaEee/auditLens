"""Чтение отзывов моделью: два этапа, повтор пропущенного, дубли текста один раз, спам-аккаунты мимо."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import select

from bank_audit.vsp.models import Review
from bank_audit.vsp.signals import llm_read as lr
from bank_audit.vsp.storage import Store

BAD = "Оформили кредит без моего согласия, менеджер обманула, теперь долг 50 тысяч рублей, верните деньги"
QUEUE = "Очень долгая очередь, стояли час, никто не помог, ужасное обслуживание в офисе, не советую никому"


class FakeReader(lr.Reader):
    def __init__(self, model="fake", system=""):
        self.model, self.system, self.rub, self.calls, self.errors = model, system, 0.0, 0, 0
        self.drop, self.sem, self.screen = set(), asyncio.Semaphore(4), system == lr.SYSTEM_SCREEN

    async def ask(self, user):
        self.calls += 1
        self.rub += 0.1
        texts = [t for t in (BAD, QUEUE) if t in user]
        n = user.count("Отзыв:")
        if self.screen:
            return {"items": [{"n": k + 1, "episode": "кредит" in user.split("[")[k + 1], "why": "x"}
                              for k in range(n)]}, 0.1
        if "Прочитай каждый отзыв" in user:                     # пакет
            return {"items": [{"n": 1, "relevant": True, "episode": "оформили кредит без согласия", "severity": 3,
                               "fault": "сотрудник", "scheme_free": "кредит без согласия", "money_rub": 50000,
                               "quote": "Оформили кредит без моего согласия"}]}, 0.1
        return {"relevant": bool(texts and texts[0] == BAD), "episode": "x", "severity": 2, "fault": "сотрудник",
                "quote": "не из текста"}, 0.1


def test_two_stage_with_dups_and_spam(db_url, monkeypatch):
    st = Store(db_url)
    d = datetime(2026, 5, 1, tzinfo=timezone.utc)
    items = [Review("2gis", "r0", "g1", 1, BAD, d, author_ref="a0"), Review("2gis", "r1", "g1", 1, QUEUE, d, author_ref="a1"),
             Review("2gis", "r2", "g2", 1, BAD, d, author_ref="a2")]                       # дубль текста r0
    items += [Review("2gis", f"sp{i}", f"g{i}", 1, f"политический спам номер {i} " + QUEUE, d, author_ref="spam")
              for i in range(lr.SPAM_AUTHOR_MIN)]
    st.save_reviews(items)
    monkeypatch.setattr(lr, "Reader", FakeReader)
    res = asyncio.run(lr.run(st, limit=100))
    assert res["screen"]["read_unique"] == 2 and res["screen"]["saved"] == 3                 # спам мимо, дубль один раз
    assert res["card"]["read_unique"] == 1 and res["card"]["saved"] == 2
    with st.engine.connect() as c:
        rows = {(r.review_id, r.prompt_version): r for r in c.execute(select(lr.review_llm))}
    card = rows[("2gis:r0", lr.PROMPT_VERSION)]
    assert card.severity == 3 and card.staff == "сотрудник" and rows[("2gis:r2", lr.PROMPT_VERSION)].severity == 3
    data = json.loads(card.data_json)
    assert data["money_rub"] == 50000 and data["quote_ok"] is True
    assert rows[("2gis:r1", lr.SCREEN_VERSION)].relevant == 0
    assert asyncio.run(lr.run(st, limit=100))["screen"]["read_unique"] == 0                  # прочитанное не перечитывается


def test_quote_ok_accepts_fragments_joined_by_ellipsis():
    from bank_audit.vsp.signals.llm_read import quote_ok
    text = ("Сотрудник банка выписала нам 2 квитанции, ничего не разъясняя. Оказалось, нам без объяснений оформили "
            "2 разные страховки. На вопрос сотрудник ответила, что и эта страховка является обязательной.")
    assert quote_ok("оказалось, нам без объяснений оформили 2 разные страховки... сотрудник ответила, что и эта "
                    "страховка является обязательной", text)
    assert not quote_ok("нам без объяснений оформили 2 разные страховки... и заставили подписать отказ", text)
    assert quote_ok("Сотрудник банка выписала нам 2 квитанции", text) and not quote_ok("", text)


def test_short_complaints_are_read_short_praise_is_not():
    from bank_audit.vsp.signals.llm_read import long_enough
    assert long_enough("Подключение услуг в тихую", 1) and long_enough("Девушка с краю сразу страховки впаривает", 3)
    assert not long_enough("Всё отлично, спасибо большое!", 5) and not long_enough("Норм", 1)
    assert long_enough("x" * 60, 5) and not long_enough("Подключение услуг в тихую", None)
