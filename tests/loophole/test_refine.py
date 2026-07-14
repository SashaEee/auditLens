"""Тест refine: на основе фикстуры записей LLM предлагает новые keywords."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from bank_audit.loophole import refine
from bank_audit.loophole import repository as repo
from bank_audit.loophole import keywords as kw_mod
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.hashing import sha256_text


from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


@pytest.fixture
def session(sqlite_session):
    return sqlite_session


def _llm_mock(kws):
    msg = MagicMock()
    msg.content = json.dumps({"keywords": kws}, ensure_ascii=False)
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


def _seed_loophole(session, title, snippet="скрытая комиссия"):
    rec = LoopholeRecord(
        sha256=sha256_text(title + snippet),
        title=title, snippet=snippet, bank_slug="sberbank",
        raw_text=snippet,
    )
    rid = repo.insert_record(rec, session=session)
    repo.update_verdict(
        rid, is_loophole=True, confidence=0.9, reason="ок", model="m", session=session
    )


@pytest.mark.asyncio
async def test_refine_adds_new_keywords(session):
    _seed_loophole(session, "лазейка в кредитном договоре")
    _seed_loophole(session, "отказ в выдаче вклада")
    kw_mod.add_manual("лазейка", session=session)

    llm = _llm_mock(["отказ в выдаче", "скрытые условия", "лазейка"])
    added = await refine.refine_keywords(llm=llm, session=session)
    # "лазейка" уже есть — дедуп.
    assert "отказ в выдаче" in added
    assert "скрытые условия" in added
    assert "лазейка" not in added
    # Сохранены в БД.
    all_kws = repo.list_keywords(session=session)
    words = {k["keyword"] for k in all_kws}
    assert "отказ в выдаче" in words


@pytest.mark.asyncio
async def test_refine_no_loopholes_returns_empty(session):
    added = await refine.refine_keywords(llm=_llm_mock(["x"]), session=session)
    assert added == []


@pytest.mark.asyncio
async def test_refine_tolerates_garbage(session):
    _seed_loophole(session, "лазейка")
    msg = MagicMock()
    msg.content = "не JSON"
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    added = await refine.refine_keywords(llm=llm, session=session)
    assert added == []
