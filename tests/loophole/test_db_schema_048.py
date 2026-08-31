"""Контракт immutable snapshot для передачи кейса в ЦК КС."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_migration_048_keeps_submitted_snapshot_greenplum_safe():
    sql = (ROOT / "migrations" / "048_loophole_verification_snapshot.sql").read_text(encoding="utf-8")
    body = "\n".join(line.split("--")[0] for line in sql.splitlines()).upper()

    assert "CREATE TABLE IF NOT EXISTS LOOPHOLE_VERIFICATION_SNAPSHOT" in body
    for field in ("CASE_SNAPSHOT JSONB", "EVIDENCE_SNAPSHOT JSONB", "RUN_ID TEXT", "STATUS TEXT"):
        assert field in body
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body
