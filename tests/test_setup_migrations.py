"""Регрессии идемпотентного запуска setup.ps1 migrations."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_market_position_migration_replaces_later_view_shape_safely():
    sql = (ROOT / "migrations" / "017_market_position.sql").read_text(encoding="utf-8")

    assert "DROP VIEW IF EXISTS v_sber_vs_market;" in sql
    assert "DROP VIEW IF EXISTS v_market_rub_offer CASCADE;" in sql


def test_setup_powershell_keeps_schema_migration_journal():
    script = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in script
    assert "SELECT 1 FROM schema_migrations" in script
    assert "INSERT INTO schema_migrations" in script


def test_review_index_rename_is_safe_after_partial_legacy_apply():
    sql = (ROOT / "migrations" / "030_review_index_multisource.sql").read_text(
        encoding="utf-8"
    )

    assert "to_regclass('public.review_index') IS NULL" in sql
    assert "to_regclass('public.bankiru_review_fts') IS NOT NULL" in sql


def test_analytics_views_keep_current_market_offer_shape():
    sql = (ROOT / "src" / "bank_audit" / "analytics" / "views.sql").read_text(
        encoding="utf-8"
    )

    for column in ("t.grace_days", "t.cashback_pct", "o.primary_source", "t.raw", "t.rate_min", "t.rate_max", "t.psk_min", "t.psk_max", "o.segment", "o.sub_segment"):
        assert column in sql
