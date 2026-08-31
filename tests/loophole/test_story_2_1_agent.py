"""TDD-контракт Story 2.1: управляемый агент исследования."""
from __future__ import annotations

import asyncio
import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


def _agent_module():
    """Загружает новый публичный слой агента с читаемой ошибкой RED."""
    try:
        return importlib.import_module("bank_audit.loophole.agent")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Слой управляемого агента ещё не реализован: {exc}")


def test_skill_registry_uses_only_allowlisted_read_only_skills():
    agent = _agent_module()

    registry = agent.SkillRegistry.default()

    assert "audit_web_search" in registry.names
    assert "audit_db_query" in registry.names
    assert "audit_save_loophole" not in registry.names
    with pytest.raises(agent.UnknownSkillError):
        registry.select(("audit_save_loophole",))


def test_managed_agent_prompt_does_not_require_unavailable_write_skill():
    """System prompt не требует write-skill, которого нет в managed allowlist."""
    prompt = Path(
        "src/bank_audit/loophole/chat/prompt/07_nanobot_system.md"
    ).read_text(encoding="utf-8")

    assert "audit_save_loophole" not in prompt
    assert "read-only инструменты" in prompt
    assert "инструменты записи" in prompt


def test_skill_registry_rejects_write_skill_selected_by_environment(monkeypatch):
    """Переменная окружения не может включить write-skill."""
    agent = _agent_module()

    monkeypatch.setenv("LOOPHOLE_AGENT_SKILLS", "audit_save_loophole")

    with pytest.raises(agent.UnknownSkillError):
        agent.SkillRegistry.default()


def test_skill_registry_rejects_mixed_environment_with_write_skill(monkeypatch):
    """Смешанный список не должен частично зарегистрироваться."""
    agent = _agent_module()

    monkeypatch.setenv(
        "LOOPHOLE_AGENT_SKILLS",
        "audit_web_search,audit_save_loophole,audit_db_query",
    )

    with pytest.raises(agent.UnknownSkillError):
        agent.SkillRegistry.default()


def test_skill_registry_environment_cannot_replace_immutable_allowlist(monkeypatch):
    """Env может быть только проверенным deploy-настройкой, но не меняет allowlist."""
    agent = _agent_module()

    monkeypatch.setenv("LOOPHOLE_AGENT_SKILLS", "audit_web_search")

    registry = agent.SkillRegistry.default()

    assert registry.names == agent.DEFAULT_ALLOWED_SKILLS


def test_skill_registry_rejects_unknown_environment_skill(monkeypatch):
    """Неизвестное имя не должно частично попадать в реестр."""
    agent = _agent_module()

    monkeypatch.setenv("LOOPHOLE_AGENT_SKILLS", "audit_web_search,unknown_skill")

    with pytest.raises(agent.UnknownSkillError):
        agent.SkillRegistry.default()


def test_skill_registry_checks_read_only_metadata_not_only_name():
    """Навык с безопасным именем, но read_only=False, тоже запрещён."""
    agent = _agent_module()

    class FakeWriteTool:
        @property
        def name(self):
            return "audit_web_search"

        @property
        def read_only(self):
            return False

    with pytest.raises(agent.UnknownSkillError):
        agent.SkillRegistry((FakeWriteTool,), allowlist=("audit_web_search",))


def test_agent_factory_creates_isolated_agent_with_registry_tools(monkeypatch, tmp_path):
    agent = _agent_module()
    captured = {}

    def fake_create_nanobot(**kwargs):
        captured.update(kwargs)
        return object(), str(tmp_path / "nanobot.json")

    monkeypatch.setattr(agent, "create_nanobot", fake_create_nanobot)
    context = agent.AgentRunContext(
        user_id="analyst-1",
        workspace_id=17,
        query="проверь условия вклада",
        run_id="run-1",
    )

    created = agent.AgentFactory().create(context)

    assert created.context == context
    assert captured["tool_classes"]
    assert all(cls().name != "audit_save_loophole" for cls in captured["tool_classes"])
    assert str(context.workspace_id) in str(captured["workspace"])
    assert context.run_id in str(captured["workspace"])


def test_agent_factory_rejects_run_id_path_traversal(monkeypatch, tmp_path):
    """Путь запуска не принимает traversal в server-side run_id."""
    agent = _agent_module()

    def forbidden_create_nanobot(**kwargs):
        raise AssertionError("небезопасный run_id не должен дойти до создания агента")

    monkeypatch.setattr(agent, "create_nanobot", forbidden_create_nanobot)
    context = agent.AgentRunContext(
        user_id="analyst-1",
        workspace_id=17,
        query="запрос",
        run_id="../../escape",
    )

    with pytest.raises(ValueError, match="run_id"):
        agent.AgentFactory().create(context)


def test_agent_factory_assigns_unique_run_ids_for_empty_contexts(monkeypatch, tmp_path):
    """Пустой run_id не смешивает рабочие каталоги двух запусков."""
    agent = _agent_module()
    captured = []

    class FakeBot:
        async def aclose(self):
            return None

    def fake_create_nanobot(**kwargs):
        captured.append(kwargs)
        return FakeBot(), str(tmp_path / f"config-{len(captured)}.json")

    monkeypatch.setattr(agent, "create_nanobot", fake_create_nanobot)
    context = agent.AgentRunContext(
        user_id="analyst-1",
        workspace_id=17,
        query="проверь условия вклада",
        run_id="",
    )

    first = agent.AgentFactory().create(context)
    second = agent.AgentFactory().create(context)

    assert first.context.run_id
    assert second.context.run_id
    assert first.context.run_id != second.context.run_id
    assert captured[0]["workspace"] != captured[1]["workspace"]


@pytest.mark.asyncio
async def test_managed_agent_unlinks_config_when_bot_close_fails(tmp_path):
    """Временный конфиг удаляется даже при исключении из aclose nanobot."""
    agent = _agent_module()
    config_path = tmp_path / "nanobot.json"
    config_path.write_text("{}", encoding="utf-8")

    class BrokenBot:
        async def aclose(self):
            raise RuntimeError("raw close payload")

    managed = agent.ManagedAgent(
        agent.AgentRunContext("analyst-1", 17, "запрос", "run-close"),
        BrokenBot(),
        str(config_path),
    )

    with pytest.raises(RuntimeError):
        await managed.aclose()

    assert not config_path.exists()


@pytest.mark.asyncio
async def test_managed_agent_aclose_survives_repeated_cancellation(tmp_path):
    """Повторная отмена не обрывает bot cleanup и удаление временного конфига."""
    agent = _agent_module()
    config_path = tmp_path / "nanobot.json"
    config_path.write_text("{}", encoding="utf-8")
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    calls = 0

    class SlowBot:
        async def aclose(self):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            completed.set()

    managed = agent.ManagedAgent(
        agent.AgentRunContext("analyst-1", 17, "запрос", "run-cancel-close"),
        SlowBot(),
        str(config_path),
    )

    close_task = asyncio.create_task(managed.aclose())
    await asyncio.wait_for(started.wait(), timeout=1)
    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert calls == 1
    assert completed.is_set()
    assert not config_path.exists()


def test_create_nanobot_unlinks_config_when_from_config_fails(monkeypatch, tmp_path):
    """Временный JSON удаляется, если Nanobot не создался из конфига."""
    import nanobot

    from bank_audit.loophole.chat import nanobot_agent

    created = {}
    real_mkstemp = nanobot_agent.tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created["path"] = Path(path)
        return fd, path

    def broken_from_config(**kwargs):
        raise RuntimeError("from_config failed")

    monkeypatch.setattr(nanobot_agent.tempfile, "mkstemp", capture_mkstemp)
    monkeypatch.setattr(nanobot.Nanobot, "from_config", broken_from_config)

    with pytest.raises(RuntimeError, match="from_config failed"):
        nanobot_agent.create_nanobot(workspace=tmp_path / "workspace")

    assert not created["path"].exists()


