"""Fail-closed аналитика published-каталога Story 4.3."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from bank_audit.loophole.analytics import AnalyticsQueryError, execute_analytics_query


def test_analytics_allows_single_select_on_published_view_and_returns_json_table(session):
    session.execute(text("""
        CREATE VIEW loophole_published_catalog_v1 AS
        SELECT bank_slug, status FROM loophole_record
        WHERE status = 'published' AND is_loophole = TRUE
    """))
    session.execute(text(
        "INSERT INTO loophole_record (sha256, status, is_loophole, bank_slug) "
        "VALUES ('published', 'published', 1, 'bank')"
    ))

    result = execute_analytics_query(
        "SELECT bank_slug, status FROM loophole_published_catalog_v1 WHERE bank_slug = :bank",
        {"bank": "bank"},
        session=session,
    )

    assert result == {"columns": ["bank_slug", "status"], "rows": [["bank", "published"]]}


@pytest.mark.parametrize("sql", [
    "DELETE FROM loophole_published_catalog_v1",
    "SELECT * FROM loophole_record",
    "SELECT * FROM loophole_published_catalog_v1; SELECT 1",
    "SELECT now() FROM loophole_published_catalog_v1",
])
def test_analytics_blocks_unsafe_sql_before_db_access(sql):
    session = MagicMock()

    with pytest.raises(AnalyticsQueryError, match="Допустим только"):
        execute_analytics_query(sql, {}, session=session)

    session.execute.assert_not_called()
