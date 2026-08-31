"""Интеграционные regression-тесты managed-agent маршрута loophole."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nanobot.sdk.types import STREAM_EVENT_TEXT_DELTA, STREAM_EVENT_TOOL_FAILED, StreamEvent
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit.loophole import repository as repo
from bank_audit.loophole.chat import clarify as clarify_mod
from bank_audit.loophole.chat import graph as chat_graph
from bank_audit.loophole.web import get_session, get_user_id, router

from .conftest import SCHEMA_SQL


@pytest.fixture
def route_context():
    """TestClient с настоящим маршрутом, graph, hook и SQLite audit log."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as connection:
        connection.connection.executescript(SCHEMA_SQL)
        connection.commit()
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()

    def override_session():
        yield session

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_user_id] = lambda: "test-user"
    with TestClient(app) as client:
        yield client, session
    session.close()


def _sse_question_token(body: str) -> str | None:
    """Достаёт непрозрачный server-side token из безопасного SSE payload."""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("clarification_token"):
            return payload["clarification_token"]
    return None


def test_chat_route_does_not_trust_client_skip_clarify(route_context, monkeypatch):
    """skip_clarify не запускает AgentFactory для неоднозначного запроса."""
    client, _session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]

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
            raise AssertionError("клиентский skip_clarify не должен запускать агента")

    monkeypatch.setattr(chat_graph, "AgentFactory", ForbiddenFactory)
    response = client.post(
        "/api/loophole/chat",
        json={
            "workspace_id": workspace_id,
            "message": "проверь вклад",
            "skip_clarify": True,
        },
    )

    assert response.status_code == 200
    assert factory_called is False
    assert "await_clarify" in response.text


def test_clarify_route_fail_closed_without_questions_still_returns_challenge(
    route_context,
    monkeypatch,
):
    """Короткий/неполный ответ LLM получает безопасный вопрос и server-side token."""
    client, _session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]

    async def incomplete(question, history=None):
        return {"complete": False, "questions": [], "reason": "query_too_short"}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)

    response = client.post(
        "/api/loophole/clarify",
        json={"question": "я", "workspace_id": workspace_id},
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["questions"]
    assert payload["clarification_token"]
    assert payload["questions"][0]["question"]


def test_chat_route_keeps_workspace_ownership_before_agent(route_context, monkeypatch):
    """Чужой workspace отклоняется до создания AgentFactory."""
    client, session = route_context
    foreign_workspace = repo.create_workspace("another-user", "чужой", session=session)
    factory_called = False

    class ForbiddenFactory:
        def create(self, *args, **kwargs):
            nonlocal factory_called
            factory_called = True
            raise AssertionError("агент нельзя создавать для чужого workspace")

    monkeypatch.setattr(chat_graph, "AgentFactory", ForbiddenFactory)
    response = client.post(
        "/api/loophole/chat",
        json={
            "workspace_id": foreign_workspace,
            "message": "проверь вклад",
            "skip_clarify": True,
        },
    )

    assert response.status_code == 403
    assert factory_called is False


def test_chat_route_legal_clarification_flow_has_safe_sse_and_audit(
    route_context,
    monkeypatch,
):
    """Легальный clarify flow проходит graph+hook+repository и пишет audit."""
    client, session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]

    async def incomplete(question, history=None):
        return {
            "complete": False,
            "questions": [{"id": "bank", "question": "Какой банк?"}],
        }

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)
    first = client.post(
        "/api/loophole/chat",
        json={"workspace_id": workspace_id, "message": "проверь вклад"},
    )
    challenge_token = _sse_question_token(first.text)
    assert first.status_code == 200
    assert challenge_token

    async def enrich(question, answers):
        return "проверь вклад: Сбербанк"

    monkeypatch.setattr(clarify_mod, "build_enriched_question", enrich)
    answer = client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": "проверь вклад",
            "answers": [{"question": "Какой банк?", "selected": ["Сбербанк"]}],
            "clarification_token": challenge_token,
        },
    )
    execution_token = answer.json().get("execution_token")
    assert answer.status_code == 200
    assert execution_token

    closed = False

    class FakeAgent:
        async def stream(self, prompt, *, hook):
            await hook.on_stream(None, "Безопасный частичный ответ")
            yield StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Безопасный частичный ответ")
            await hook.after_iteration(
                SimpleNamespace(
                    tool_calls=[],
                    tool_events=[
                        {
                            "name": "audit_web_fetch",
                            "status": "failed",
                            "error": "sk-route-secret",
                        }
                    ],
                )
            )
            yield StreamEvent(type=STREAM_EVENT_TOOL_FAILED, name="audit_web_fetch")

        async def aclose(self):
            nonlocal closed
            closed = True

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return FakeAgent()

    monkeypatch.setattr(chat_graph, "AgentFactory", FakeFactory)
    second = client.post(
        "/api/loophole/chat",
        json={
            "workspace_id": workspace_id,
            "message": "проверь вклад: Сбербанк",
            "clarify_token": execution_token,
        },
    )
    audit = session.execute(text("SELECT * FROM agent_audit_log")).mappings().all()

    assert second.status_code == 200
    assert closed is True
    assert "sk-route-secret" not in second.text
    assert '"arguments"' not in second.text
    assert '"status": "failed"' in second.text
    assert audit
    assert audit[-1]["user_id"] == "test-user"
    assert audit[-1]["workspace_id"] == workspace_id
    assert audit[-1]["status"] == "partial"
    history = client.get(f"/api/loophole/history/{workspace_id}").json()["messages"]
    assert [message["content"] for message in history if message["role"] == "user"] == [
        "проверь вклад",
        "Сбербанк",
    ]