def test_create_nanobot_unlinks_config_when_workspace_creation_fails(monkeypatch, tmp_path):
    """Временный JSON удаляется, если не создан рабочий каталог."""
    from bank_audit.loophole.chat import nanobot_agent

    created = {}
    real_mkstemp = nanobot_agent.tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created["path"] = Path(path)
        return fd, path

    def broken_mkdir(self, *args, **kwargs):
        raise RuntimeError("workspace mkdir failed")

    monkeypatch.setattr(nanobot_agent.tempfile, "mkstemp", capture_mkstemp)
    monkeypatch.setattr(nanobot_agent.Path, "mkdir", broken_mkdir)

    with pytest.raises(RuntimeError, match="workspace mkdir failed"):
        nanobot_agent.create_nanobot(workspace=tmp_path / "workspace")

    assert not created["path"].exists()


@pytest.mark.asyncio
async def test_agent_marks_nanobot_iteration_limit_as_partial():
    """Сигнал nanobot max_iterations даёт частичный результат и счётчик."""
    agent = _agent_module()
    from nanobot.sdk.types import RunResult

    class LimitedBot:
        async def run(self, prompt, **kwargs):
            hook = kwargs["hooks"][0]
            await hook.after_iteration(
                SimpleNamespace(iteration=19, tool_calls=[], tool_events=[])
            )
            return RunResult(
                content="Найденный до лимита фрагмент",
                stop_reason="max_iterations",
                metadata={"iterations": 20},
            )

        async def aclose(self):
            return None

    managed = agent.ManagedAgent(
        agent.AgentRunContext("analyst-1", 17, "запрос", "run-limit", max_iterations=20),
        LimitedBot(),
        "",
    )

    result = await managed.run()

    assert result.partial is True
    assert result.iterations == 20
    assert "max_iterations" in result.errors
    assert "лимит" in result.answer.lower()


def test_nanobot_iteration_limit_is_default_twenty_and_configurable(monkeypatch):
    from bank_audit.loophole.chat.nanobot_agent import build_nanobot_config

    monkeypatch.delenv("LOOPHOLE_NANOBOT_MAX_ITERATIONS", raising=False)
    assert build_nanobot_config()["agents"]["defaults"]["maxToolIterations"] == 20

    monkeypatch.setenv("LOOPHOLE_NANOBOT_MAX_ITERATIONS", "7")
    assert build_nanobot_config()["agents"]["defaults"]["maxToolIterations"] == 7


@pytest.mark.parametrize("raw_limit", ["-1", "0", "501", "not-a-number"])
def test_nanobot_iteration_limit_rejects_invalid_env(monkeypatch, raw_limit):
    """Недопустимый env-лимит не превращается в разрешённый config."""
    from bank_audit.loophole.chat.nanobot_agent import build_nanobot_config

    monkeypatch.setenv("LOOPHOLE_NANOBOT_MAX_ITERATIONS", raw_limit)

    with pytest.raises(ValueError, match="max_iterations"):
        build_nanobot_config()


def test_invalid_iteration_env_does_not_create_nanobot(monkeypatch):
    """Invalid iteration env блокирует AgentFactory до вызова Nanobot."""
    agent = _agent_module()
    monkeypatch.setenv("LOOPHOLE_NANOBOT_MAX_ITERATIONS", "501")

    def forbidden_create_nanobot(**kwargs):
        raise AssertionError("Nanobot не должен создаваться при invalid max iterations")

    monkeypatch.setattr(agent, "create_nanobot", forbidden_create_nanobot)
    context = agent.AgentRunContext(
        user_id="analyst-1",
        workspace_id=17,
        query="проверь вклад",
        run_id="run-invalid-limit",
    )

    with pytest.raises(ValueError, match="max_iterations"):
        agent.AgentFactory().create(context, session=object())


@pytest.mark.asyncio
async def test_clarification_wait_does_not_consume_iteration(monkeypatch):
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import run_chat

    async def fake_gen(question, history=None):
        return {"complete": False, "questions": [{"id": "bank", "question": "Какой банк?"}]}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", fake_gen)
    state = {
        "query": "проверь вклад",
        "workspace_id": 17,
        "user_id": "analyst-1",
        "iterations": 0,
    }

    result = await run_chat(state)

    assert result["phase"] == "await_clarify"
    assert result["iterations"] == 0


@pytest.mark.asyncio
async def test_incomplete_clarification_without_questions_emits_safe_question_and_token(monkeypatch):
    """Неполный ответ без questions не должен завершать ход или запускать агента."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import stream_chat

    async def incomplete(question, history=None):
        return {"complete": False, "questions": [], "reason": "query_too_short"}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)

    events = [
        event
        async for event in stream_chat(
            {"query": "я", "workspace_id": 17, "user_id": "analyst-1"}
        )
    ]

    question = next(event for event in events if event["event"] == "question")
    assert question["data"]["questions"]
    assert question["data"]["clarification_token"]
    assert any(
        event["event"] == "phase" and event["data"].get("phase") == "await_clarify"
        for event in events
    )
    assert not any(
        event["event"] == "phase" and event["data"].get("phase") == "execute"
        for event in events
    )


@pytest.mark.asyncio
async def test_generic_incomplete_clarification_keeps_run_and_stream_in_retry_state(monkeypatch):
    """Общий incomplete без вопросов не должен завершать ход или запускать агента."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import run_chat, stream_chat

    async def incomplete(question, history=None):
        return {
            "complete": False,
            "questions": [],
            "reason": "clarification_unavailable",
        }

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)

    class ForbiddenFactory:
        def create(self, *args, **kwargs):
            raise AssertionError("AgentFactory нельзя запускать без clarification")

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", ForbiddenFactory)
    state = {
        "query": "проверь вклад",
        "workspace_id": 17,
        "user_id": "analyst-1",
        "iterations": 0,
    }

    result = await run_chat(state)
    events = [event async for event in stream_chat(state)]

    assert result["phase"] == "await_clarify"
    assert result["clarify_questions"]
    assert result["clarification_token"]
    assert result["iterations"] == 0
    assert any(event["event"] == "question" for event in events)
    assert any(
        event["event"] == "phase" and event["data"].get("phase") == "await_clarify"
        for event in events
    )
    assert not any(
        event["event"] == "phase" and event["data"].get("phase") == "execute"
        for event in events
    )
    assert not any(event["event"] in {"done", "answer"} for event in events)


@pytest.mark.asyncio
async def test_agent_prompt_masks_query_and_history(monkeypatch):
    """В prompt managed agent не попадают credential и телефон из state."""
    from bank_audit.loophole.agent import AgentResult
    from bank_audit.loophole.chat.graph import _run_nanobot

    captured = {}

    class FakeAgent:
        async def run(self, prompt, *, session=None):
            captured["prompt"] = prompt
            return AgentResult(answer="Безопасный ответ", run_id="run-mask")

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return FakeAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)
    secret = "sk-agent-secret"
    phone = "+7 912 345-67-89"

    await _run_nanobot(
        {
            "query": f"Проверь контакт {phone}, credential={secret}",
            "messages": [{"role": "user", "content": f"История: {phone} {secret}"}],
            "workspace_id": 17,
            "user_id": "analyst-1",
            "run_id": "run-mask",
        }
    )

    assert secret not in captured["prompt"]
    assert phone not in captured["prompt"]
    assert "[PHONE_" in captured["prompt"]


