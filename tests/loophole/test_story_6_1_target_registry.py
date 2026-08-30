"""TDD-проверки Story 6.1: регистрация Telegram-цели без управления доступом."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from bank_audit.loophole.telegram_targets import InvalidTelegramTarget, TargetRegistryService


@pytest.fixture
def telegram_targets_table(session):
    session.execute(text("""
        CREATE TABLE loophole_telegram_target (
            target_id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_address TEXT NOT NULL UNIQUE,
            target_kind TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """))
    session.commit()
    return session


@pytest.mark.parametrize(
    ("address", "expected", "kind"),
    [
        ("@Bank_News", "t.me/bank_news", "public"),
        ("https://t.me/bank_news", "t.me/bank_news", "public"),
        ("t.me/+AbCdEf123", "t.me/+AbCdEf123", "invite"),
        ("https://telegram.me/joinchat/AbCdEf123", "t.me/+AbCdEf123", "invite"),
    ],
)
def test_register_normalizes_supported_addresses(telegram_targets_table, address, expected, kind):
    result = TargetRegistryService(telegram_targets_table).register(address)

    assert result.normalized_address == expected
    assert result.target_kind == kind
    assert result.registration == "created"
    assert result.account_access == "unchecked"
    assert result.collection == "not_started"


def test_repeat_registration_returns_existing_target_without_duplicate(telegram_targets_table):
    service = TargetRegistryService(telegram_targets_table)

    created = service.register("@bank_news")
    existing = service.register("https://t.me/Bank_News")

    assert existing.target_id == created.target_id
    assert existing.registration == "existing"
    assert telegram_targets_table.execute(
        text("SELECT count(*) FROM loophole_telegram_target")
    ).scalar_one() == 1


@pytest.mark.parametrize(
    "address",
    ["", "https://example.com/channel", "@bad", "t.me/joinchat/", "t.me/+bad!"],
)
def test_register_rejects_unsupported_target_fail_closed(telegram_targets_table, address):
    with pytest.raises(InvalidTelegramTarget):
        TargetRegistryService(telegram_targets_table).register(address)