def test_clarify_answer_uses_real_builder_token_and_history(
    route_context,
    monkeypatch,
):
    """Route передаёт реальные answers в детерминированную сборку без LLM-mock."""
    client, _session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]

    async def incomplete(question, history=None):
        return {
            "complete": False,
            "questions": [
                {"id": "bank", "question": "Какой банк?", "type": "text"}
            ],
        }

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)
    first = client.post(
        "/api/loophole/chat",
        json={"workspace_id": workspace_id, "message": "проверь вклад"},
    )
    challenge_token = _sse_question_token(first.text)
    assert challenge_token

    answer = client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": "проверь вклад",
            "answers": [{"question": "Какой банк?", "selected": ["Сбербанк"]}],
            "clarification_token": challenge_token,
        },
    )

    expected = "проверь вклад (уточнения — Какой банк: Сбербанк)"
    payload = answer.json()
    assert answer.status_code == 200
    assert payload["enriched_question"] == expected
    assert payload["answer_message"] == "Сбербанк"
    assert clarify_mod.consume_execution_token(
        payload["execution_token"],
        user_id="test-user",
        workspace_id=workspace_id,
        query=expected,
    )
    history = client.get(f"/api/loophole/history/{workspace_id}").json()["messages"]
    assert [message["content"] for message in history if message["role"] == "user"] == [
        "проверь вклад",
        "Сбербанк",
    ]


def test_clarify_answer_internal_failure_does_not_repeat_answered_question(
    route_context,
    monkeypatch,
):
    """Внутренняя ошибка сборки завершается typed error без нового challenge."""
    client, _session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]

    async def incomplete(question, history=None):
        return {
            "complete": False,
            "questions": [{"id": "bank", "question": "Какой банк?"}],
        }

    async def broken(question, answers):
        raise RuntimeError("raw rewrite provider payload")

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", broken)
    challenge = client.post(
        "/api/loophole/clarify",
        json={"question": "проверь вклад", "workspace_id": workspace_id},
    ).json()["clarification_token"]

    response = client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": "проверь вклад",
            "answers": [{"question": "банк?", "selected": ["Сбербанк"]}],
            "clarification_token": challenge,
        },
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == {
        "code": "clarification_assembly_failed",
        "message": "Не удалось подготовить исследование. Повторите отправку ответа.",
    }
    assert "questions" not in response.json()
    assert "clarification_token" not in response.json()

    async def recovered(question, answers):
        return "проверь вклад (банк: Сбербанк)"

    monkeypatch.setattr(clarify_mod, "build_enriched_question", recovered)
    retry = client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": "проверь вклад",
            "answers": [{"question": "банк?", "selected": ["Сбербанк"]}],
            "clarification_token": challenge,
        },
    )
    assert retry.status_code == 200
    assert retry.json()["execution_token"]