@pytest.mark.asyncio
async def test_noncritical_skill_failure_returns_partial_answer(monkeypatch, tmp_path, session):
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import run_chat

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)

    agent = _agent_module()

    class FakeBot:
        async def run(self, prompt, **kwargs):
            hook = kwargs["hooks"][0]
            await hook.on_stream(None, "Результат по доступному источнику.")
            await hook.after_iteration(
                SimpleNamespace(
                    tool_calls=[],
                    tool_events=[
                        {
                            "name": "audit_web_fetch",
                            "status": "error",
                            "error": "источник недоступен",
                        }
                    ],
                )
            )
            return SimpleNamespace(content="")

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return agent.ManagedAgent(
                context,
                FakeBot(),
                str(tmp_path / "nanobot.json"),
            )

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)
    result = await run_chat(
        {"query": "проверь вклад", "workspace_id": None, "user_id": "analyst-1"},
        session=session,
    )

    assert "Результат по доступному источнику" in result["answer"]
    assert "частич" in result["answer"].lower()
    assert result["tools_used"] == ["audit_web_fetch"]


def test_agent_audit_is_redacted_and_contains_run_metadata(session):
    agent = _agent_module()
    from sqlalchemy import text

    from bank_audit.loophole import repository as repo

    secret = "sk-live-secret-123"
    audit_id = repo.create_agent_audit(
        run_id="run-audit-1",
        user_id="analyst-1",
        workspace_id=17,
        query="Проверь контакт +7 999 123-45-67 и " + secret,
        tools_used=["audit_web_search", "audit_db_query"],
        duration_ms=321,
        result="Результат с токеном " + secret,
        status="partial",
        error_code="skill_failed",
        session=session,
    )

    row = session.execute(
        text("SELECT * FROM agent_audit_log WHERE audit_id = :id"),
        {"id": audit_id},
    ).mappings().one()
    serialized = json.dumps(dict(row), ensure_ascii=False)

    assert row["run_id"] == "run-audit-1"
    assert row["user_id"] == "analyst-1"
    assert row["duration_ms"] == 321
    assert json.loads(row["tools_used"]) == ["audit_web_search", "audit_db_query"]
    assert secret not in serialized
    assert "+7 999 123-45-67" not in serialized
    assert "[PHONE_" in serialized
    assert "raw prompt" not in serialized.lower()
    assert agent is not None


def test_sse_tool_events_never_expose_arguments_or_result_payload():
    """Безопасная SSE-маппинговка не требует технического audit hook."""
    from nanobot.sdk.types import (
        STREAM_EVENT_TOOL_COMPLETED,
        STREAM_EVENT_TOOL_STARTED,
        StreamEvent,
    )

    from bank_audit.loophole.chat.graph import _map_event

    started = _map_event(
        StreamEvent(
            type=STREAM_EVENT_TOOL_STARTED,
            name="audit_web_fetch",
            arguments={"url": "https://example.test", "api_key": "sk-secret"},
        ),
        object(),
    )
    completed = _map_event(
        StreamEvent(
            type=STREAM_EVENT_TOOL_COMPLETED,
            name="audit_web_fetch",
            metadata={"detail": "raw result with sk-secret"},
        ),
        object(),
    )

    assert started["data"] == {"name": "audit_web_fetch"}
    assert completed["data"] == {"name": "audit_web_fetch", "status": "completed"}
    assert "sk-secret" not in repr((started, completed))


@pytest.mark.asyncio
async def test_streamed_text_redacts_phone_and_secret_before_hook_and_sse():
    """Стримируемый delta не раскрывает ПДн и credential в hook или SSE."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    sensitive = "Контакт +7 912 345-67-89, api_key=sk-stream-secret"
    hook = AuditHook()

    await hook.on_stream(None, sensitive)
    mapped = _map_event(
        StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta=sensitive),
        hook,
    )
    assert mapped is None
    serialized = json.dumps(
        {"event": "token", "data": hook.flush_stream_for_sse()},
        ensure_ascii=False,
    )

    assert "+7 912 345-67-89" not in hook.final_answer
    assert "sk-stream-secret" not in hook.final_answer
    assert "+7 912 345-67-89" not in serialized
    assert "sk-stream-secret" not in serialized
    assert "[PHONE_" in hook.final_answer
    assert "[PHONE_" in serialized


@pytest.mark.asyncio
async def test_streamed_split_chunks_redact_phone_and_secret_before_sse():
    """Redaction сохраняется между delta boundaries и не отдаёт raw SSE."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    chunks = [
        "Контакт +7 912",
        " 345-67-89, api_key=sk-",
        "split-secret",
    ]
    hook = AuditHook()
    mapped = []
    for chunk in chunks:
        await hook.on_stream(None, chunk)
        mapped.append(_map_event(StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta=chunk), hook))
    mapped.append({"event": "token", "data": hook.flush_stream_for_sse()})

    serialized = json.dumps(mapped, ensure_ascii=False)
    assert "+7 912 345-67-89" not in serialized
    assert "sk-split-secret" not in serialized
    assert "+7 912" not in serialized
    assert "345-67-89" not in serialized
    assert "split-secret" not in serialized
    assert "[PHONE_" in serialized
    assert "[SECRET]" in serialized
    assert "[SECRET]" in serialized
    assert "+7 912 345-67-89" not in hook.final_answer
    assert "sk-split-secret" not in hook.final_answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "raw_value", "raw_prefix", "marker"),
    [
        (("Контакт +", "7 912 345-67-89"), "+7 912 345-67-89", "+", "[PHONE_"),
        (("credential api_", "key=split-prefix-secret"), "api_key=split-prefix-secret", "api_", "[SECRET]"),
    ],
)
async def test_streamed_sensitive_prefix_chunks_never_reach_sse(
    chunks,
    raw_value,
    raw_prefix,
    marker,
):
    """Потенциальный sensitive prefix удерживается до следующей delta."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    hook = AuditHook()
    mapped = []
    for chunk in chunks:
        await hook.on_stream(None, chunk)
        mapped.append(_map_event(StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta=chunk), hook))
    mapped.append({"event": "token", "data": hook.flush_stream_for_sse()})

    serialized = json.dumps(mapped, ensure_ascii=False)
    assert raw_value not in serialized
    assert raw_prefix not in serialized
    assert marker in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "raw_value", "raw_prefix", "marker"),
    [
        (("s", "k-secret"), "sk-secret", '"data": "s"', "[SECRET]"),
        (
            ("Контакт +7 912 345-", "67-89"),
            "+7 912 345-67-89",
            "+7 912",
            "[PHONE_",
        ),
        (
            ("Карта 4111 1111 ", "1111 1111"),
            "4111 1111 1111 1111",
            "4111 1111",
            "[CARD_",
        ),
        (("ИНН 123456", "789012"), "123456789012", "123456", "[INN_"),
    ],
)
async def test_streamed_redactor_holds_split_secret_and_pii_prefixes(
    chunks,
    raw_value,
    raw_prefix,
    marker,
):
    """Даже первый символ потенциального секрета или ПДн не попадает в SSE."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    hook = AuditHook()
    mapped = []
    for chunk in chunks:
        await hook.on_stream(None, chunk)
        mapped.append(_map_event(StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta=chunk), hook))
    mapped.append({"event": "token", "data": hook.flush_stream_for_sse()})

    serialized = json.dumps(mapped, ensure_ascii=False)
    assert raw_value not in serialized
    assert raw_prefix not in serialized
    assert marker in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "raw_value", "raw_prefix", "marker"),
    [
        (
            ("A", "uthorization=Bearer secret"),
            "Authorization=Bearer secret",
            '"data": "A"',
            "[SECRET]",
        ),
        (
            ("e", "yJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SIGNATURE"),
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SIGNATURE",
            '"data": "e"',
            "[SECRET]",
        ),
        (("Почта user@", "example"), "user@example", "user@", "[EMAIL]"),
    ],
)
async def test_streamed_redactor_holds_unknown_secret_and_email_prefixes(
    chunks,
    raw_value,
    raw_prefix,
    marker,
):
    """Неизвестные split-prefix и незавершённый email не попадают в SSE."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    hook = AuditHook()
    mapped = []
    for chunk in chunks:
        await hook.on_stream(None, chunk)
        mapped.append(_map_event(StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta=chunk), hook))
    mapped.append({"event": "token", "data": hook.flush_stream_for_sse()})

    serialized = json.dumps(mapped, ensure_ascii=False)
    assert raw_value not in serialized
    assert raw_prefix not in serialized
    assert marker in serialized


def test_streamed_text_delta_is_buffered_until_final_redaction():
    """Ни одна текстовая delta не публикуется до полного flush ответа."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    hook = AuditHook()
    first = _map_event(
        StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Обычный текст"),
        hook,
    )

    assert first is None
    assert hook.flush_stream_for_sse() == "Обычный текст"


