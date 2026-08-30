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
@pytest.mark.parametrize("query", ["", "я"])
async def test_short_query_is_fail_closed_even_when_llm_says_complete(
    patched_client,
    monkeypatch,
    query,
):
    """Серверный guard не принимает пустой/короткий запрос по ответу LLM."""
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps({"complete": True, "questions": []})
    )

    result = await clarify_mod.generate_clarifications(query)

    assert result == {
        "complete": False,
        "questions": [],
        "reason": "query_too_short",
    }
    patched_client.chat.completions.create.assert_not_awaited()


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
async def test_clarification_prompts_mask_query_history_and_answers(patched_client, monkeypatch):
    """В clarification и rewrite не уходят credential и телефон из пользовательских данных."""
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    secret = "sk-clarify-secret"
    phone = "+7 999 123-45-67"
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps(
            {
                "complete": False,
                "reason": "нужно уточнение",
                "questions": [{"id": "scope", "question": "Что исследовать?", "type": "text"}],
            }
        )
    )

    await clarify_mod.generate_clarifications(
        f"Проверь запрос {phone}, credential={secret}",
        history=[{"role": "user", "content": f"Ранее: {phone}, {secret}"}],
    )
    clarification_messages = patched_client.chat.completions.create.call_args.kwargs["messages"]
    clarification_prompt = json.dumps(clarification_messages, ensure_ascii=False)

    assert secret not in clarification_prompt
    assert phone not in clarification_prompt
    assert "[PHONE_" in clarification_prompt
    assert "История диалога" in clarification_prompt

    patched_client.chat.completions.create.return_value = _mock_openai_response(
        "Проверь запрос с выбранным банком и периодом"
    )
    await clarify_mod.build_enriched_question(
        f"Проверь запрос {phone}",
        [{"question": "Credential", "selected": [secret]}],
    )
    rewrite_messages = patched_client.chat.completions.create.call_args.kwargs["messages"]
    rewrite_prompt = json.dumps(rewrite_messages, ensure_ascii=False)

    assert secret not in rewrite_prompt
    assert phone not in rewrite_prompt
    assert "[PHONE_" in rewrite_prompt


@pytest.mark.asyncio
async def test_generate_clarifications_fail_closed_on_llm_error(patched_client, monkeypatch):
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    patched_client.chat.completions.create.side_effect = RuntimeError("network")
    result = await clarify_mod.generate_clarifications("вопрос")
    assert result["complete"] is False
    assert result["reason"] == "clarification_unavailable"
    assert result["questions"] == []


@pytest.mark.asyncio
async def test_generate_clarifications_fail_closed_on_bad_json(patched_client, monkeypatch):
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        "это не JSON вообще"
    )
    result = await clarify_mod.generate_clarifications("вопрос")
    assert result["complete"] is False
    assert result["reason"] == "clarification_unavailable"
    assert result["questions"] == []


@pytest.mark.asyncio
async def test_generate_clarifications_disabled_does_not_bypass_contract(
    patched_client,
    monkeypatch,
):
    """Отключённый флаг не должен обходить обязательную clarification-воронку."""
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "0")
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps(
            {
                "complete": False,
                "reason": "не указан банк",
                "questions": [{"id": "bank", "question": "Какой банк?", "type": "text"}],
            }
        )
    )

    result = await clarify_mod.generate_clarifications("что угодно")

    assert result["complete"] is False
    assert result["questions"]


@pytest.mark.asyncio
async def test_short_query_does_not_bypass_clarification(patched_client, monkeypatch):
    """Короткий запрос блокируется до LLM и не разрешает execution."""
    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps(
            {
                "complete": False,
                "reason": "нужно уточнение",
                "questions": [{"id": "scope", "question": "Что исследовать?", "type": "text"}],
            }
        )
    )

    result = await clarify_mod.generate_clarifications("я")

    assert result["complete"] is False
    assert result["questions"] == []
    assert result["reason"] == "query_too_short"
    patched_client.chat.completions.create.assert_not_awaited()


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
    """Пустые ответы не превращаются в разрешённый исходный запрос."""
    enriched = await clarify_mod.build_enriched_question("просто запрос", [])

    assert enriched == {
        "complete": False,
        "questions": [],
        "reason": "answers_required",
    }


@pytest.mark.asyncio
async def test_build_enriched_question_fallback_on_error(patched_client):
    """Ошибка rewrite блокирует execution вместо template fallback."""
    patched_client.chat.completions.create.side_effect = RuntimeError("boom")
    answers = [{"question": "Банк?", "selected": ["ВТБ"], "other": None}]
    enriched = await clarify_mod.build_enriched_question("вопрос", answers)
    assert enriched == {
        "complete": False,
        "questions": [],
        "reason": "clarification_unavailable",
    }


@pytest.mark.asyncio
async def test_build_enriched_question_returns_typed_fail_closed_result_on_exception(
    patched_client,
):
    """Rewrite exception не выдаёт строку, которую можно передать агенту."""
    patched_client.chat.completions.create.side_effect = RuntimeError("raw rewrite")

    result = await clarify_mod.build_enriched_question(
        "проверь вклад", [{"question": "Банк?", "selected": ["ВТБ"]}]
    )

    assert result["complete"] is False
    assert result["reason"] == "clarification_unavailable"