def test_chat_route_rejects_invalid_execution_token_before_sse_and_history(
    route_context,
    monkeypatch,
):
    """Поддельный execution token не запускает graph и не создаёт user-message."""
    client, session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]
    graph_called = False

    async def forbidden_stream(*args, **kwargs):
        nonlocal graph_called
        graph_called = True
        if False:
            yield None

    monkeypatch.setattr(chat_graph, "stream_chat", forbidden_stream)

    response = client.post(
        "/api/loophole/chat",
        json={
            "workspace_id": workspace_id,
            "message": "внутренний enriched-запрос",
            "clarify_token": "invalid-execution-token",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Недействительный execution token"
    assert graph_called is False
    history = client.get(f"/api/loophole/history/{workspace_id}").json()["messages"]
    assert history == []
    rejected = session.execute(
        text(
            "SELECT user_id, workspace_id, action, detail "
            "FROM loophole_action_log ORDER BY log_id DESC LIMIT 1"
        )
    ).mappings().one()
    assert rejected["user_id"] == "test-user"
    assert rejected["workspace_id"] == workspace_id
    assert rejected["action"] == "chat_rejected"
    assert json.loads(rejected["detail"]) == {"reason": "invalid_execution_token"}


def test_clarify_answer_rejects_empty_answers_and_keeps_challenge(
    route_context,
    monkeypatch,
):
    """Пустой ответ остаётся в clarification и не сжигает server-side challenge."""
    client, _session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]

    async def incomplete(question, history=None):
        return {
            "complete": False,
            "questions": [{"id": "bank", "question": "Какой банк?"}],
        }

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)
    challenge = client.post(
        "/api/loophole/clarify",
        json={"question": "проверь вклад", "workspace_id": workspace_id},
    ).json()["clarification_token"]

    empty = client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": "проверь вклад",
            "answers": [],
            "clarification_token": challenge,
        },
    )

    assert empty.status_code == 200
    assert empty.json()["complete"] is False
    assert empty.json()["reason"] == "answers_required"
    assert empty.json().get("execution_token") is None

    async def enrich(question, answers):
        return "проверь вклад: Сбербанк"

    monkeypatch.setattr(clarify_mod, "build_enriched_question", enrich)
    valid = client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": "проверь вклад",
            "answers": [{"question": "Какой банк?", "selected": ["Сбербанк"]}],
            "clarification_token": challenge,
        },
    )

    assert valid.status_code == 200
    assert valid.json().get("execution_token")


def test_chat_and_clarify_action_audit_redacts_sensitive_text(route_context, monkeypatch):
    """В action log не попадают ПДн, Bearer, JWT и api_key из chat/clarify."""
    client, session = route_context
    workspace_id = client.post(
        "/api/loophole/workspace", json={"name": "исследование"}
    ).json()["workspace_id"]
    sensitive = (
        "Контакт +7 999 123-45-67; Authorization=Bearer action-bearer-secret; "
        "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.action-jwt-secret; "
        'json={"api_key":"action-api-secret"}'
    )

    async def incomplete(question, history=None):
        return {
            "complete": False,
            "questions": [{"id": "bank", "question": "Какой банк?"}],
        }

    async def enriched(question, answers):
        return "безопасный обогащённый запрос"

    monkeypatch.setattr(clarify_mod, "generate_clarifications", incomplete)
    monkeypatch.setattr(clarify_mod, "build_enriched_question", enriched)
    challenge = client.post(
        "/api/loophole/clarify",
        json={"question": sensitive, "workspace_id": workspace_id},
    ).json()["clarification_token"]
    client.post(
        "/api/loophole/clarify/answer",
        json={
            "workspace_id": workspace_id,
            "question": sensitive,
            "answers": [],
            "clarification_token": challenge,
        },
    )

    async def complete(question, history=None):
        return {"complete": True, "questions": []}

    class SafeAgent:
        async def stream(self, prompt, *, hook):
            yield StreamEvent(type=STREAM_EVENT_TEXT_DELTA, delta="Готово")

        async def aclose(self):
            return None

    class FakeFactory:
        def create(self, context, *, llm=None, session=None):
            return SafeAgent()

    monkeypatch.setattr(clarify_mod, "generate_clarifications", complete)
    monkeypatch.setattr(chat_graph, "AgentFactory", FakeFactory)
    response = client.post(
        "/api/loophole/chat",
        json={"workspace_id": workspace_id, "message": sensitive},
    )

    assert response.status_code == 200
    actions = repo.list_actions("test-user", session=session)
    serialized = json.dumps(actions, ensure_ascii=False)
    for raw in (
        "+7 999 123-45-67",
        "action-bearer-secret",
        "action-jwt-secret",
        "action-api-secret",
    ):
        assert raw not in serialized
    assert any(action["action"] == "chat" for action in actions)
    assert any(action["action"] == "clarify" for action in actions)
    assert any(action["action"] == "clarify_answer" for action in actions)