def test_text_delta_without_buffer_hook_is_fail_closed():
    """Без текстового buffer hook SSE предпочитает безопасный empty."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event

    assert _map_event(
        StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="raw text"),
        object(),
    ) is None


@pytest.mark.asyncio
async def test_final_agent_content_redacts_phone_and_secret_before_sse_fallback():
    """Финальный content nanobot не возвращается в SSE без redaction."""
    from types import SimpleNamespace

    from bank_audit.loophole.chat.hooks import AuditHook

    sensitive = "Контакт +7 912 345-67-89, api_key=sk-final-secret"
    hook = AuditHook()

    await hook.after_run(SimpleNamespace(final_content=sensitive))

    assert "+7 912 345-67-89" not in hook.final_answer
    assert "sk-final-secret" not in hook.final_answer
    assert "[PHONE_" in hook.final_answer


@pytest.mark.asyncio
async def test_managed_agent_masks_unhooked_result_content_before_return():
    """Fallback result.content не должен обходить redaction hook-а."""
    agent = _agent_module()

    sensitive = "Контакт +7 912 345-67-89, api_key=sk-result-secret"

    class FakeBot:
        async def run(self, prompt, **kwargs):
            return SimpleNamespace(content=sensitive)

        async def aclose(self):
            return None

    managed = agent.ManagedAgent(
        agent.AgentRunContext("analyst-1", 17, "запрос", "run-content"),
        FakeBot(),
        "",
    )

    result = await managed.run(session=object())

    assert sensitive not in result.answer
    assert "+7 912 345-67-89" not in result.answer
    assert "sk-result-secret" not in result.answer
    assert "[PHONE_" in result.answer
    assert "[SECRET]" in result.answer


def test_ui_shows_tool_names_without_technical_payloads():
    source = Path("src/bank_audit/loophole/static/loophole.jsx").read_text(encoding="utf-8")

    assert "Использованные инструменты" in source
    assert "ev.args" not in source
    assert "ev.result" not in source


def test_ui_uses_server_side_clarification_tokens():
    """UI продолжает clarification только по server-side execution token."""
    source = Path("src/bank_audit/loophole/static/loophole.jsx").read_text(encoding="utf-8")

    assert "clarificationToken" in source
    assert "clarification_token" in source
    assert "execution_token" in source
    assert "clarify_token" in source
    assert "skip_clarify" not in source


def test_ui_restores_answer_without_requesting_a_rewrite_retry_challenge():
    """Typed error восстанавливает текущий ответ без ветви второго challenge."""
    source = Path("src/bank_audit/loophole/static/loophole.jsx").read_text(encoding="utf-8")

    assert 'd && d.reason === "answers_required"' not in source
    assert 'd && d.reason === "clarification_unavailable"' not in source
    assert "setPendingQuestions(questionsForRetry);" in source
    assert "setClarificationToken(clarificationTokenForRetry);" in source
    assert "setChatInput(inputForRetry);" in source


@pytest.mark.asyncio
async def test_client_skip_clarify_cannot_start_agent_for_ambiguous_query(monkeypatch):
    """Клиентский флаг не должен обходить server-side clarification gate."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import stream_chat

    async def incomplete(question, history=None):
        return {
            "complete": False,
            "questions": [{"id": "bank", "question": "Какой банк?"}],
        }

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)
    factory_called = False

    class ForbiddenFactory:
        def create(self, *args, **kwargs):
            nonlocal factory_called
            factory_called = True
            raise AssertionError("AgentFactory нельзя запускать до clarification")

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", ForbiddenFactory)

    events = [
        event
        async for event in stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "skip_clarify": True,
            }
        )
    ]

    assert factory_called is False
    assert any(event["data"].get("phase") == "await_clarify" for event in events)


@pytest.mark.asyncio
async def test_clarification_error_refuses_without_agent_or_iteration(monkeypatch):
    """Сбой проверки полноты не становится разрешением на запуск агента."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import run_chat

    monkeypatch.setenv("LOOPHOLE_ASKING_ENABLED", "1")

    class FailingCompletions:
        async def create(self, **kwargs):
            raise RuntimeError("raw provider payload")

    client = SimpleNamespace(chat=SimpleNamespace(completions=FailingCompletions()))
    monkeypatch.setattr(clarify_mod, "_client", lambda: client)

    class ForbiddenFactory:
        def create(self, *args, **kwargs):
            raise AssertionError("AgentFactory нельзя запускать при ошибке clarify")

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", ForbiddenFactory)

    result = await run_chat(
        {
            "query": "проверь вклад",
            "workspace_id": 17,
            "user_id": "analyst-1",
            "iterations": 0,
        }
    )

    assert result["phase"] == "await_clarify"
    assert result["iterations"] == 0


@pytest.mark.asyncio
async def test_enrichment_error_returns_typed_error_without_repeated_question(monkeypatch):
    """Сбой детерминированной сборки не повторяет уже отвеченный challenge."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import run_chat

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    async def broken(question, answers):
        raise RuntimeError("raw rewrite provider payload")

    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", broken)

    class ForbiddenFactory:
        def create(self, *args, **kwargs):
            raise AssertionError("AgentFactory нельзя создавать при сбое rewrite")

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", ForbiddenFactory)

    result = await run_chat(
        {
            "query": "проверь вклад",
            "workspace_id": 17,
            "user_id": "analyst-1",
            "iterations": 0,
            "clarify_answers": [{"question": "Банк?", "selected": ["Сбербанк"]}],
        }
    )

    assert result["phase"] == "error"
    assert result["error"] == "clarification_assembly_failed"
    assert result["iterations"] == 0
    assert "clarify_questions" not in result
    assert "clarification_token" not in result


