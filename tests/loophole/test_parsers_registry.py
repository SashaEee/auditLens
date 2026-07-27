"""Тест registry: каталог с агрегатами и поиск конфликтов source_keys."""
from __future__ import annotations

import pytest

from bank_audit.loophole import repository as repo
from bank_audit.loophole.parsers import registry
from bank_audit.loophole.models import LoopholeRecord


@pytest.fixture
def parser_id(session) -> int:
    wid = repo.create_workspace("u1", "ws1", session=session)
    return repo.save_parser(
        wid, "p1", "/tmp/p1.py",
        config={"query": "q", "targets": ["https://a.ru/x"]},
        created_by="u1", source_keys=["a.ru/x"], session=session,
    )


def test_list_catalog_aggregates(session, parser_id):
    repo.insert_record(
        LoopholeRecord(sha256="s1", url="https://a.ru/1", raw_text="r",
                       parser_id=parser_id), session=session,
    )
    run_id = repo.create_run(parser_id, "manual", session=session)
    repo.finish_run(run_id, "success", items_found=1, items_new=1, session=session)

    rows = registry.list_catalog(session=session)
    assert len(rows) == 1
    row = rows[0]
    assert row["parser_id"] == parser_id
    assert row["records_count"] == 1
    assert row["last_run"]["status"] == "success"
    assert row["last_run"]["items_new"] == 1
    assert row["is_running"] is False
    assert row["needs_attention"] is False
    assert row["targets"] == ["https://a.ru/x"]


def test_list_catalog_needs_attention(session, parser_id):
    repo.set_heal_attempts(parser_id, 3, session=session)
    repo.disable_auto(parser_id, session=session)
    row = registry.list_catalog(session=session)[0]
    assert row["needs_attention"] is True


def test_delete_parser_removes_directory(session, parser_id, tmp_path):
    parser_dir = tmp_path / f"parser_{parser_id}_p1"
    parser_dir.mkdir()
    (parser_dir / "parser.py").write_text("code")
    (parser_dir / "venv").mkdir()
    repo.update_parser_code_path(parser_id, str(parser_dir / "parser.py"), session=session)
    assert registry.delete_parser(parser_id, session=session) is True
    assert not parser_dir.exists()
    assert repo.get_parser(parser_id, session=session) is None


def test_find_conflicts_full_and_partial(session, parser_id):
    # Полное пересечение.
    full = registry.find_conflicts(["a.ru/x"], session=session)
    assert full == [{"parser_id": parser_id, "name": "p1", "overlap": ["a.ru/x"]}]
    # Частичное.
    part = registry.find_conflicts(["a.ru/x", "b.ru/y"], session=session)
    assert part[0]["overlap"] == ["a.ru/x"]
    # Без пересечения.
    assert registry.find_conflicts(["c.ru/z"], session=session) == []
    assert registry.find_conflicts([], session=session) == []
