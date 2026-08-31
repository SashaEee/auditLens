"""DB-side lifecycle constraints Story 3.3."""
from __future__ import annotations

from bank_audit.config import ROOT
from bank_audit.loophole import db_schema


def test_migration_051_guarantees_one_lifecycle_result_per_business_key():
    sql = (ROOT / "migrations" / "051_loophole_lifecycle_constraints.sql").read_text(encoding="utf-8")

    assert "uq_lvs_candidate_draft" in sql
    assert "uq_lvd_snapshot" in sql
    assert "uq_lpm_decision" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql


def test_lifecycle_postgres_verifier_is_honest_without_staging(monkeypatch):
    monkeypatch.delenv("AUDITLENS_POSTGRES_STAGING_URL", raising=False)

    result = db_schema.verify_lifecycle_postgres()

    assert result["status"] == "UNVERIFIED"
    assert "staging" in result["reason"].lower()