@pytest.mark.asyncio
async def test_verified_run_chat_skips_clarification_gate(monkeypatch):
    """Проверенный execution token не запускает второй LLM-gate."""
    from bank_audit.loophole.agent import AgentResult
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat import graph as graph_mod

    async def forbidden_gate(question, history=None):
        raise AssertionError("повторный clarification gate запрещён")

    async def successful_agent(*args, **kwargs):
        return AgentResult(answer="Готово", run_id="verified-run")

    monkeypatch.setattr(clarify_mod, "generate_clarifications", forbidden_gate)
    monkeypatch.setattr(graph_mod, "_run_nanobot", successful_agent)
    monkeypatch.setattr(graph_mod, "_save_agent_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(graph_mod.repo, "add_chat_message", lambda *args, **kwargs: None)

    result = await graph_mod.run_chat(
        {
            "query": "проверь вклад",
            "workspace_id": 17,
            "user_id": "analyst-1",
            "clarification_verified": True,
        },
        session=object(),
    )

    assert result["phase"] == "done"
    assert result["answer"] == "Готово"


@pytest.mark.asyncio
async def test_assembly_exception_emits_stream_error_without_new_challenge(monkeypatch):
    """SSE после сбоя сборки возвращает error и не выдаёт новый вопрос."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import stream_chat

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    async def broken(question, answers):
        raise RuntimeError("raw rewrite provider payload")

    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", broken)
    factory_called = False

    class ForbiddenFactory:
        def create(self, *args, **kwargs):
            nonlocal factory_called
            factory_called = True
            raise AssertionError("AgentFactory нельзя запускать при retry clarification")

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", ForbiddenFactory)

    events = [
        event
        async for event in stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "iterations": 0,
                "clarify_answers": [{"question": "Банк?", "selected": ["Сбербанк"]}],
            }
        )
    ]

    assert factory_called is False
    assert any(
        event["data"].get("phase") == "error"
        and event["data"].get("error") == "clarification_assembly_failed"
        for event in events
    )
    question_events = [event for event in events if event.get("event") == "question"]
    assert question_events == []
    assert not any(event["data"].get("phase") == "execute" for event in events)


@pytest.mark.asyncio
async def test_stream_failure_emits_safe_partial_terminal_event_and_audit(session, monkeypatch):
    """Падение stream закрывает агент, даёт terminal event и пишет partial audit."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent
    from sqlalchemy import text

    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat.graph import stream_chat

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)
    state = {
        "query": "проверь вклад",
        "workspace_id": 17,
        "user_id": "analyst-1",
        "clarification_verified": True,
    }
    closed = False

    class ExplodingAgent:
        async def stream(self, prompt, *, hook):
            await hook.on_stream(None, "Частичный безопасный ответ")
            yield StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Частичный безопасный ответ")
            await hook.after_iteration(
                SimpleNamespace(
                    tool_calls=[],
                    tool_events=[
                        {
                            "name": "audit_web_fetch",
                            "status": "failed",
                            "error": "sk-stream-secret",
                        }
                    ],
                )
            )
            raise RuntimeError("sk-stream-secret raw provider payload")

        async def aclose(self):
            nonlocal closed
            closed = True

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return ExplodingAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)

    events = [event async for event in stream_chat(state, session=session)]
    serialized = json.dumps(events, ensure_ascii=False)
    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().one()

    assert closed is True
    assert any(
        event["event"] == "phase"
        and event["data"].get("phase") == "answer"
        and event["data"].get("partial") is True
        for event in events
    )
    assert "sk-stream-secret" not in serialized
    assert audit["status"] == "partial"
    assert "sk-stream-secret" not in json.dumps(dict(audit), ensure_ascii=False)


@pytest.mark.asyncio
async def test_stream_cancellation_persists_partial_audit(session, monkeypatch):
    """CancelledError не должен обходить сохранение partial audit."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent
    from sqlalchemy import text

    from bank_audit.loophole.chat.graph import stream_chat

    started = asyncio.Event()
    closed = False

    class HangingAgent:
        async def stream(self, prompt, *, hook):
            await hook.on_stream(None, "Частичный ответ до отмены")
            started.set()
            yield StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Частичный ответ до отмены")
            await asyncio.Event().wait()

        async def aclose(self):
            nonlocal closed
            closed = True

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return HangingAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)

    async def consume():
        return [
            event
            async for event in stream_chat(
                {
                    "query": "проверь вклад",
                    "workspace_id": 17,
                    "user_id": "analyst-1",
                    "clarification_verified": True,
                },
                session=session,
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().one()
    assert closed is True
    assert audit["status"] == "partial"
    assert audit["error_code"] == "agent_cancelled"
    assert "Частичный ответ до отмены" in audit["result_redacted"]


@pytest.mark.asyncio
async def test_run_chat_cancellation_persists_partial_audit(monkeypatch):
    """Отмена синхронного запуска не должна обходить partial audit."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat import graph as graph_mod

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    async def enrich(question, answers):
        return question

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    captured = {}

    def capture_audit(state, result, *, started_at, session):
        captured["state"] = state
        captured["result"] = result

    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", enrich)
    monkeypatch.setattr(graph_mod, "_run_nanobot", cancelled)
    monkeypatch.setattr(graph_mod, "_save_agent_audit", capture_audit)

    with pytest.raises(asyncio.CancelledError):
        await graph_mod.run_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "run_id": "run-cancel",
            },
            session=object(),
        )

    assert captured["result"].partial is True
    assert captured["result"].errors == ("agent_cancelled",)
    assert captured["result"].run_id == "run-cancel"


@pytest.mark.asyncio
async def test_run_chat_replaces_unsafe_run_id_before_audit_fallback(monkeypatch):
    """Небезопасный run_id из state не должен попасть в fallback-аудит."""
    from bank_audit.loophole.agent import AgentResult
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat import graph as graph_mod

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    async def enrich(question, answers):
        return question

    async def successful_agent(*args, **kwargs):
        return AgentResult(answer="Безопасный ответ", run_id=None)

    captured = {}

    def capture_audit(**kwargs):
        captured["run_id"] = kwargs["run_id"]

    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", enrich)
    monkeypatch.setattr(graph_mod, "_run_nanobot", successful_agent)
    monkeypatch.setattr(graph_mod.repo, "create_agent_audit", capture_audit)
    monkeypatch.setattr(graph_mod.repo, "add_chat_message", lambda *args, **kwargs: None)

    result = await graph_mod.run_chat(
        {
            "query": "проверь вклад",
            "workspace_id": 17,
            "user_id": "analyst-1",
            "run_id": "../../escape",
        },
        session=object(),
    )

    assert captured["run_id"] != "../../escape"
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", captured["run_id"])
    assert result["run_id"] == captured["run_id"]


@pytest.mark.asyncio
async def test_stream_chat_replaces_unsafe_run_id_before_audit_fallback(monkeypatch):
    """Fallback-аудит stream также получает только безопасный run_id."""
    from bank_audit.loophole.chat import graph as graph_mod

    class ExplodingFactory:
        def create(self, *args, **kwargs):
            raise RuntimeError("factory failure")

    captured = {}

    def capture_audit(state, result, *, started_at, session):
        captured["run_id"] = result.run_id

    monkeypatch.setattr(graph_mod, "AgentFactory", ExplodingFactory)
    monkeypatch.setattr(graph_mod, "_save_agent_audit", capture_audit)

    events = [
        event
        async for event in graph_mod.stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "clarification_verified": True,
                "run_id": "../../escape",
            },
            session=object(),
        )
    ]

    assert events
    assert captured["run_id"] != "../../escape"
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", captured["run_id"])


