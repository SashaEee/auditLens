"""Тест classify: LLM-классификатор «лазейка/не лазейка».

LLM-мок возвращает JSON {is_loophole, confidence, reason}. Проверяем:
- парсинг вердикта;
- сохранение вердикта в БД через repository.update_verdict;
- толерантность к мусорному JSON (fail-safe → is_loophole=False).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bank_audit.loophole import classify
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import LoopholeRecord, Verdict
from bank_audit.hashing import sha256_text


from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


@pytest.fixture
def session(sqlite_session):
    return sqlite_session


def _make_llm_mock(verdict_json: dict):
    """Мок langchain ChatModel: ainvoke → AIMessage с content=JSON."""
    msg = MagicMock()
    msg.content = json.dumps(verdict_json, ensure_ascii=False)
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


@pytest.mark.asyncio
async def test_classify_loophole_positive(session):
    llm = _make_llm_mock({"is_loophole": True, "confidence": 0.9, "reason": "скрытая комиссия"})
    verdict = await classify.classify_text(
        "договор содержит скрытую комиссию за выдачу вклада",
        llm=llm,
    )
    assert verdict.is_loophole is True
    assert verdict.confidence == 0.9
    assert "скрытая" in verdict.reason


@pytest.mark.asyncio
async def test_classify_loophole_negative(session):
    llm = _make_llm_mock({"is_loophole": False, "confidence": 0.1, "reason": "стандартные условия"})
    verdict = await classify.classify_text("обычный вклад", llm=llm)
    assert verdict.is_loophole is False


@pytest.mark.asyncio
async def test_classify_and_persist(session):
    rec = LoopholeRecord(
        sha256=sha256_text("doc1"),
        title="кредитный договор",
        raw_text="скрытая комиссия 500 руб за выдачу",
    )
    rid = repo.insert_record(rec, session=session)
    llm = _make_llm_mock({"is_loophole": True, "confidence": 0.88, "reason": "скрытая комиссия"})
    await classify.classify_record(rid, llm=llm, model="test-model", session=session)
    row = repo.get_record(rid, session=session)
    assert row["is_loophole"] == 1
    assert row["verdict_confidence"] == 0.88
    assert row["verdict_model"] == "test-model"
    assert row["status"] == "classified"


@pytest.mark.asyncio
async def test_classify_tolerates_garbage_json():
    """Мусорный ответ LLM → fail-safe (is_loophole=False, low confidence)."""
    msg = MagicMock()
    msg.content = "не JSON вообще"
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    verdict = await classify.classify_text("текст", llm=llm)
    assert verdict.is_loophole is False
    assert verdict.confidence == 0.0


@pytest.mark.asyncio
async def test_classify_strips_fences():
    """LLM оборачивает JSON в markdown-fence — парсер справляется."""
    msg = MagicMock()
    msg.content = '```json\n{"is_loophole": true, "confidence": 0.7, "reason": "ок"}\n```'
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    verdict = await classify.classify_text("текст", llm=llm)
    assert verdict.is_loophole is True
    assert verdict.confidence == 0.7
