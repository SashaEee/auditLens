"""Настройки model classifier Story 2.3."""
from __future__ import annotations

import pytest

from bank_audit.loophole.config import LoopholeSettings


def test_research_classifier_batch_size_comes_from_config(monkeypatch):
    monkeypatch.setenv("LOOPHOLE_RESEARCH_CLASSIFY_BATCH_SIZE", "7")

    assert LoopholeSettings.load().research_classify_batch_size == 7


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_research_classifier_batch_size_rejects_invalid_config(monkeypatch, value):
    monkeypatch.setenv("LOOPHOLE_RESEARCH_CLASSIFY_BATCH_SIZE", value)

    with pytest.raises(ValueError, match="batch"):
        LoopholeSettings.load()
