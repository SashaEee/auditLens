"""Демон дописывания: расписание, «кому пора», цикл источника, паузы, обходы (без сети)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import update

from bank_audit.vsp import mass
from bank_audit.vsp.collectors.base import Area, Capabilities, Collector
from bank_audit.vsp.collectors.yandex import YandexMapsCollector
from bank_audit.vsp.config import Settings
from bank_audit.vsp.http import SourceHttp
from bank_audit.vsp.models import Review, ReviewPage, SourceBlocked, SourceBranch, utcnow
from bank_audit.vsp.storage import Store, sync_state
from bank_audit.vsp.sync import schedule_next, sync_branch


@pytest.fixture
def store(db_url):
    return Store(db_url)


def run(c):
    return asyncio.run(c)


def _rev(src, sid, k, days):
    return Review(source=src, source_review_id=f"{sid}-{k}", source_branch_id=sid, rating=5, review_text=f"т{k}",
                  review_date=utcnow() - timedelta(days=days), collection_method="t")


class FakeSrc(Collector):
    """Источник «2gis» для демона: отзывы по отделениям, блокировка по желанию."""
    source = "2gis"

    def __init__(self, settings, data, *, block=False):
        super().__init__(settings)
        self.data, self.block, self.calls = data, block, []
        self.capabilities = Capabilities(method="t", sorts=("date_edited",), full_history=True)

    async def discover(self, area, *, seen=None, patience=2):
        for sid in self.data:
            yield SourceBranch("2gis", sid, city=area.city)

    async def branch(self, sid):
        if self.block:
            raise SourceBlocked("2gis", "captcha")
        return SourceBranch("2gis", sid, declared_reviews=len(self.data[sid]))

    async def pages(self, sid, *, sort=None, cursor=None):
        self.calls.append(sid)
        yield ReviewPage(self.data[sid], page_no=0, next_cursor=None, declared_total=len(self.data[sid]))


def test_schedule_follows_review_rate(store):
    s = Settings()
    col = FakeSrc(s, {})
    now = utcnow()
    store.save_reviews([_rev("2gis", "busy", k, k % 89) for k in range(90)])        # ~1 в день
    store.save_reviews([_rev("2gis", "hot", k, k % 89) for k in range(900)])        # ~10 в день
    store.save_reviews([_rev("2gis", "old", k, 400 + k) for k in range(30)])        # всё старое
    h = {sid: (schedule_next(store, col, sid, full_pass=True, now=now)["next_due_at"] - now).total_seconds() / 3600
         for sid in ("busy", "hot", "old", "empty")}
    assert h["busy"] == pytest.approx(24, rel=0.02)
    assert h["hot"] == 6 and h["old"] == 48 and h["empty"] == 48                   # пределы 6 ч и двое суток
    nf = schedule_next(store, col, "busy", full_pass=True, now=now)["next_full_at"] - now
    assert timedelta(days=30) <= nf < timedelta(days=33)                          # месяц + сдвиг до 3 суток


def test_due_selection(store):
    for sid in ("a", "b", "c", "d", "e"):
        store.upsert_source_branch(SourceBranch("2gis", sid))
    now = utcnow()
    store.put_state("2gis", "b", status="done", last_full_at=now, next_due_at=now - timedelta(hours=1),
                    next_full_at=now + timedelta(days=20))
    store.put_state("2gis", "c", status="done", last_full_at=now, next_due_at=now + timedelta(hours=5),
                    next_full_at=now + timedelta(days=20))
    store.put_state("2gis", "d", status="failed", next_due_at=now + timedelta(hours=3))   # повтор позже
    store.put_state("2gis", "e", status="running", cursor="50")                            # прерванный
    store.set_availability("2gis", "a", "not_found")                                       # как после NotFound
    store.put_state("2gis", "a", status="failed", next_due_at=now + timedelta(days=30),
                    next_full_at=now + timedelta(days=30))
    due = store.due("2gis", now)
    assert due["full"] == ["e"] and due["new"] == ["b"]
    store.put_state("2gis", "a", next_full_at=now - timedelta(minutes=1))                  # пора перепроверить
    assert "a" in store.due("2gis", now)["full"]


def test_source_cycle_full_then_new(store):
    s = Settings()
    data = {"x": [_rev("2gis", "x", 1, 2)], "y": [_rev("2gis", "y", 1, 300)]}
    for sid in data:
        store.upsert_source_branch(SourceBranch("2gis", sid))
    fake = FakeSrc(s, data)
    out = run(mass.source_cycle(store, s, "2gis", factory=lambda src, st: fake))
    assert out["full"]["branches"] == 2 and out["full"]["new"] == 2
    assert run(mass.source_cycle(store, s, "2gis", factory=lambda src, st: fake)) == {}    # всем рано
    with store.engine.begin() as c:                                                         # подошёл срок «x»
        c.execute(update(sync_state).where(sync_state.c.source_branch_id == "x")
                  .values(next_due_at=utcnow() - timedelta(minutes=1)))
    data["x"] = [_rev("2gis", "x", 2, 0)] + data["x"]
    out = run(mass.source_cycle(store, s, "2gis", factory=lambda src, st: fake))
    assert list(out) == ["new"] and out["new"]["branches"] == 1 and out["new"]["new"] == 1


def test_block_pauses_source_and_lowers_rate(store):
    s = Settings()
    s.source("2gis").rps = 1.0
    store.upsert_source_branch(SourceBranch("2gis", "x"))
    out = run(mass.source_cycle(store, s, "2gis", factory=lambda src, st: FakeSrc(st, {"x": []}, block=True)))
    assert out["full"]["blocked"] == {"2gis": "captcha"}
    assert store.kv_get("rps:2gis") == 0.7 and mass.paused(store, "2gis", utcnow()) > 3 * 3600 - 60
    assert run(mass.source_cycle(store, s, "2gis", factory=lambda src, st: FakeSrc(st, {"x": []}))) == {"paused": True}


class FakeYa(Collector):
    source = "yandex"
    capabilities = Capabilities(method="t", sorts=("union",), full_history=False)

    def __init__(self, settings, cards):
        super().__init__(settings)
        self.cards, self.queries = cards, []

    async def discover(self, area):
        self.queries.append((round(area.lat, 4), round(area.lon, 4)))
        for sid, lat, lon in self.cards:
            if abs(lat - area.lat) < 0.02 and abs(lon - area.lon) < 0.02:
                yield SourceBranch("yandex", sid, lat=lat, lon=lon)

    async def branch(self, sid):
        return SourceBranch("yandex", sid)

    async def pages(self, sid, *, sort=None, cursor=None):
        if False:
            yield


def test_discover_yandex_skips_covered_points_and_resumes(store):
    s = Settings()
    # три карточки 2ГИС: две в одном квартале, третья в другом городе
    for sid, lat, lon in (("g1", 55.7500, 37.6000), ("g2", 55.7505, 37.6005), ("g3", 56.8000, 60.6000)):
        store.upsert_source_branch(SourceBranch("2gis", sid, lat=lat, lon=lon))
    fy = FakeYa(s, [("y1", 55.7501, 37.6001), ("y2", 55.7504, 37.6006), ("y3", 56.8002, 60.6001)])
    res = run(mass.discover_yandex_points(store, s, factory=lambda src, st: fy))
    assert res["queried"] == 2 and res["found"] == 3                    # точка g2 покрыта ответом на g1
    assert run(mass.discover_yandex_points(store, s, factory=lambda src, st: fy)) == {"skipped": "свежий обход"}
    assert len(run(mass.discover_yandex_points(store, s, force=True, factory=lambda src, st: fy))
               and fy.queries) == 2                                     # новый круг: всё уже покрыто


def test_discover_2gis_all_tracks_settlements(store):
    s = Settings()

    class G(FakeSrc):
        async def settlements(self):
            return [{"alias": "moscow", "region_id": "32"}, {"alias": "kimry", "region_id": "0"}]

        async def discover(self, area, *, seen=None, patience=2):
            ids = {"moscow": ["m1", "m2"], "kimry": ["k1", "m1"]}[area.city]
            for sid in ids:
                if sid not in seen:
                    seen.add(sid)
                    yield SourceBranch("2gis", sid, city=area.city)
    res = run(mass.discover_2gis_all(store, s, factory=lambda src, st: G(st, {})))
    assert res == {"settlements": 2, "found": 3, "errors": 0}
    st = store.kv_get("discover:2gis")
    assert st["done"] == ["kimry", "moscow"] and st["finished"]
    assert sorted(store.source_branch_ids("2gis")) == ["k1", "m1", "m2"]


def test_upsert_keeps_discovery_raw_fields(store):
    store.upsert_source_branch(SourceBranch("yandex", "1", address="Москва, ул. Мира, 1", raw={"cbr_num": "1481/1/2"}))
    store.upsert_source_branch(SourceBranch("yandex", "1", declared_reviews=10))          # карточка из branch()
    with store.engine.connect() as c:
        from bank_audit.vsp.storage import source_branches
        r = c.execute(source_branches.select()).mappings().first()
    assert json.loads(r["raw_json"])["cbr_num"] == "1481/1/2" and r["address"] == "Москва, ул. Мира, 1"
    assert r["declared_reviews"] == 10


def _ya_state(oid, count):
    item = {"id": oid, "title": "СберБанк", "seoname": "sberbank", "fullAddress": "Москва, улица Мира, 1",
            "compositeAddress": {"locality": "Москва"}, "coordinates": [37.6, 55.7],
            "ratingData": {"reviewCount": count}, "reviewResults": {"reviews": [], "params": {"count": count}}}
    st = {"config": {"csrfToken": "tok", "counters": {"analytics": {"sessionId": "sid"}}},
          "stack": [{"results": {"items": [item], "requestId": "rq"}}]}
    return f'<html><script type="application/json" class="state-view">{json.dumps(st)}</script></html>'


def test_yandex_signed_sync_one_page_load_for_many_branches(store):
    """С подписью: страница организации (≈1 Мбайт) — одна на сессию; проверка отделения —
    JSON-запросы; первая страница из branch() не запрашивается повторно."""
    hits = {"org": 0, "api": 0}
    counts = {"1": 70, "2": 30}

    def handler(req: httpx.Request):
        u = str(req.url)
        if "/maps/org/" in u:
            hits["org"] += 1
            oid = u.split("/maps/org/")[1].split("/")[1]
            return httpx.Response(200, text=_ya_state(oid, counts[oid]))
        hits["api"] += 1
        q = parse_qs(urlparse(u).query)
        oid, page = q["businessId"][0], int(q["page"][0])
        n = counts[oid]
        revs = [{"reviewId": f"{oid}-{(page - 1) * 50 + i}", "text": "т", "rating": 5,
                 "updatedTime": "2026-01-01T00:00:00Z", "author": {"publicId": "x"}}
                for i in range(min(50, n - (page - 1) * 50))]
        return httpx.Response(200, json={"data": {"reviews": revs, "params": {"count": n, "totalPages": -(-n // 50)}}})
    http = SourceHttp("yandex", rps=0, timeout_s=5, max_retries=0, user_agent="t",
                      client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    y = YandexMapsCollector(Settings(), http=http, signed=True)
    r1 = run(sync_branch(y, store, "1"))                  # полный проход: полная карточка (страница)
    assert (r1.fetched, r1.completeness) == (70, "complete") and hits == {"org": 1, "api": 2}
    r2 = run(sync_branch(y, store, "2", mode="new"))      # проверка новых у другого отделения
    assert r2.new == 30 and hits == {"org": 1, "api": 3}  # сессия «1» годится для «2»: один JSON-запрос


def test_card_error_backs_off_instead_of_looping(store):
    from bank_audit.vsp.models import CollectorError

    class Broken(FakeSrc):
        async def branch(self, sid):
            raise CollectorError("2gis: заглушка вместо данных")
    s = Settings()
    store.upsert_source_branch(SourceBranch("2gis", "x"))
    out = run(mass.source_cycle(store, s, "2gis", factory=lambda src, st: Broken(st, {"x": []})))
    assert out["full"]["by_status"] == {"partial": 1}
    st = store.get_state("2gis", "x")
    assert st["status"] == "failed" and st["next_due_at"] is not None
    assert store.due("2gis", utcnow()) == {"full": [], "new": []}                  # ждёт retry_h


def test_pause_from_other_loop_stops_batch_and_no_double_backoff(store):
    s = Settings()
    s.source("2gis").rps = 1.0
    for sid in ("a", "b", "c"):
        store.upsert_source_branch(SourceBranch("2gis", sid))
    mass._pause(store, "2gis", s, "captcha")                                       # поиск поймал капчу
    mass._pause(store, "2gis", s, "captcha")                                       # повтор — без второго снижения
    assert store.kv_get("rps:2gis") == 0.7
    store.kv_set("pause:2gis", (utcnow() - timedelta(seconds=1)).isoformat())      # пауза кончилась
    fake = FakeSrc(s, {"a": [], "b": [], "c": []})
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 1                                                      # после первого отделения — снова пауза
    from bank_audit.vsp.sync import sync_many
    st = run(sync_many(fake, store, ["a", "b", "c"], stop_check=stop))
    assert len(st.results) == 1 and st.blocked == {"2gis": "пауза источника"}


def test_empty_settlements_do_not_finish_round(store):
    s = Settings()

    class Stub(FakeSrc):
        async def settlements(self):
            return []
    from bank_audit.vsp.models import CollectorError
    with pytest.raises(CollectorError):
        run(mass.discover_2gis_all(store, s, factory=lambda src, st: Stub(st, {})))
    assert not (store.kv_get("discover:2gis") or {}).get("finished")
    assert run(mass.discover_yandex_points(store, s, factory=lambda src, st: FakeYa(st, []))) == \
        {"skipped": "нет точек (карточек 2ГИС и ВСП с координатами)"}
    assert not (store.kv_get("discover:yandex") or {}).get("finished")


def test_host_limiter_is_shared_and_capped():
    from bank_audit.vsp import http as H
    a = H.host_limiter("tsrc", "h", 1.0)
    a._next = 123.0
    b = H.host_limiter("tsrc", "h", 0.5)                                           # другой клиент, другая частота
    assert a is b and b._next == 123.0 and b.interval == pytest.approx(2.0)        # очередь не сброшена
    H.set_source_rps("tsrc", 0.25)
    assert H.host_limiter("tsrc", "h", 1.0).interval == pytest.approx(4.0)         # потолок источника
