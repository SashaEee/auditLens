"""Общий механизм сборщика ВСП на поддельном источнике (без сети)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bank_audit.vsp.collectors.base import Area, Capabilities, Collector
from bank_audit.vsp.config import Settings
from bank_audit.vsp.models import CollectorError, NotFound, Review, ReviewPage, SourceBlocked, SourceBranch
from bank_audit.vsp.storage import Store
from bank_audit.vsp.sync import completeness_of, sync_branch, sync_many

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _rev(sid, k, text=None, reply=None, days=0):
    return Review(source="fake", source_review_id=f"{sid}-{k}", source_branch_id=sid, rating=5 - k % 5,
                  review_text=text or f"отзыв {k}", review_date=T0 - timedelta(days=days or k),
                  organization_reply=reply, collection_method="test")


class Fake(Collector):
    """Выдача: список отзывов на отделение, по page_size на страницу, курсор — номер."""
    source = "fake"

    def __init__(self, data, *, full_history=True, fail_at=None, block_at=None, declared=None,
                 page_size=3):
        s = Settings()
        s.sources["fake"] = s.source("fake")
        s.sources["fake"].concurrency = 2
        super().__init__(s)
        self.data = data
        self.capabilities = Capabilities(method="test", sorts=("newest",), full_history=full_history)
        self.fail_at, self.block_at, self.declared, self.page_size = fail_at, block_at, declared, page_size
        self.calls = []

    async def discover(self, area: Area):
        for sid in self.data:
            yield SourceBranch("fake", sid, name=f"ВСП {sid}", city=area.city)

    async def branch(self, sid):
        if sid not in self.data:
            raise NotFound(sid)
        return SourceBranch("fake", sid, name=f"ВСП {sid}", declared_reviews=self.declared)

    async def pages(self, sid, *, sort=None, cursor=None):
        items = self.data[sid]
        start = int(cursor or 0)
        n = start // self.page_size
        for i in range(start, len(items), self.page_size):
            self.calls.append((sid, i))
            if self.block_at is not None and n == self.block_at:
                raise SourceBlocked("fake", "captcha")
            if self.fail_at is not None and n == self.fail_at:
                self.fail_at = None
                raise CollectorError("сеть")
            chunk = items[i:i + self.page_size]
            nxt = i + self.page_size
            yield ReviewPage(chunk, page_no=n, next_cursor=str(nxt) if nxt < len(items) else None)
            n += 1


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'v.sqlite'}")


def run(coro):
    return asyncio.run(coro)


def test_full_pass_new_updated_and_gone(store):
    data = {"a": [_rev("a", k) for k in range(1, 8)]}
    c = Fake(data, declared=7)
    r = run(sync_branch(c, store, "a"))
    assert (r.status, r.completeness, r.new, r.fetched, r.pages) == ("done", "complete", 7, 7, 3)
    st = store.get_state("fake", "a")
    assert st["stored_total"] == 7 and st["cursor"] is None and st["last_full_at"] is not None
    # второй проход: отзыв 3 отредактирован (появился ответ банка), отзыв 7 исчез
    data["a"] = [_rev("a", k, reply="Ответ банка" if k == 3 else None) for k in range(1, 7)]
    r2 = run(sync_branch(c, store, "a"))
    assert (r2.new, r2.updated, r2.gone) == (0, 1, 1)
    from sqlalchemy import select
    from bank_audit.vsp.storage import review_versions, reviews
    with store.engine.connect() as cn:
        gone = cn.execute(select(reviews.c.review_id).where(reviews.c.gone_at.is_not(None))).scalars().all()
        ver = cn.execute(select(review_versions.c.review_id)).scalars().all()
    assert gone == ["fake:a-7"] and ver == ["fake:a-3"]
    assert store.get_state("fake", "a")["stored_total"] == 6


def test_resume_after_failure_keeps_progress_and_gone_logic(store):
    data = {"a": [_rev("a", k) for k in range(1, 10)]}
    run(sync_branch(Fake(data), store, "a"))                 # всё известно
    c = Fake(data, fail_at=2)
    r = run(sync_branch(c, store, "a"))
    assert r.status == "failed" and r.completeness == "partial"
    st = store.get_state("fake", "a")
    assert st["status"] == "failed" and st["cursor"] == "6"
    # статус failed — следующий полный проход начинается заново (cursor только для running)
    store.put_state("fake", "a", status="running")           # как после падения процесса
    c.calls.clear()
    r2 = run(sync_branch(c, store, "a"))
    assert c.calls == [("a", 6)] and r2.completeness == "complete" and r2.fetched == 9
    assert r2.gone == 0                                      # продолженный проход — ничего не потеряно


def test_window_source_marks_gone_only_inside_window(store):
    old = [_rev("a", k, days=400 + k) for k in range(1, 3)]   # старые, вне окна выдачи
    fresh = [_rev("a", k) for k in range(3, 6)]
    c = Fake({"a": fresh + old}, full_history=False)
    run(sync_branch(c, store, "a"))
    # источник с потолком: старые отзывы больше не отдаёт, свежий из середины окна исчез
    c.data["a"] = [fresh[0], fresh[2]]
    r = run(sync_branch(c, store, "a"))
    assert r.completeness == "window" and r.gone == 1          # старые не помечены пропавшими
    # самый старый из окна выпал за край — не «пропал»: мог просто уйти за потолок
    c.data["a"] = [fresh[0]]
    assert run(sync_branch(c, store, "a")).gone == 0


def test_new_mode_stops_at_known_page(store):
    data = {"a": [_rev("a", k) for k in range(10, 20)]}
    run(sync_branch(Fake(data), store, "a"))
    data["a"] = [_rev("a", k) for k in range(1, 3)] + data["a"]  # два новых сверху
    c = Fake(data)
    r = run(sync_branch(c, store, "a", mode="new"))
    assert r.new == 2 and r.stop == "caught_up" and len(c.calls) == 2


def test_new_mode_does_not_stop_on_recently_edited_known_page(store):
    """Сверху три старых отзыва, отредактированных после прошлой синхронизации без смены
    текста (2ГИС сортирует по дате правки), под ними — новый. Правило «страница целиком
    знакома» остановилось бы на первой странице и потеряло новый отзыв; отсечка по времени — нет."""
    from bank_audit.vsp.models import utcnow
    old = [_rev("a", k) for k in range(10, 20)]
    run(sync_branch(Fake({"a": old}), store, "a"))
    now = utcnow()
    edited = [Review(**{**r.__dict__, "edited_at": now}) for r in old[:3]]       # правка без смены содержимого
    fresh = Review(source="fake", source_review_id="a-new", source_branch_id="a", rating=1, review_text="новый",
                   review_date=now - timedelta(hours=1), edited_at=now - timedelta(hours=1), collection_method="test")
    c = Fake({"a": edited + [fresh] + old[3:]})
    r = run(sync_branch(c, store, "a", mode="new"))
    assert r.new == 1 and r.stop == "caught_up" and len(c.calls) == 3


def test_new_mode_without_previous_success_goes_to_end(store):
    data = {"a": [_rev("a", k) for k in range(1, 8)]}
    c = Fake(data)
    r = run(sync_branch(c, store, "a", mode="new"))
    assert r.new == 7 and r.stop == "end"


def test_duplicates_across_pages_do_not_inflate_completeness(store):
    """Выдача по релевантности повторила отзыв на соседней странице: строк 5 = заявлено 5,
    но разных 4 — это below_declared, а не complete."""
    revs = [_rev("a", k) for k in range(1, 5)]
    c = Fake({"a": revs[:3] + [revs[2]] + revs[3:]}, declared=5)
    r = run(sync_branch(c, store, "a"))
    assert (r.fetched, r.dupes, r.completeness) == (4, 1, "below_declared")


def test_block_stops_source_and_records(store):
    data = {s: [_rev(s, k) for k in range(1, 8)] for s in "abcd"}
    c = Fake(data, block_at=1)
    c.cfg.concurrency = 1
    st = run(sync_many(c, store, list("abcd")))
    assert st.blocked == {"fake": "captcha"}
    assert [r.status for r in st.results] == ["blocked"]       # остальные не запускались
    assert store.get_state("fake", "a")["completeness"] == "blocked"


def test_not_found_branch(store):
    r = run(sync_branch(Fake({}), store, "zzz"))
    assert r.status == "not_found"


def test_completeness_rules():
    assert completeness_of("end", full_history=True, declared=100, fetched=99) == "complete"
    assert completeness_of("end", full_history=True, declared=100, fetched=80) == "below_declared"
    assert completeness_of("end", full_history=False, declared=300, fetched=50) == "window"
    assert completeness_of("end", full_history=False, declared=134, fetched=134) == "complete"
    assert completeness_of("end", full_history=False, declared=None, fetched=50) == "window"
    assert completeness_of("max_pages", full_history=True, declared=None, fetched=10) == "partial"


def test_report_single_author_unknown_when_source_lacks_field(store):
    from bank_audit.vsp.report import completeness_report
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    a = Review(source="ya", source_review_id="1", source_branch_id="b", rating=5, review_text="т",
               review_date=now, collection_method="t", raw={})
    g = Review(source="gis", source_review_id="1", source_branch_id="b", rating=5, review_text="т",
               review_date=now, collection_method="t", raw={"author_reviews_count": 1})
    store.save_reviews([a, g], seen_at=now)
    prof = completeness_report(store)["profile"]
    assert prof["ya"]["five_2026_single_author"] is None and prof["gis"]["five_2026_single_author"] == 1
