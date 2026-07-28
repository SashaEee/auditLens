"""Тест repository: CRUD + дедуп по sha256.

На in-memory SQLite с адаптированной схемой (BIGSERIAL→INTEGER AUTOINCREMENT,
JSONB→TEXT, TEXT[]→TEXT, TIMESTAMPTZ→TEXT). Это проверяет SQL-логику repository
без реальной Greenplum-БД. ILIKE эмулируем через LIKE (SQLite регистрочувствителен —
тестовые данные в нижнем регистре).
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from sqlalchemy import text

from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.hashing import sha256_text


# SQLite не знает ILIKE — регистрируем функцию-заглушку (lower-case сравнение).
def _ilike_pattern(pattern: str) -> str:
    return pattern.lower().replace("%", "%")


def _make_record(**kw) -> LoopholeRecord:
    base = dict(
        sha256=sha256_text("test"),
        title="лазейка в кредитном договоре",
        url="https://example.ru/doc",
        snippet="скрытая комиссия",
        domain="example.ru",
        trust_score=0.8,
        bank_slug="sberbank",
        keyword="лазейка",
        raw_text="текст договора со скрытой комиссией",
    )
    base.update(kw)
    return LoopholeRecord(**base)


def test_insert_record_returns_id(session):
    rec = _make_record()
    rid = repo.insert_record(rec, session=session)
    assert rid is not None and rid > 0


def test_insert_record_dedup_by_sha256(session):
    rec = _make_record()
    rid1 = repo.insert_record(rec, session=session)
    rid2 = repo.insert_record(rec, session=session)
    assert rid1 == rid2, "дедуп по sha256 не сработал"
    rows = session.execute(text("SELECT count(*) FROM loophole_record")).scalar()
    assert rows == 1


def test_exists_sha256(session):
    rec = _make_record()
    assert not repo.exists_sha256(rec.sha256, session=session)
    repo.insert_record(rec, session=session)
    assert repo.exists_sha256(rec.sha256, session=session)


def test_update_verdict(session):
    rid = repo.insert_record(_make_record(), session=session)
    repo.update_verdict(
        rid, is_loophole=True, confidence=0.92,
        reason="скрытая комиссия", model="test-model", session=session,
    )
    row = repo.get_record(rid, session=session)
    assert row["is_loophole"] == 1  # SQLite хранит bool как int
    assert row["verdict_confidence"] == 0.92
    assert row["status"] == "classified"


def test_search_relevant_by_query(session):
    repo.insert_record(_make_record(title="лазейка в договоре"), session=session)
    repo.insert_record(
        _make_record(sha256=sha256_text("other"), title="безопасный продукт"),
        session=session,
    )
    # Обе записи ещё не классифицированы → only_loophole=True даст 0.
    results = repo.search_relevant("лазейка", only_loophole=False, session=session)
    assert len(results) == 1
    assert "лазейка" in results[0]["title"]


def test_search_relevant_bank_filter(session):
    repo.insert_record(_make_record(bank_slug="sberbank"), session=session)
    repo.insert_record(
        _make_record(sha256=sha256_text("vtb"), bank_slug="vtb"),
        session=session,
    )
    results = repo.search_relevant(
        "", bank_slugs=["sberbank"], only_loophole=False, session=session
    )
    assert len(results) == 1
    assert results[0]["bank_slug"] == "sberbank"


def test_keyword_crud_and_dedup(session):
    kid1 = repo.add_keyword("лазейка", category="seed", source="cbr", session=session)
    kid2 = repo.add_keyword("лазейка", category="seed", source="cbr", session=session)
    assert kid1 == kid2, "дедуп ключевого слова не сработал"
    kws = repo.list_keywords(session=session)
    assert len(kws) == 1
    repo.set_keyword_active(kid1, False, session=session)
    active = repo.list_keywords(only_active=True, session=session)
    assert len(active) == 0


def test_workspace_isolation(session):
    wid_a = repo.create_workspace("user-a", "ws-a", session=session)
    wid_b = repo.create_workspace("user-b", "ws-b", session=session)
    ws_a = repo.list_workspaces("user-a", session=session)
    ws_b = repo.list_workspaces("user-b", session=session)
    assert len(ws_a) == 1 and ws_a[0]["workspace_id"] == wid_a
    assert len(ws_b) == 1 and ws_b[0]["workspace_id"] == wid_b


def test_chat_history_order(session):
    wid = repo.create_workspace("u", session=session)
    repo.add_chat_message(wid, "user", "вопрос", session=session)
    repo.add_chat_message(wid, "assistant", "ответ", session=session)
    hist = repo.list_chat_history(wid, session=session)
    assert len(hist) == 2
    assert hist[0]["role"] == "user"
    assert hist[1]["role"] == "assistant"


def test_log_action_and_list(session):
    repo.log_action("u1", "search", detail={"q": "лазейка"}, ip="127.0.0.1", session=session)
    repo.log_action("u1", "export", detail={"fmt": "pdf"}, session=session)
    actions = repo.list_actions("u1", session=session)
    assert len(actions) == 2
    # DESC по created_at — последний первым.
    assert actions[0]["action"] == "export"
    assert actions[0]["user_id"] == "u1"


# ── KB: record_id (ручная маркировка) ───────────────────────────────────────
def test_save_kb_example_with_record_id_without_embedding(session):
    """Без embedding вставка работает на SQLite (нет ::vector каста)."""
    ex_id = repo.save_kb_example(
        "Скрытая комиссия", "Банк не раскрывает ПСК",
        category="manual", record_id=777, session=session,
    )
    assert ex_id is not None
    row = repo.get_kb_example_by_record(777, session=session)
    assert row is not None
    assert row["title"] == "Скрытая комиссия"
    assert row["category"] == "manual"
    assert row["record_id"] == 777


def test_save_kb_example_with_embedding_uses_cast_bind():
    """Регрессия: :emb::vector ломает bind SQLAlchemy (имя 'em') → SyntaxError в PG.

    Ветка с embedding на SQLite не исполняется — проверяем текст SQL и bindparams
    через mock-сессию (как в production-пути mark_verdict → add_example).
    """
    from unittest.mock import MagicMock

    mock_s = MagicMock()
    mock_s.execute.return_value.scalar_one.return_value = 99
    repo.save_kb_example(
        "t", "d", category="manual",
        embedding=[0.1, 0.2], record_id=1, session=mock_s,
    )
    stmt = mock_s.execute.call_args[0][0]
    params = mock_s.execute.call_args[0][1]
    sql = str(stmt)
    assert ":emb::vector" not in sql
    assert "CAST(:emb AS vector)" in sql
    assert "emb" in stmt._bindparams
    assert "emb" in params


def test_save_kb_example_without_record_id(session):
    """Обратная совместимость: record_id опционален (None по умолчанию)."""
    ex_id = repo.save_kb_example("t", "d", category="general", session=session)
    assert ex_id is not None
    rid = session.execute(
        text("SELECT record_id FROM loophole_kb_example WHERE example_id = :id"),
        {"id": ex_id},
    ).scalar()
    assert rid is None


def test_get_kb_example_by_record_missing(session):
    assert repo.get_kb_example_by_record(424242, session=session) is None


def test_delete_kb_example_by_record(session):
    repo.save_kb_example("t1", "d1", category="manual", record_id=55, session=session)
    deleted = repo.delete_kb_example_by_record(55, session=session)
    assert deleted == 1
    assert repo.get_kb_example_by_record(55, session=session) is None


def test_delete_kb_example_by_record_missing(session):
    assert repo.delete_kb_example_by_record(31337, session=session) == 0


# ── Content fields + backfill ────────────────────────────────────────────────
def test_insert_record_with_content_fields(session):
    rec = LoopholeRecord(
        sha256=sha256_text("c1"), title="t", url="https://x.ru/a",
        snippet="s", raw_text="полный текст", content_status="full",
        raw_text_len=12, raw_text_truncated=False,
    )
    rid = repo.insert_record(rec, session=session)
    row = repo.get_record(rid, session=session)
    assert row["content_status"] == "full"
    assert row["raw_text_len"] == 12
    assert row["raw_text_truncated"] in (False, 0)


def test_update_content(session):
    rec = LoopholeRecord(sha256=sha256_text("c2"), title="t",
                         url="https://x.ru/b", snippet="s", raw_text="сниппет")
    rid = repo.insert_record(rec, session=session)
    repo.update_content(
        rid, raw_text="ДОГРУЖЕНО", content_status="full",
        raw_text_len=9, truncated=False, session=session,
    )
    row = repo.get_record(rid, session=session)
    assert row["raw_text"] == "ДОГРУЖЕНО"
    assert row["content_status"] == "full"
    assert row["raw_text_len"] == 9


def test_update_content_none_keeps_existing_text(session):
    """raw_text=None не затирает сохранённый текст (COALESCE) — случай fetch_failed."""
    rec = LoopholeRecord(sha256=sha256_text("c3"), title="t",
                         url="https://x.ru/c", snippet="s", raw_text="важный сниппет")
    rid = repo.insert_record(rec, session=session)
    repo.update_content(
        rid, raw_text=None, content_status="fetch_failed",
        raw_text_len=0, truncated=False, session=session,
    )
    row = repo.get_record(rid, session=session)
    assert row["raw_text"] == "важный сниппет"
    assert row["content_status"] == "fetch_failed"


def test_list_records_needing_content_queue(session):
    """В очередь backfill попадают legacy/NULL/fetch_failed/empty, но не full."""
    repo.insert_record(LoopholeRecord(
        sha256=sha256_text("q1"), url="https://x.ru/1", snippet="s",
        raw_text="полный", content_status="full"), session=session)
    repo.insert_record(LoopholeRecord(
        sha256=sha256_text("q2"), url="https://x.ru/2", snippet="s",
        raw_text="сниппет"), session=session)  # content_status NULL
    repo.insert_record(LoopholeRecord(
        sha256=sha256_text("q3"), url="https://x.ru/3", snippet="s",
        content_status="fetch_failed"), session=session)
    repo.insert_record(LoopholeRecord(
        sha256=sha256_text("q4"), snippet="без url",
        content_status="legacy"), session=session)  # url NULL — не в очереди
    targets = repo.list_records_needing_content(limit=10, session=session)
    urls = {t["url"] for t in targets}
    assert urls == {"https://x.ru/2", "https://x.ru/3"}
    assert repo.count_records_needing_content(session=session) == 2


def test_list_records_returns_content_metadata(session):
    repo.insert_record(LoopholeRecord(
        sha256=sha256_text("m1"), title="t", url="https://x.ru/m",
        snippet="s", raw_text="полный текст", content_status="truncated",
        raw_text_len=5000), session=session)
    rows = repo.list_records(session=session)
    assert rows[0]["content_status"] == "truncated"
    assert rows[0]["raw_text_len"] == 5000
    assert "raw_text" not in rows[0], "список не должен тащить полный текст"


def test_list_records_include_content(session):
    repo.insert_record(LoopholeRecord(
        sha256=sha256_text("m2"), title="t", url="https://x.ru/e",
        snippet="s", raw_text="ПОЛНЫЙ ДЛЯ ЭКСПОРТА", content_status="full",
        raw_text_len=18, raw_text_truncated=True), session=session)
    rows = repo.list_records(include_content=True, session=session)
    assert rows[0]["raw_text"] == "ПОЛНЫЙ ДЛЯ ЭКСПОРТА"
    assert rows[0]["raw_text_truncated"] in (True, 1)
