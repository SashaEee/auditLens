"""Контракт экспорта опубликованного каталога Story 4.2."""
from __future__ import annotations

import asyncio

import pytest

from bank_audit.hashing import sha256_text
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.web import ReportFilterV1, export_csv_filtered


def test_filtered_export_uses_published_catalog_only(session):
    repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("export-published"),
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
            sha256=sha256_text("export-pending"),
            title="Черновой кейс",
            url="https://example.ru/pending",
            snippet="Не публиковать",
            status="classified",
            is_loophole=True,
        ),
        session=session,
    )

    response = export_csv_filtered(ReportFilterV1(), user_id="analyst", session=session)
    payload = response.body.decode("utf-8")

    assert "Опубликованный кейс" in payload
    assert "Черновой кейс" not in payload


def test_xlsx_export_rejects_more_than_ten_thousand_without_partial_file(monkeypatch, session):
    from fastapi import HTTPException

    from bank_audit.loophole.web import export_xlsx_filtered

    monkeypatch.setattr(repo, "list_records", lambda **_: [{}] * 10_001)

    with pytest.raises(HTTPException) as exc:
        export_xlsx_filtered(ReportFilterV1(), user_id="analyst", session=session)

    assert exc.value.status_code == 409
    assert "10001" in exc.value.detail


def test_pdf_export_returns_structured_error_when_renderer_is_unavailable(monkeypatch, session):
    from fastapi import HTTPException

    from bank_audit.loophole import pdf_export
    from bank_audit.loophole.web import export_pdf_filtered

    async def unavailable(*args, **kwargs):
        raise RuntimeError("Playwright недоступен")

    monkeypatch.setattr(pdf_export, "export_pdf", unavailable)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(export_pdf_filtered(ReportFilterV1(), user_id="analyst", session=session))

    assert exc.value.status_code == 503
    assert exc.value.detail == {"code": "pdf_unavailable", "message": "PDF-экспорт недоступен"}
