"""Тест repository: расширенные parser-функции, schedule, heal-счётчики."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from bank_audit.loophole import repository as repo


@pytest.fixture
def parser_id(session) -> int:
    wid = repo.create_workspace("u", "ws", session=session)
    return repo.save_parser(
        wid, "p1", "", config={"query": "q", "targets": ["https://a.ru"]},
        created_by="user-1", source_keys=["a.ru/"], session=session,
    )


def test_save_parser_new_fields(session, parser_id):
    row = repo.get_parser(parser_id, session=session)
    assert row["created_by"] == "user-1"
    assert json.loads(row["source_keys"]) == ["a.ru/"]
    assert row["heal_attempts"] == 0


def test_update_parser_code_path(session, parser_id):
    repo.update_parser_code_path(parser_id, "/tmp/x.py", session=session)
    assert repo.get_parser(parser_id, session=session)["code_path"] == "/tmp/x.py"


def test_update_parser_schedule(session, parser_id):
    nxt = datetime(2026, 7, 27, 5, 0, tzinfo=UTC)
    repo.update_parser_schedule(
        parser_id, cron_expr="0 5 * * *", auto_enabled=True,
        next_run_at=nxt, last_edited_by="user-2", name="new-name", session=session,
    )
    row = repo.get_parser(parser_id, session=session)
    assert row["cron_expr"] == "0 5 * * *"
    assert row["auto_enabled"] in (True, 1)
    assert row["last_edited_by"] == "user-2"
    assert row["name"] == "new-name"
    assert row["next_run_at"] == nxt.isoformat()


def test_update_parser_next_run(session, parser_id):
    # Сначала ставим не-NULL, чтобы доказать, что SET работает.
    repo.update_parser_next_run(
        parser_id, datetime(2026, 7, 27, 5, 0, tzinfo=UTC), session=session,
    )
    assert repo.get_parser(parser_id, session=session)["next_run_at"] is not None
    # Затем сбрасываем в NULL.
    repo.update_parser_next_run(parser_id, None, session=session)
    assert repo.get_parser(parser_id, session=session)["next_run_at"] is None


def test_heal_attempts_and_disable_auto(session, parser_id):
    repo.set_heal_attempts(parser_id, 3, session=session)
    repo.disable_auto(parser_id, session=session)
    row = repo.get_parser(parser_id, session=session)
    assert row["heal_attempts"] == 3
    assert row["auto_enabled"] in (False, 0)


def test_list_all_and_source_keys(session, parser_id):
    all_rows = repo.list_all_parsers(session=session)
    assert [r["parser_id"] for r in all_rows] == [parser_id]
    sk = repo.list_parsers_with_source_keys(session=session)
    assert sk[0]["parser_id"] == parser_id


def test_list_auto_parsers(session, parser_id):
    assert repo.list_auto_parsers(session=session) == []
    repo.update_parser_status(parser_id, "ready", session=session)
    repo.update_parser_schedule(
        parser_id, cron_expr="* * * * *", auto_enabled=True,
        next_run_at=datetime.now(UTC), last_edited_by="u", session=session,
    )
    rows = repo.list_auto_parsers(session=session)
    assert [r["parser_id"] for r in rows] == [parser_id]


def test_list_auto_parsers_excludes_failed_validation(session, parser_id):
    """Cron не должен получать parser, не прошедший validation."""
    repo.update_parser_schedule(
        parser_id, cron_expr="* * * * *", auto_enabled=True,
        next_run_at=datetime.now(UTC), last_edited_by="u", session=session,
    )
    repo.update_parser_status(parser_id, "validation_failed", session=session)

    assert repo.list_auto_parsers(session=session) == []


# ── parser_run ───────────────────────────────────────────────────────────────
def test_run_lifecycle(session, parser_id):
    run_id = repo.create_run(parser_id, "manual", session=session)
    assert run_id > 0
    run = repo.get_run(run_id, session=session)
    assert run["status"] == "running"
    assert run["run_trigger"] == "manual"

    repo.finish_run(
        run_id, "success", items_found=5, items_new=3, items_dup=2,
        error_text=None, log_tail="line1\nline2", session=session,
    )
    run = repo.get_run(run_id, session=session)
    assert run["status"] == "success"
    assert run["items_found"] == 5
    assert run["items_new"] == 3
    assert run["items_dup"] == 2
    assert run["log_tail"] == "line1\nline2"
    assert run["finished_at"] is not None


def test_list_runs_and_last_run(session, parser_id):
    repo.create_run(parser_id, "cron", session=session)
    r2 = repo.create_run(parser_id, "manual", session=session)
    repo.finish_run(r2, "empty", session=session)
    runs = repo.list_runs(parser_id, session=session)
    assert len(runs) == 2
    last = repo.last_run(parser_id, session=session)
    assert last["run_id"] == r2
    assert last["status"] == "empty"
    assert repo.last_run(99999, session=session) is None


def test_reap_stale_runs(session, parser_id):
    repo.create_run(parser_id, "cron", session=session)
    n = repo.reap_stale_runs(session=session)
    assert n == 1
    last = repo.last_run(parser_id, session=session)
    assert last["status"] == "error"
    assert last["error_text"] == "server restart"
    # Повторный вызов — ничего не трогает.
    assert repo.reap_stale_runs(session=session) == 0


# ── дедуп записей: URL и полный текст ────────────────────────────────────────
def test_exists_url(session):
    from bank_audit.loophole.models import LoopholeRecord
    repo.insert_record(
        LoopholeRecord(sha256="s1", url="https://a.ru/page?x=1", raw_text="r1"),
        session=session,
    )
    assert repo.exists_url("https://a.ru/page?x=1", session=session) is True
    assert repo.exists_url("https://a.ru/other", session=session) is False


def test_exists_text_sha256(session):
    from bank_audit.loophole.models import LoopholeRecord
    repo.insert_record(
        LoopholeRecord(sha256="s2", url="https://b.ru", raw_text="r2",
                       text_sha256="abc123"),
        session=session,
    )
    assert repo.exists_text_sha256("abc123", session=session) is True
    assert repo.exists_text_sha256("nope", session=session) is False


def test_insert_record_stores_parser_and_text_sha(session):
    from bank_audit.loophole.models import LoopholeRecord
    rid = repo.insert_record(
        LoopholeRecord(sha256="s3", url="https://c.ru", raw_text="r3",
                       parser_id=7, text_sha256="t9"),
        session=session,
    )
    row = session.execute(
        text("SELECT parser_id, text_sha256 FROM loophole_record WHERE record_id = :id"),
        {"id": rid},
    ).mappings().one()
    assert row["parser_id"] == 7
    assert row["text_sha256"] == "t9"


def test_count_records_by_parser(session, parser_id):
    from bank_audit.loophole.models import LoopholeRecord
    assert repo.count_records_by_parser(parser_id, session=session) == 0
    repo.insert_record(
        LoopholeRecord(sha256="cx", url="https://cnt.ru/1", raw_text="r",
                       parser_id=parser_id),
        session=session,
    )
    assert repo.count_records_by_parser(parser_id, session=session) == 1