@pytest.mark.asyncio
async def test_run_chat_without_session_fails_before_factory(monkeypatch):
    """Отсутствие серверной сессии блокирует запуск до AgentFactory."""
    from bank_audit.loophole.chat import clarify as clarify_mod
    from bank_audit.loophole.chat import graph as graph_mod

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    async def enrich(question, answers):
        return question

    factory_called = False

    class ForbiddenFactory:
        def __init__(self):
            nonlocal factory_called
            factory_called = True

        def create(self, *args, **kwargs):
            raise AssertionError("AgentFactory не должен запускаться без session")

    monkeypatch.setattr(graph_mod, "AgentFactory", ForbiddenFactory)
    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", enrich)

    result = await graph_mod.run_chat(
        {
            "query": "проверь вклад",
            "workspace_id": 17,
            "user_id": "analyst-1",
        },
        session=None,
    )

    assert factory_called is False
    assert result["phase"] == "error"
    assert result["error"] == "session_unavailable"


@pytest.mark.asyncio
async def test_stream_chat_without_session_fails_before_factory(monkeypatch):
    """SSE без серверной сессии возвращает typed error до AgentFactory."""
    from bank_audit.loophole.chat import graph as graph_mod

    factory_called = False

    class ForbiddenFactory:
        def __init__(self):
            nonlocal factory_called
            factory_called = True

        def create(self, *args, **kwargs):
            raise AssertionError("AgentFactory не должен запускаться без session")

    monkeypatch.setattr(graph_mod, "AgentFactory", ForbiddenFactory)
    events = [
        event
        async for event in graph_mod.stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "clarification_verified": True,
            },
            session=None,
        )
    ]

    assert factory_called is False
    assert any(
        event["event"] == "phase"
        and event["data"].get("phase") == "error"
        and event["data"].get("error") == "session_unavailable"
        for event in events
    )
    assert not any(event["data"].get("phase") == "execute" for event in events)


@pytest.mark.asyncio
async def test_stream_close_persists_partial_audit(session, monkeypatch):
    """Явное закрытие async generator сохраняет уже собранный partial audit."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent
    from sqlalchemy import text

    from bank_audit.loophole.chat.graph import stream_chat

    closed = False

    class ClosingAgent:
        async def stream(self, prompt, *, hook):
            await hook.on_stream(None, "Ответ до закрытия stream")
            yield StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Ответ до закрытия stream")

        async def aclose(self):
            nonlocal closed
            closed = True

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return ClosingAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)
    stream = stream_chat(
        {
            "query": "проверь вклад",
            "workspace_id": 17,
            "user_id": "analyst-1",
            "clarification_verified": True,
        },
        session=session,
    )

    await stream.__anext__()
    await stream.__anext__()
    await stream.aclose()

    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().one()
    assert closed is True
    assert audit["status"] == "partial"
    assert audit["error_code"] == "agent_cancelled"
    assert "Ответ до закрытия stream" in audit["result_redacted"]


@pytest.mark.asyncio
async def test_stream_factory_error_emits_safe_terminal_event_and_audit(session, monkeypatch):
    """Исключение AgentFactory превращается в безопасный partial SSE и аудит."""
    from sqlalchemy import text

    from bank_audit.loophole.chat.graph import stream_chat

    class ExplodingFactory:
        def create(self, *args, **kwargs):
            raise RuntimeError("Bearer factory-secret raw payload")

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", ExplodingFactory)

    events = [
        event
        async for event in stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "clarification_verified": True,
            },
            session=session,
        )
    ]
    serialized = json.dumps(events, ensure_ascii=False)
    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().one()

    assert any(
        event["event"] == "phase"
        and event["data"].get("phase") == "answer"
        and event["data"].get("partial") is True
        for event in events
    )
    assert "factory-secret" not in serialized
    assert "Bearer" not in serialized
    assert audit["status"] == "partial"
    assert audit["error_code"] == "agent_error"


@pytest.mark.asyncio
async def test_provider_connection_error_emits_safe_retry_state_and_audit(session, monkeypatch):
    """Транспортный текст nanobot не попадает в SSE, чат или audit как готовый ответ."""
    from sqlalchemy import text

    from bank_audit.loophole.chat.graph import stream_chat

    class ProviderErrorAgent:
        async def stream(self, prompt, *, hook):
            await hook.after_run(
                SimpleNamespace(
                    final_content="Error calling LLM: Connection error.",
                    stop_reason="error",
                    tools_used=[],
                )
            )
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return ProviderErrorAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)
    events = [
        event
        async for event in stream_chat(
            {
                "query": "кредитные карты за август 2026 года",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "clarification_verified": True,
            },
            session=session,
        )
    ]
    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().one()
    serialized = json.dumps(events, ensure_ascii=False)

    terminal = next(
        event for event in events
        if event["event"] == "phase" and event["data"].get("phase") == "error"
    )
    assert terminal["data"]["error"] == "agent_unavailable"
    assert "повторите" in terminal["data"]["message"].lower()
    assert "Error calling LLM" not in serialized
    assert "Connection error" not in serialized
    assert audit["status"] == "partial"
    assert audit["error_code"] == "agent_error"
    assert "Connection error" not in (audit["result_redacted"] or "")


@pytest.mark.asyncio
async def test_stream_failure_events_mark_hook_and_audit_partial(session, monkeypatch):
    """Прямые nanobot tool/run failure events не дают completed audit."""
    from nanobot.sdk.types import (
        STREAM_EVENT_RUN_FAILED,
        STREAM_EVENT_TOOL_FAILED,
        StreamEvent,
    )
    from sqlalchemy import text

    from bank_audit.loophole.chat.graph import stream_chat

    class FailedEventsAgent:
        async def stream(self, prompt, *, hook):
            yield StreamEvent(
                type=STREAM_EVENT_TOOL_FAILED,
                name="audit_web_fetch",
                error="api_key=event-secret",
            )
            yield StreamEvent(
                type=STREAM_EVENT_RUN_FAILED,
                error="JWT event-secret",
            )

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return FailedEventsAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)
    events = [
        event
        async for event in stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "clarification_verified": True,
            },
            session=session,
        )
    ]
    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().one()
    serialized = json.dumps(events, ensure_ascii=False)

    assert any(
        event["event"] == "phase"
        and event["data"].get("phase") == "answer"
        and event["data"].get("partial") is True
        for event in events
    )
    assert audit["status"] == "partial"
    assert audit["error_code"] in {"skill_failed", "agent_error"}
    assert "event-secret" not in serialized


@pytest.mark.asyncio
async def test_stream_iteration_limit_is_partial_and_explained(session, monkeypatch):
    """Лимит итераций в stream даёт partial-ответ и redacted audit-статус."""
    from sqlalchemy import text

    from bank_audit.loophole.chat.graph import stream_chat

    class LimitedAgent:
        async def stream(self, prompt, *, hook):
            await hook.after_run(
                SimpleNamespace(
                    final_content="Найденный до лимита фрагмент",
                    stop_reason="max_iterations",
                    tools_used=["audit_web_fetch"],
                )
            )
            if False:
                yield None

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return LimitedAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)
    events = [
        event
        async for event in stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "clarification_verified": True,
            },
            session=session,
        )
    ]
    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().one()

    assert any(
        event["event"] == "phase"
        and event["data"].get("phase") == "answer"
        and event["data"].get("partial") is True
        for event in events
    )
    assert any(
        event["event"] == "token" and "лимит" in event["data"].lower()
        for event in events
    )
    assert audit["status"] == "partial"
    assert audit["error_code"] == "max_iterations"


@pytest.mark.asyncio
async def test_stream_sends_partial_explanation_after_text_delta(session, monkeypatch):
    """После уже отправленного text.delta объяснение partial не теряется в SSE."""
    from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, StreamEvent

    from bank_audit.loophole.chat.graph import stream_chat

    class LimitedAfterDeltaAgent:
        async def stream(self, prompt, *, hook):
            await hook.on_stream(None, "Найдено до остановки.")
            yield StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Найдено до остановки.")
            await hook.after_run(
                SimpleNamespace(
                    final_content="Найдено до остановки.",
                    stop_reason="max_iterations",
                    tools_used=["audit_web_fetch"],
                )
            )

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return LimitedAfterDeltaAgent()

    monkeypatch.setattr("bank_audit.loophole.chat.graph.AgentFactory", FakeFactory)
    events = [
        event
        async for event in stream_chat(
            {
                "query": "проверь вклад",
                "workspace_id": 17,
                "user_id": "analyst-1",
                "clarification_verified": True,
            },
            session=session,
        )
    ]

    token_index = next(
        index
        for index, event in enumerate(events)
        if event["event"] == "token" and "Найдено до остановки" in event["data"]
    )
    partial_index = next(index for index, event in enumerate(events) if event["event"] == "partial")
    assert partial_index > token_index
    assert "лимит" in events[partial_index]["data"]["message"].lower()


def test_ui_renders_safe_partial_explanation_event():
    """Клиент показывает понятное объяснение partial из безопасного SSE."""
    source = Path("src/bank_audit/loophole/static/loophole.jsx").read_text(encoding="utf-8")

    assert 'case "partial"' in source
    assert "payload.message" in source


def test_sse_unknown_tool_name_is_replaced_with_safe_label():
    """SSE не публикует произвольное имя инструмента от nanobot."""
    from nanobot.sdk.types import STREAM_EVENT_TOOL_STARTED, StreamEvent

    from bank_audit.loophole.chat.graph import _map_event
    from bank_audit.loophole.chat.hooks import AuditHook

    malicious = "../../secret?Authorization=Bearer raw-secret"
    mapped = _map_event(
        StreamEvent(type=STREAM_EVENT_TOOL_STARTED, name=malicious),
        AuditHook(),
    )

    assert malicious not in repr(mapped)
    assert mapped["data"]["name"] == "инструмент недоступен"


def test_db_query_requires_server_context_and_denies_before_database(monkeypatch):
    """DB tool без server-side user/workspace context не открывает сессию."""
    from bank_audit.loophole.chat import tools_nanobot

    def forbidden_session(*args, **kwargs):
        raise AssertionError("неавторизованный DB query не должен дойти до БД")

    monkeypatch.setattr(tools_nanobot.repo, "_session", forbidden_session)

    result = tools_nanobot.db_query(
        "SELECT workspace_id FROM loophole_workspace",
        context=None,
    )

    assert result["error"] == "db_query_unauthorized"


def test_db_query_rejects_cross_user_tool_context_before_query(session):
    """Прямой DB tool проверяет владельца workspace, а не только наличие context."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import tools_nanobot

    session.execute(
        text(
            "INSERT INTO loophole_workspace (workspace_id, user_id, name) "
            "VALUES (17, 'workspace-owner', 'private')"
        )
    )
    context = tools_nanobot.ToolContext(
        user_id="other-user",
        workspace_id=17,
        session=session,
    )

    result = tools_nanobot.db_query(
        "SELECT name FROM loophole_workspace",
        context=context,
    )

    assert result["error"] == "workspace_unauthorized"


