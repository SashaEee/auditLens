"""Контракт безопасного экспорта immutable отчёта исследования."""
from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from bank_audit.loophole import pdf_export
from bank_audit.loophole import repository as repo
from bank_audit.loophole.research_cases import ResearchCaseService
from bank_audit.loophole.web import export_research_report


def _create_report_schema(session) -> None:
    session.connection().connection.executescript("""
        CREATE TABLE IF NOT EXISTS loophole_research_report (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL, run_id TEXT NOT NULL,
            query_text TEXT NOT NULL, result_text TEXT NOT NULL,
            evidence_snapshot TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS loophole_research (
            research_id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, run_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS loophole_research_source (
            source_id INTEGER PRIMARY KEY, research_id INTEGER NOT NULL,
            url TEXT NOT NULL, title TEXT, extracted_text TEXT,
            published_at TEXT, status TEXT NOT NULL, access_status TEXT NOT NULL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS loophole_verification_snapshot (
            snapshot_id INTEGER PRIMARY KEY, research_id INTEGER NOT NULL,
            evidence_snapshot TEXT NOT NULL
        );
    """)


def _saved_report(session, *, user_id: str = "analyst") -> tuple[int, int]:
    _create_report_schema(session)
    workspace_id = repo.create_workspace(user_id, "исследование", session=session)
    report_id = ResearchCaseService(session).save_report_result(
        workspace_id=workspace_id,
        run_id="report-export",
        query="проверь комиссию",
        result="Комиссия не видна заранее.",
    )
    return workspace_id, report_id


def test_report_result_copies_only_immutable_evidence_for_its_run(session):
    _create_report_schema(session)
    workspace_id = repo.create_workspace("analyst", "исследование", session=session)
    session.execute(
        text("INSERT INTO loophole_research VALUES (1, :workspace_id, 'run-1')"),
        {"workspace_id": workspace_id},
    )
    session.execute(
        text(
            "INSERT INTO loophole_research_source "
            "VALUES (1, 1, :url, 'Правила', :extracted_text, :published_at, 'fetched', 'active', CURRENT_TIMESTAMP)"
        ),
        {
            "url": "https://bank.example/rules",
            "extracted_text": "Подтверждённый текст",
            "published_at": "2026-08-27T09:25:00+03:00",
        },
    )
    report_id = ResearchCaseService(session).save_report_result(
        workspace_id=workspace_id, run_id="run-1", query="тема", result="итог"
    )
    session.execute(text("UPDATE loophole_research_source SET extracted_text = 'изменённый текст'"))
    report = ResearchCaseService(session).get_report_result(report_id)

    assert report is not None
    assert report["run_id"] == "run-1"
    evidence = report["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["url"] == "https://bank.example/rules"
    assert evidence[0]["extracted_text"] == "Подтверждённый текст"
    assert evidence[0]["published_at"] == "2026-08-27T09:25:00+03:00"


def test_report_result_is_bound_to_current_workspace_run_and_has_no_evidence(session):
    _, report_id = _saved_report(session)
    report = ResearchCaseService(session).get_report_result(report_id)

    assert report is not None
    assert report["query"] == "проверь комиссию"
    assert report["result"] == "Комиссия не видна заранее."
    assert report["evidence"] == []


def test_report_renderer_escapes_untrusted_text_and_marks_missing_evidence():
    html = pdf_export.render_research_report_html({
        "query": "<img src=x onerror=alert(1)>",
        "result": "<script>alert(1)</script>",
        "evidence": [],
    })

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Проверенные доказательства отсутствуют." in html


def test_report_docx_contains_query_result_and_only_snapshot_evidence(session):
    _, report_id = _saved_report(session)
    report = ResearchCaseService(session).get_report_result(report_id)
    assert report is not None

    payload = pdf_export.export_research_report_docx(report)
    assert payload.startswith(b"PK")

    from docx import Document

    document = Document(BytesIO(payload))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "проверь комиссию" in text
    assert "Комиссия не видна заранее." in text
    assert "Проверенные доказательства отсутствуют." in text


def test_report_export_denies_foreign_workspace(session):
    _, report_id = _saved_report(session)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(export_research_report(report_id, "docx", user_id="intruder", session=session))

    assert exc.value.status_code == 403


def test_report_pdf_failure_is_typed_and_word_remains_available(monkeypatch, session):
    _, report_id = _saved_report(session)

    async def unavailable(_: dict) -> bytes:
        raise RuntimeError("Playwright недоступен")

    monkeypatch.setattr(pdf_export, "export_research_report_pdf", unavailable)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(export_research_report(report_id, "pdf", user_id="analyst", session=session))

    assert exc.value.status_code == 503
    assert exc.value.detail == {
        "code": "pdf_unavailable",
        "message": "PDF-экспорт недоступен. Выберите Word или повторите PDF.",
    }
    response = asyncio.run(export_research_report(report_id, "docx", user_id="analyst", session=session))
    assert response.media_type.startswith("application/vnd.openxmlformats-officedocument")
