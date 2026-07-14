"""Тест chat/clarify.py: generate_clarifications и build_enriched_question.

Мок AsyncOpenAI через monkeypatch ``clarify._client``. Без сети.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bank_audit.loophole.chat import clarify as clarify_mod


def _mock_openai_response(content: str):
    """Мок ответа openai.AsyncOpenAI.chat.completions.create."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.fixture
def patched_client(monkeypatch):
    """Патчит clarify._client чтобы вернуть мок-клиент с настраиваемым create."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    monkeypatch.setattr(clarify_mod, "_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_generate_clarifications_complete_true(patched_client, monkeypatch):
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps({"complete": True, "reason": "всё есть", "questions": []})
    )
    result = await clarify_mod.generate_clarifications(
        "Проверь вклады Сбербанка 2025 на скрытые комиссии"
    )
    assert result["complete"] is True
    assert result["questions"] == []


@pytest.mark.asyncio
async def test_generate_clarifications_with_questions(patched_client, monkeypatch):
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    payload = {
        "complete": False,
        "reason": "не указан банк",
        "questions": [
            {
                "id": "bank",
                "question": "Какой банк?",
                "type": "single",
                "allow_other": True,
                "options": [
                    {"value": "sber", "label": "Сбербанк", "recommended": True},
                    {"value": "vtb", "label": "ВТБ"},
                ],
            }
        ],
    }
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps(payload)
    )
    result = await clarify_mod.generate_clarifications("найди лазейки в кредитах")
    assert result["complete"] is False
    assert len(result["questions"]) == 1
    assert result["questions"][0]["id"] == "bank"
    assert result["questions"][0]["options"][0]["label"] == "Сбербанк"


@pytest.mark.asyncio
async def test_generate_clarifications_fail_open_on_llm_error(patched_client, monkeypatch):
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    patched_client.chat.completions.create.side_effect = RuntimeError("network")
    result = await clarify_mod.generate_clarifications("вопрос")
    assert result["complete"] is True
    assert result["reason"] == "llm_error"


@pytest.mark.asyncio
async def test_generate_clarifications_fail_open_on_bad_json(patched_client, monkeypatch):
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        "это не JSON вообще"
    )
    result = await clarify_mod.generate_clarifications("вопрос")
    assert result["complete"] is True
    assert result["reason"] == "parse_fail"


@pytest.mark.asyncio
async def test_generate_clarifications_disabled(monkeypatch):
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "0")
    result = await clarify_mod.generate_clarifications("что угодно")
    assert result["complete"] is True
    assert result["reason"] == "disabled"


@pytest.mark.asyncio
async def test_build_enriched_question(patched_client):
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        "Проверь скрытые комиссии по вкладам Сбербанка за 2025 год"
    )
    answers = [
        {
            "question": "Какой банк?",
            "selected": ["Сбербанк"],
            "other": None,
        }
    ]
    enriched = await clarify_mod.build_enriched_question(
        "Проверь скрытые комиссии по вкладам", answers
    )
    assert "Сбербанк" in enriched or "сбербанк" in enriched.lower()


@pytest.mark.asyncio
async def test_build_enriched_question_no_answers():
    # Без ответов — возвращается исходный запрос без вызова LLM.
    enriched = await clarify_mod.build_enriched_question("просто запрос", [])
    assert enriched == "просто запрос"


@pytest.mark.asyncio
async def test_build_enriched_question_fallback_on_error(patched_client):
    patched_client.chat.completions.create.side_effect = RuntimeError("boom")
    answers = [{"question": "Банк?", "selected": ["ВТБ"], "other": None}]
    enriched = await clarify_mod.build_enriched_question("вопрос", answers)
    # template fallback содержит уточнение
    assert "ВТБ" in enriched
