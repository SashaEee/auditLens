"""Тест keywords: seed/activate/deactivate, add_refined."""
from __future__ import annotations

import pytest

from bank_audit.loophole import keywords as kw_mod
from bank_audit.loophole import repository as repo


# Переиспользуем SQLite-фикстуру из test_repository.
from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


@pytest.fixture
def session(sqlite_session):
    return sqlite_session


def test_seed_keywords_idempotent(session):
    n1 = kw_mod.seed_keywords(session=session)
    assert n1 > 0, "seed должен добавить слова при первом вызове"
    n2 = kw_mod.seed_keywords(session=session)
    assert n2 == 0, "повторный seed не должен дублировать"


def test_activate_deactivate(session):
    kw_mod.seed_keywords(session=session)
    kws = repo.list_keywords(session=session)
    kid = kws[0]["keyword_id"]
    kw_mod.deactivate(kid, session=session)
    active = repo.list_keywords(only_active=True, session=session)
    assert all(k["keyword_id"] != kid for k in active)
    kw_mod.activate(kid, session=session)
    active = repo.list_keywords(only_active=True, session=session)
    assert any(k["keyword_id"] == kid for k in active)


def test_add_manual_and_refined(session):
    kw_mod.add_manual("ручное слово", session=session)
    kw_mod.add_refined("уточнённое слово", session=session)
    kws = repo.list_keywords(session=session)
    cats = {k["keyword"]: k["category"] for k in kws}
    assert cats.get("ручное слово") == "manual"
    assert cats.get("уточнённое слово") == "refined"


def test_active_keywords_list(session):
    kw_mod.seed_keywords(session=session)
    active = kw_mod.active_keywords(session=session)
    assert len(active) > 0
    assert all(isinstance(k, str) for k in active)