def test_db_query_rejects_cross_workspace_before_database(monkeypatch):
    """DB tool не принимает workspace, отличный от server-side контекста."""
    from bank_audit.loophole.chat import tools_nanobot

    class ForbiddenSession:
        def execute(self, *args, **kwargs):
            raise AssertionError("cross-workspace query не должен дойти до БД")

    context = tools_nanobot.ToolContext(
        user_id="analyst-1",
        workspace_id=17,
        session=ForbiddenSession(),
    )
    result = tools_nanobot.db_query(
        "SELECT name FROM loophole_workspace WHERE workspace_id = 18",
        context=context,
    )

    assert result["error"] == "workspace_scope_denied"


def test_db_query_rejects_limit_over_500_before_database():
    """DB tool явно отклоняет LIMIT выше жёсткого предела."""
    from bank_audit.loophole.chat import tools_nanobot

    class ForbiddenSession:
        def execute(self, *args, **kwargs):
            raise AssertionError("LIMIT выше 500 не должен дойти до БД")

    context = tools_nanobot.ToolContext(
        user_id="analyst-1",
        workspace_id=17,
        session=ForbiddenSession(),
    )
    result = tools_nanobot.db_query(
        "SELECT name FROM loophole_workspace LIMIT 501",
        context=context,
    )

    assert result["error"] == "db_query_limit_exceeded"


@pytest.mark.parametrize("limit", ["-1", "+501"])
def test_db_query_rejects_signed_limit_bypass_before_database(limit):
    """Знаковый LIMIT не должен обходить жёсткий предел до обращения к БД."""
    from bank_audit.loophole.chat import tools_nanobot

    class ForbiddenSession:
        def execute(self, *args, **kwargs):
            raise AssertionError("знаковый LIMIT не должен дойти до БД")

    context = tools_nanobot.ToolContext(
        user_id="analyst-1",
        workspace_id=17,
        session=ForbiddenSession(),
    )

    result = tools_nanobot.db_query(
        f"SELECT name FROM loophole_workspace LIMIT {limit}",
        context=context,
    )

    assert result["error"] == "db_query_limit_exceeded"


@pytest.mark.asyncio
async def test_table_load_requires_server_context_and_denies_before_database(monkeypatch):
    """Table tool без доверенного user/workspace context не открывает БД."""
    from bank_audit.loophole.chat import tools_nanobot

    def forbidden_list_records(*args, **kwargs):
        raise AssertionError("неавторизованный table load не должен дойти до БД")

    monkeypatch.setattr(tools_nanobot.repo, "list_records", forbidden_list_records)

    result = json.loads(await tools_nanobot.AuditTableLoadTool().execute())

    assert result["error"] == "table_load_unauthorized"


@pytest.mark.asyncio
async def test_table_load_rejects_cross_user_tool_context_before_repository(session, monkeypatch):
    """Прямой table tool не читает записи чужого workspace."""
    from sqlalchemy import text

    from bank_audit.loophole.chat import tools_nanobot

    session.execute(
        text(
            "INSERT INTO loophole_workspace (workspace_id, user_id, name) "
            "VALUES (17, 'workspace-owner', 'private')"
        )
    )

    def forbidden_list_records(*args, **kwargs):
        raise AssertionError("чужой workspace не должен дойти до repository")

    monkeypatch.setattr(tools_nanobot.repo, "list_records", forbidden_list_records)
    context = tools_nanobot.ToolContext(
        user_id="other-user",
        workspace_id=17,
        session=session,
    )

    result = json.loads(await tools_nanobot.AuditTableLoadTool(context=context).execute())

    assert result["error"] == "workspace_unauthorized"


@pytest.mark.asyncio
async def test_table_load_injects_authorized_server_session(monkeypatch):
    """Разрешённый table tool получает server-side session, а не None."""
    from bank_audit.loophole.chat import tools_nanobot

    captured = {}
    server_session = object()

    def allowed_list_records(**kwargs):
        captured.update(kwargs)
        return [{"record_id": 17}]

    monkeypatch.setattr(tools_nanobot.repo, "list_records", allowed_list_records)
    monkeypatch.setattr(
        tools_nanobot.repo,
        "get_workspace",
        lambda workspace_id, *, session: {"workspace_id": workspace_id, "user_id": "analyst-1"},
    )
    context = tools_nanobot.ToolContext(
        user_id="analyst-1",
        workspace_id=17,
        session=server_session,
    )

    result = json.loads(await tools_nanobot.AuditTableLoadTool(context=context).execute())

    assert result == [{"record_id": 17}]
    assert captured["session"] is server_session


