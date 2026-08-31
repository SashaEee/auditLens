"""Регрессия bootstrap-роли published analytics view (Story 4.3)."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_published_analytics_migration_bootstraps_role_before_grant():
    sql = (ROOT / "migrations" / "052_loophole_published_analytics_view.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE ROLE loophole_readonly NOLOGIN" in sql
    assert "pg_roles" in sql
    assert "GRANT SELECT ON loophole_published_catalog_v1 TO loophole_readonly" in sql
    assert "insufficient_privilege" in sql
