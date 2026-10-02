"""Заголовок выпуска и типы из базы.

28.09 утренний выпуск собрался без заголовка: повод «новая лазейка в
продуктах Сбера» нёс уверенность находки как Decimal (numeric в базе), и
json.dumps при записи секции упал — на главной висела вчерашняя сводка.
БД не нужна: сессия подменяется.
"""
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from bank_audit.digest import store, writer

MSK = timezone(timedelta(hours=3))


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params or {}))
        return _Result(self.rows)


def _patch_session(monkeypatch, module, sess):
    @contextmanager
    def fake():
        yield sess
    monkeypatch.setattr(module.db, "session", fake)


def test_upsert_writes_decimal_and_dates_as_json(monkeypatch):
    sess = _Session()
    _patch_session(monkeypatch, store, sess)
    store.upsert(date(2026, 9, 28), "headline", {
        "insights": [{"data": {"verdict_confidence": Decimal("0.95"),
                               "collected_at": datetime(2026, 9, 25, 14, 55, tzinfo=MSK),
                               "day": date(2026, 9, 25)}}]})
    payload = json.loads(sess.calls[0][1]["p"])
    data = payload["insights"][0]["data"]
    assert data["verdict_confidence"] == 0.95
    assert data["collected_at"] == "2026-09-25T14:55:00+03:00"
    assert data["day"] == "2026-09-25"


def test_sber_loophole_lead_has_plain_types(monkeypatch):
    sess = _Session([{
        "record_id": 76928,
        "title": "СберПрайм за 1 рубль\nпо промокоду   из акции",
        "url": None, "verdict_reason": "промокод продлевает подписку",
        "verdict_confidence": Decimal("0.95"),
        "collected_at": datetime(2026, 9, 25, 14, 55, tzinfo=MSK),
    }])
    _patch_session(monkeypatch, writer, sess)
    rows = writer._new_sber_loopholes()
    assert rows[0]["verdict_confidence"] == 0.95 and isinstance(rows[0]["verdict_confidence"], float)
    assert rows[0]["collected_at"] == "2026-09-25T14:55:00+03:00"
    assert rows[0]["title"] == "СберПрайм за 1 рубль по промокоду из акции"
    sql = sess.calls[0][0]
    assert "nullif(headline, '')" in sql          # короткий заголовок находки
    assert "summary_doubt" in sql                 # сомнительные — не в выпуск

    monkeypatch.setattr(writer, "_sber_rating_move", lambda: None)
    leads, _ = writer._build_leads({}, set())
    loop = [ld for ld in leads if ld["kind"] == "loophole"]
    assert loop and loop[0]["ref"] == "loop:76928"
    json.dumps(loop, ensure_ascii=False)          # без default — чистые типы


def test_sber_loopholes_db_error_is_not_fatal(monkeypatch):
    @contextmanager
    def broken():
        raise RuntimeError("нет базы")
        yield
    monkeypatch.setattr(writer.db, "session", broken)
    assert writer._new_sber_loopholes() == []
