"""Контракт Greenplum-совместимой миграции изолированных кейсов Story 2.2."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_migration_045_keeps_research_candidates_isolated_and_gp_compatible():
    sql = (ROOT / "migrations" / "045_loophole_research_cases.sql").read_text(encoding="utf-8")
    body = "\n".join(line.split("--")[0] for line in sql.splitlines()).upper()

    assert "CREATE TABLE IF NOT EXISTS LOOPHOLE_RESEARCH" in body
    assert "CREATE TABLE IF NOT EXISTS LOOPHOLE_RESEARCH_SOURCE" in body
    assert "CREATE TABLE IF NOT EXISTS LOOPHOLE_RESEARCH_CANDIDATE" in body
    assert "LOOPHOLE_RECORD" not in body
    assert "SEARCH_PARAMS JSONB NOT NULL" in body
    assert "LIMITATION_MESSAGE TEXT" in body
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body
