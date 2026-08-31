"""Server-side граница передачи исследовательского кейса в очередь ЦК КС."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from bank_audit.loophole import repository as repo
from bank_audit.loophole.research_cases import ResearchCaseService
from bank_audit.loophole.web import SubmitResearchCandidateRequest, submit_research_candidate


def _create_submission_schema(session) -> None:
    session.connection().connection.executescript("""
        CREATE TABLE IF NOT EXISTS loophole_research (
            research_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            query_text TEXT NOT NULL,
            search_params TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS loophole_research_source (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            extracted_text TEXT,
            published_at TEXT,
            status TEXT NOT NULL,
            limitation_message TEXT,
            access_status TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS loophole_research_candidate (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            evidence TEXT NOT NULL,
            category TEXT,
            description TEXT NOT NULL,
            severity TEXT NOT NULL,
            is_loophole INTEGER NOT NULL,
            model_is_loophole INTEGER,
            model_confidence REAL,
            model_reason TEXT,
            model_name TEXT,
            manual_verdict INTEGER,
            ccks_decision TEXT,
            draft_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS loophole_verification_snapshot (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            research_id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            draft_version INTEGER NOT NULL,
            case_snapshot TEXT NOT NULL,
            evidence_snapshot TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL
        );
    """)


def _seed_candidate(session, workspace_id: int) -> tuple[int, int]:
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=workspace_id,
        run_id="research-route",
        query="проверь комиссию",
        search_params={"max_results": 1},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/terms",
        title="Условия",
        extracted_text="Комиссия скрыта в условии.",
    )
    candidate_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Скрытая комиссия",
        evidence="Комиссия скрыта в условии.",
        category="комиссия",
        description="Комиссия не видна заранее.",
        severity="high",
        is_loophole=True,
    )
    return candidate_id, source_id


def test_submit_route_requires_workspace_owner_and_returns_waiting_status(session):
    _create_submission_schema(session)
    workspace_id = repo.create_workspace("analyst", "исследование", session=session)
    candidate_id, source_id = _seed_candidate(session, workspace_id)
    body = SubmitResearchCandidateRequest(evidence_source_ids=[source_id], run_id="agent-run-7")

    response = submit_research_candidate(
        candidate_id,
        body,
        user_id="analyst",
        session=session,
    )

    assert response["status_label"] == "Ожидает решения ЦК КС"
    with pytest.raises(HTTPException, match="чужому workspace"):
        submit_research_candidate(
            candidate_id,
            body,
            user_id="intruder",
            session=session,
        )
