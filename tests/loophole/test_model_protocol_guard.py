"""Служебный протокол модели не заменяет проверенный аналитический ответ."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bank_audit.loophole.chat.hooks import (
    MODEL_PROTOCOL_ERROR,
    AuditHook,
    ModelProtocolError,
    validate_final_content,
)

EXACT_PROTOCOL = '<ipython_send_cmd>\n<parameter name="command">ls /tmp/__outputs</parameter>'


@pytest.mark.parametrize("content", [
    EXACT_PROTOCOL,
    "Проверю источники и подготовлю ответ.\n\n" + EXACT_PROTOCOL,
    '<tool_call>\n{"name":"audit_web_fetch","arguments":{}}</tool_call>',
    '<function_call name="audit_web_search">{"query":"карты"}</function_call>',
    '<minimax:invoke name="audit_web_search">',
])
def test_protocol_markup_is_typed_invalid_final(content):
    with pytest.raises(ModelProtocolError, match=MODEL_PROTOCOL_ERROR):
        validate_final_content(content)


@pytest.mark.parametrize("content", [
    "Механизм возврата комиссии подтверждается цитатой из источника.",
    "Текст содержит термин tool_call, но не является вызовом инструмента.",
    "В источнике приведён пример `<ipython_send_cmd>`; он не выполнялся.",
    "Пример XML из источника:\n```xml\n" + EXACT_PROTOCOL + "\n```\nВывод аудитора.",
    "Процитированный фрагмент:\n> <ipython_send_cmd>\n> параметр команды\nАнализ рисков.",
    "Код проверки:\n```python\nfunction_call = 'пример'\nprint(function_call)\n```",
    "Пример кода с отступом:\n\n    <ipython_send_cmd>\n    параметр команды",
])
def test_ordinary_analysis_and_quoted_code_remain_valid(content):
    validate_final_content(content)


@pytest.mark.asyncio
async def test_hook_drops_whole_buffer_after_protocol_in_final():
    hook = AuditHook()
    hook.stream_delta_for_sse("План проверки ранее найденных источников. " * 8)
    hook.stream_delta_for_sse("<ipython_send_")
    hook.stream_delta_for_sse('cmd>\n<parameter name="command">ls /tmp/__outputs</parameter>')
    await hook.after_run(SimpleNamespace(final_content=EXACT_PROTOCOL, stop_reason="completed"))
    assert hook.flush_stream_for_sse() == ""
    assert hook.stop_reason == MODEL_PROTOCOL_ERROR
    assert EXACT_PROTOCOL not in hook.final_answer
    assert "План проверки" not in hook.final_answer
    assert hook.finalize_content(None, EXACT_PROTOCOL) == hook.final_answer


@pytest.mark.asyncio
async def test_managed_run_returns_terminal_protocol_failure_without_records():
    from bank_audit.loophole.agent import AgentRunContext, ManagedAgent

    context = AgentRunContext("analyst", 1, "Найди лазейки", "protocol-run")
    context.pending_records.append({"title": "Не сохранять", "is_loophole": True})

    class Bot:
        async def run(self, prompt, **kwargs):
            return SimpleNamespace(content=EXACT_PROTOCOL, stop_reason="completed")

        async def aclose(self):
            pass

    result = await ManagedAgent(context, Bot(), "").run()
    assert result.stop_reason == MODEL_PROTOCOL_ERROR
    assert result.errors == (MODEL_PROTOCOL_ERROR,)
    assert result.partial is False
    assert result.records == result.sources == ()
    assert "ipython_send_cmd" not in result.answer


@pytest.mark.asyncio
async def test_real_nanobot_finalization_rejects_protocol_content(monkeypatch, tmp_path):
    from nanobot.providers.base import LLMResponse

    from bank_audit.loophole.agent import AgentRunContext, ManagedAgent
    from bank_audit.loophole.chat.nanobot_agent import create_nanobot

    bot, config_path = create_nanobot(workspace=tmp_path / "nanobot")

    async def malformed_provider(**kwargs):
        await kwargs["on_content_delta"](EXACT_PROTOCOL)
        return LLMResponse(content=EXACT_PROTOCOL)

    monkeypatch.setattr(bot._loop.provider, "chat_stream_with_retry", malformed_provider)
    context = AgentRunContext("analyst", 1, "Исследование карт", "protocol-sdk")
    hook = AuditHook()
    managed = ManagedAgent(context, bot, config_path)
    _events = [event async for event in managed.stream("проверка", hook=hook)]
    assert hook.stop_reason == MODEL_PROTOCOL_ERROR
    assert "ipython_send_cmd" not in hook.final_answer
    assert hook.flush_stream_for_sse() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered_plan", [False, True])
async def test_protocol_sse_is_terminal_error_without_report_or_evidence(
    monkeypatch, session, buffered_plan,
):
    from nanobot.sdk.types import StreamEvent

    from bank_audit.loophole.chat import graph

    messages = []
    audits = []

    class Factory:
        def create(self, context, **kwargs):
            context.pending_records.append({"title": "Кандидат", "is_loophole": True})
            context.fetched_sources["https://example.test/source"] = {
                "url": "https://example.test/source", "extracted_text": "Прочитанный источник",
            }
            return Bot()

    class Bot:
        async def stream(self, prompt, *, hook):
            if buffered_plan:
                yield StreamEvent(type="text.delta", delta="План проверки. " * 26)
            yield StreamEvent(type="text.delta", delta=EXACT_PROTOCOL)
            await hook.after_run(SimpleNamespace(
                final_content=EXACT_PROTOCOL, stop_reason="completed",
            ))

        async def aclose(self):
            pass

    def forbidden_persistence(*args, **kwargs):
        raise AssertionError("Протокольный сбой не сохраняет исследование и evidence")

    monkeypatch.setattr(graph, "AgentFactory", Factory)
    monkeypatch.setattr(graph, "_persist_confirmed_findings", forbidden_persistence)
    monkeypatch.setattr(graph, "_save_agent_audit", lambda state, result, **kw: audits.append(result))
    monkeypatch.setattr(graph.repo, "add_chat_message", lambda *args, **kwargs: messages.append(args))
    events = [event async for event in graph.stream_chat({
        "query": "Найди 1 лазейку по кредитным картам за 2026 год",
        "workspace_id": 1, "user_id": "analyst", "clarification_verified": True,
    }, session=session)]
    serialized = json.dumps(events, ensure_ascii=False)
    assert "ipython_send_cmd" not in serialized
    assert "План проверки" not in serialized
    assert not any(event["event"] in {"token", "records", "partial"} for event in events)
    assert events[-1]["event"] == "phase"
    assert events[-1]["data"]["phase"] == "error"
    assert events[-1]["data"]["code"] == MODEL_PROTOCOL_ERROR
    assert events[-1]["data"]["partial"] is False
    assert not messages
    assert len(audits) == 1
    assert audits[0].stop_reason == MODEL_PROTOCOL_ERROR
    assert "ipython_send_cmd" not in audits[0].answer
