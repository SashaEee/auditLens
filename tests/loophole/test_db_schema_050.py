"""Контракт idempotency mapping публикации Story 3.2."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_migration_050_creates_one_mapping_per_command_key():
    sql = (ROOT / "migrations" / "050_loophole_publication_mapping.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS loophole_publication_mapping" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_lpm_command_key" in sql
    assert "status TEXT NOT NULL DEFAULT 'publishing'" in sql
