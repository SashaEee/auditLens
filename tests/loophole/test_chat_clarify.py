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


def test_client_uses_short_timeout_without_transport_retries(monkeypatch):
    """FAST-gate не возвращается к 70-секундному timeout и transport retry."""
    captured: dict = {}
    sentinel = object()

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(clarify_mod, "AsyncOpenAI", fake_openai)

    client = clarify_mod._client()

    assert client is sentinel
    assert {key: value for key, value in captured.items() if key != "http_client"} == {
        "base_url": "https://llm.example/v1",
        "api_key": "test-key",
        "timeout": 15,
        "max_retries": 0,
    }
    assert captured["http_client"].trust_env is False


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
async def test_product_and_period_make_research_actionable_without_llm(patched_client):
    """Явный продукт и период не должны повторно уточняться при недоступном FAST-gate."""
    query = (
        "Найди лазейки по продукту кредитная карта за август 2026 года. "
        "Не показывай публикации, созданные раньше августа 2026 года."
    )

    result = await clarify_mod.generate_clarifications(query)

    assert result["complete"] is True
    assert result["questions"] == []
    assert result["reason"] == "actionable_product_period_scope"
    patched_client.chat.completions.create.assert_not_awaited()


def test_fail_closed_question_asks_only_for_missing_period():
    """Fallback не повторяет уже указанный продукт и не требует необязательный банк."""
    questions = clarify_mod.clarification_questions(
        clarify_mod._clarification_unavailable(),
        query="Найди лазейки по продукту кредитная карта",
    )

    assert [question["id"] for question in questions] == ["period"]
    assert "период" in questions[0]["question"].lower()
    assert "банк" not in questions[0]["question"].lower()
    assert "продукт" not in questions[0]["question"].lower()


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
async def test_gate_keeps_only_the_first_useful_question_and_uses_fast_model(
    patched_client,
    monkeypatch,
):
    """Лишние вопросы модели не должны растягивать clarification-воронку."""
    monkeypatch.delenv("LOOPHOLE_ASKING_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL_FAST", "fast-clarification-model")
    patched_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps(
            {
                "complete": False,
                "reason": "нужно уточнение",
                "questions": [
                    {"id": "scope", "question": "Что исследовать?", "type": "text"},
                    {"id": "period", "question": "За какой период?", "type": "text"},
                ],
            }
        )
    )

    result = await clarify_mod.generate_clarifications("найди лазейки")

    assert [question["id"] for question in result["questions"]] == ["scope"]
    call = patched_client.chat.completions.create.call_args.kwargs
    assert call["model"] == "fast-clarification-model"
    assert "extra_body" not in call


@pytest.mark.asyncio
async def test_clarification_prompts_mask_query_history_and_answers(patched_client, monkeypatch):
    """В единственный LLM-gate не уходят credential и телефон пользователя."""
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

    llm_calls_before_answer = patched_client.chat.completions.create.await_count
    enriched = await clarify_mod.build_enriched_question(
        f"Проверь запрос {phone}",
        [{"question": "Credential", "selected": [secret]}],
    )

    assert patched_client.chat.completions.create.await_count == llm_calls_before_answer
    assert enriched == (
        f"Проверь запрос {phone} (уточнения — Credential: {secret})"
    )


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
async def test_build_enriched_question_is_deterministic_without_llm(patched_client):
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
    assert enriched == (
        "Проверь скрытые комиссии по вкладам "
        "(уточнения — Какой банк: Сбербанк)"
    )
    patched_client.chat.completions.create.assert_not_awaited()


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
async def test_build_enriched_question_combines_selected_and_other_answers():
    result = await clarify_mod.build_enriched_question(
        "проверь вклад",
        [
            {
                "question": "Банк?",
                "selected": ["ВТБ"],
                "other": "Альфа-Банк",
            }
        ],
    )

    assert result == "проверь вклад (уточнения — Банк: ВТБ, Альфа-Банк)"
