"""Контракт append-only решения ЦК КС Story 3.1."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_migration_049_has_allowed_decisions_and_postgres_append_only_guard():
    sql = (ROOT / "migrations" / "049_loophole_verification_decision.sql").read_text(encoding="utf-8")
    body = "\n".join(line.split("--")[0] for line in sql.splitlines()).upper()

    assert "CREATE TABLE IF NOT EXISTS LOOPHOLE_VERIFICATION_DECISION" in body
    assert all(value in sql for value in ("vulnerability", "fraud_scheme", "not_confirmed"))
    assert "TRG_LVD_APPEND_ONLY" in body
    assert "IF VERSION() NOT ILIKE '%GREENPLUM%' THEN" in body
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body
