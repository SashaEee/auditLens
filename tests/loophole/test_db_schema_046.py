"""Контракт реестра Telegram-целей Story 6.1."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_migration_046_has_unique_normalized_telegram_address():
    sql = (ROOT / "migrations" / "046_loophole_telegram_target.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS loophole_telegram_target" in sql
    assert "normalized_address TEXT NOT NULL" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_ltt_normalized_address" in sql
