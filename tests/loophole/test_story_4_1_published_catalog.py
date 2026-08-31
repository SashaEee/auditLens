"""Контракт опубликованного каталога Story 4.1."""
from __future__ import annotations

from pathlib import Path

from bank_audit.hashing import sha256_text
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord


def test_published_catalog_excludes_research_and_pending_cases(session):
    published_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("published"),
            title="Опубликованный кейс",
            url="https://example.ru/published",
            snippet="Подтверждённый материал",
            status="published",
            is_loophole=True,
        ),
        session=session,
    )
    repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("pending"),
            title="Черновой кейс",
            url="https://example.ru/pending",
            snippet="Не публиковать",
            status="classified",
            is_loophole=True,
        ),
        session=session,
    )

    records = repo.list_published_cases(session=session)

    assert [record["record_id"] for record in records] == [published_id]


def test_catalog_ui_uses_published_endpoint_and_debounces_text_search():
    jsx = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "bank_audit"
        / "loophole"
        / "static"
        / "loophole.jsx"
    ).read_text(encoding="utf-8")

    assert "${API}/catalog" in jsx
    assert "setTimeout(() => loadRecords(), 350)" in jsx
