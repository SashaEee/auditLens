"""Контракт явного переноса источников исследования в общую базу."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from bank_audit.loophole import repository as repo
from bank_audit.loophole.research_cases import ResearchCaseService
from tests.loophole.test_story_2_2_research_cases import _create_research_schema


def _create_import_schema(session) -> None:
    _create_research_schema(session)
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS loophole_preliminary_import (
            import_id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            record_id INTEGER NOT NULL,
            imported_by TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id)
        )
    """))


def _research_with_candidate(session, *, workspace_id: int = 1, url: str = "https://bank.example/rules"):
    _create_import_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=workspace_id,
        run_id="run-preliminary",
        query="проверь комиссии",
        search_params={"max_results": 3},
    )
    source_id = service.record_source(
        research_id,
        url=url,
        title="Условия продукта",
        extracted_text="Комиссия указана только в примечании.",
        published_at="2026-08-27T09:25:00+03:00",
    )
    candidate_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Скрытая комиссия",
        evidence="Комиссия указана только в примечании.",
        category="комиссии",
        description="Условие не вынесено в основное предложение.",
        severity="medium",
        is_loophole=True,
    )
    session.execute(text("""
        UPDATE loophole_research_candidate
        SET model_is_loophole = 1, model_confidence = 0.82, model_reason = 'похоже на лазейку'
        WHERE candidate_id = :candidate_id
    """), {"candidate_id": candidate_id})
    return service, research_id, source_id


def test_imports_only_fetched_new_suspected_sources_idempotently(session):
    service, research_id, source_id = _research_with_candidate(session)

    first = service.import_preliminary_sources(research_id, imported_by="analyst")
    second = service.import_preliminary_sources(research_id, imported_by="analyst")

    assert first == {"imported": 1, "skipped": 0, "record_ids": first["record_ids"]}
    assert second == {"imported": 0, "skipped": 1, "record_ids": []}
    record = repo.get_record(first["record_ids"][0], session=session)
    assert record["status"] == "preliminary"
    assert record["is_loophole"] is True
    assert record["verdict_confidence"] == pytest.approx(0.82)
    assert str(record["published_at"]).startswith("2026-08-27 09:25:00")
    provenance = session.execute(text("""
        SELECT research_id, source_id, workspace_id FROM loophole_preliminary_import
        WHERE record_id = :record_id
    """), {"record_id": record["record_id"]}).mappings().one()
    assert dict(provenance) == {"research_id": research_id, "source_id": source_id, "workspace_id": 1}


def test_import_skips_existing_url_and_unavailable_or_non_suspicious_sources(session):
    service, research_id, _ = _research_with_candidate(session)
    existing_id = repo.insert_record(
        __import__("bank_audit.loophole.models", fromlist=["LoopholeRecord"]).LoopholeRecord(
            sha256="existing", title="Старый", url="https://bank.example/rules", status="published",
            is_loophole=True,
        ),
        session=session,
    )
    unavailable = service.record_source_failure(
        research_id, url="https://bank.example/unavailable", limitation_message="HTTP 503"
    )
    assert unavailable

    result = service.import_preliminary_sources(research_id, imported_by="analyst")

    assert result == {"imported": 0, "skipped": 1, "record_ids": []}
    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 1
    assert repo.get_record(existing_id, session=session)["status"] == "published"


def test_catalog_filter_requires_positive_ccks_decision_for_verified_records(session):
    service, research_id, source_id = _research_with_candidate(session)
    service.import_preliminary_sources(research_id, imported_by="analyst")
    candidate_id = session.execute(
        text("SELECT candidate_id FROM loophole_research_candidate WHERE source_id = :source_id"),
        {"source_id": source_id},
    ).scalar_one()
    snapshot = service.submit_for_verification(
        candidate_id,
        evidence_source_ids=[source_id],
        submitted_by="analyst",
        correlation_run_id="verified-import",
    )
    assert snapshot is not None
    service.decide_snapshot(
        snapshot["snapshot_id"],
        decision="vulnerability",
        comment="Подтверждено ЦК КС",
        decided_by="expert",
        run_id="verified-import",
    )

    waiting = repo.list_catalog_cases(verification_status="pending", session=session)
    verified = repo.list_catalog_cases(verification_status="verified", session=session)

    assert waiting == []
    assert [item["status"] for item in verified] == ["preliminary"]
    assert verified[0]["provenance"]["research_id"] == research_id


def test_import_route_requires_research_workspace_owner_and_audits(session):
    from bank_audit.loophole.web import import_research_sources

    _service, research_id, _ = _research_with_candidate(session)
    session.execute(text("INSERT INTO loophole_workspace (workspace_id, user_id, name) VALUES (1, 'owner', 'w')"))

    with pytest.raises(HTTPException) as error:
        import_research_sources(research_id, user_id="intruder", session=session)
    assert error.value.status_code == 403

    result = import_research_sources(research_id, user_id="owner", session=session)
    assert result["imported"] == 1
    assert session.execute(text("SELECT action FROM loophole_action_log")).scalar_one() == "import_research_sources"


def test_migration_and_ui_expose_preliminary_import_and_verification_filter():
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations" / "060_loophole_preliminary_import.sql").read_text(encoding="utf-8")
    jsx = (root / "src" / "bank_audit" / "loophole" / "static" / "loophole.jsx").read_text(encoding="utf-8")

    assert "loophole_preliminary_import" in migration
    assert "UNIQUE INDEX" in migration
    assert "verification_status" in jsx
    assert "Добавить в общую базу" in jsx