@pytest.mark.asyncio
async def test_table_load_rejects_limit_over_500_before_database(monkeypatch):
    """Table tool не передаёт в БД limit выше жёсткого предела."""
    from bank_audit.loophole.chat import tools_nanobot

    def forbidden_list_records(*args, **kwargs):
        raise AssertionError("LIMIT выше 500 не должен дойти до БД")

    monkeypatch.setattr(tools_nanobot.repo, "list_records", forbidden_list_records)
    monkeypatch.setattr(
        tools_nanobot.repo,
        "get_workspace",
        lambda workspace_id, *, session: {"workspace_id": workspace_id, "user_id": "analyst-1"},
    )
    context = tools_nanobot.ToolContext(
        user_id="analyst-1",
        workspace_id=17,
        session=object(),
    )

    result = json.loads(
        await tools_nanobot.AuditTableLoadTool(context=context).execute(limit=501)
    )

    assert result["error"] == "table_load_limit_exceeded"


@pytest.mark.asyncio
async def test_web_fetch_tool_redacts_sensitive_excerpt_before_llm(monkeypatch):
    """Результат web_fetch маскируется на границе возврата tool."""
    from bank_audit.loophole.chat import tools_nanobot

    sensitive = "Контакт +7 912 345-67-89, api_key=sk-tool-secret"
    page = SimpleNamespace(
        url="https://example.test",
        final_url="https://example.test",
        status=200,
        title="Источник",
        excerpt=sensitive,
        via="http",
    )

    monkeypatch.setattr(
        tools_nanobot.fetch_decorator,
        "fetch_and_parse",
        lambda *args, **kwargs: page,
    )
    result = json.loads(await tools_nanobot.AuditWebFetchTool().execute("https://example.test"))

    serialized = json.dumps(result, ensure_ascii=False)
    assert "+7 912 345-67-89" not in serialized
    assert "sk-tool-secret" not in serialized
    assert "[PHONE_" in serialized
    assert "[SECRET]" in serialized


@pytest.mark.asyncio
async def test_db_query_tool_redacts_rows_before_llm_and_scopes_limit(monkeypatch):
    """Результат db_query маскируется и получает server-side workspace predicate."""
    from bank_audit.loophole.chat import tools_nanobot

    sensitive = "Контакт +7 912 345-67-89, api_key=sk-row-secret"
    captured = {}

    monkeypatch.setattr(
        tools_nanobot.repo,
        "get_workspace",
        lambda workspace_id, *, session: {"workspace_id": workspace_id, "user_id": "analyst-1"},
    )

    class Result:
        def keys(self):
            return ["content"]

        def mappings(self):
            return self

        def all(self):
            return [{"content": sensitive}]

    class Session:
        def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return Result()

    context = tools_nanobot.ToolContext(
        user_id="analyst-1",
        workspace_id=17,
        session=Session(),
    )
    result = json.loads(
        await tools_nanobot.AuditDbQueryTool(context=context).execute(
            "SELECT content FROM loophole_chat_message"
        )
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert "+7 912 345-67-89" not in serialized
    assert "sk-row-secret" not in serialized
    assert "[PHONE_" in serialized
    assert "[SECRET]" in serialized
    assert "WHERE workspace_id = :_managed_workspace_id LIMIT 500" in captured["sql"]
    assert captured["params"] == {"_managed_workspace_id": 17}


def test_agent_audit_failure_is_observable(monkeypatch, session):
    """Ошибка обязательной записи audit не проглатывается как completed."""
    from bank_audit.loophole import repository as repo
    from bank_audit.loophole.agent import AgentResult
    from bank_audit.loophole.chat.graph import _save_agent_audit

    def broken(**kwargs):
        raise RuntimeError("database secret payload")

    monkeypatch.setattr(repo, "create_agent_audit", broken)

    with pytest.raises(RuntimeError):
        _save_agent_audit(
            {"query": "запрос", "user_id": "analyst-1", "workspace_id": 17},
            AgentResult(answer="ответ", run_id="run-audit-error"),
            started_at=0.0,
            session=session,
        )


def test_agent_audit_requires_server_session():
    """Запуск без server-side session не может тихо обойти обязательный аудит."""
    from bank_audit.loophole.agent import AgentResult
    from bank_audit.loophole.chat.graph import AgentAuditError, _save_agent_audit

    with pytest.raises(AgentAuditError, match="серверная сессия"):
        _save_agent_audit(
            {"query": "запрос", "user_id": "analyst-1", "workspace_id": 17},
            AgentResult(answer="ответ", run_id="run-audit-session"),
            started_at=0.0,
            session=None,
        )


def test_agent_audit_redacts_bearer_jwt_and_cloud_tokens(session):
    """Известные и неизвестные форматы credentials не сохраняются как raw."""
    from sqlalchemy import text

    from bank_audit.loophole import repository as repo

    secrets = (
        "bearer-secret-123",
        "equal-bearer-secret-123",
        "json-api-secret-456",
        "json-password-456",
        "json-secret-456",
        "json-token-456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-secret",
        "cloud-access-secret-456",
        "cloud-json-secret-456",
        "opaque-unknown-sensitive-value",
    )
    query = (
        "Authorization: Bearer bearer-secret-123; "
        "Authorization=Bearer equal-bearer-secret-123; "
        "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-secret; "
        "cloud_access_key=cloud-access-secret-456; "
        'cloud_api_key="cloud-json-secret-456"; '
        'json={"api_key":"json-api-secret-456","password":"json-password-456",'
        '"secret":"json-secret-456","token":"json-token-456"}; '
        'credentials="opaque-unknown-sensitive-value"'
    )
    audit_id = repo.create_agent_audit(
        run_id="run-redaction-2",
        user_id="analyst-1",
        workspace_id=17,
        query=query,
        tools_used=["audit_web_fetch"],
        duration_ms=12,
        result=query,
        status="completed",
        session=session,
    )
    row = session.execute(
        text("SELECT * FROM agent_audit_log WHERE audit_id = :id"),
        {"id": audit_id},
    ).mappings().one()
    serialized = json.dumps(dict(row), ensure_ascii=False)

    for value in secrets:
        assert value not in serialized


def test_migration_044_and_agent_audit_insert_contract_are_postgresql_safe():
    """Проверяет DDL PostgreSQL-типы, индексы и repository RETURNING contract."""
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations" / "044_loophole_agent_audit.sql").read_text(
        encoding="utf-8"
    )
    repository = (root / "src/bank_audit/loophole/repository.py").read_text(encoding="utf-8")

    assert "BIGSERIAL" in migration
    assert "JSONB NOT NULL" in migration
    assert "idx_agent_audit_run" in migration
    assert "idx_agent_audit_user" in migration
    assert re.search(r"RETURNING\s+audit_id", repository, re.IGNORECASE)
    assert "tools_used" in repository
    assert "query_redacted" in repository
    assert "result_redacted" in repository
    assert "raw_prompt" not in migration.lower()
    assert "CREATE TRIGGER" in migration.upper()
    assert "BEFORE UPDATE OR DELETE" in migration.upper()
    assert "REVOKE UPDATE" in migration.upper()
    assert "REVOKE DELETE" in migration.upper()
    assert re.search(r'json\.dumps\(names', repository)
