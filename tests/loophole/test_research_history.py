"""История исследований: сохранность, серверные права и восстановление диалога."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DataError, InternalError
from sqlalchemy.orm import Session

from bank_audit.loophole import repository as repo
from bank_audit.loophole import web
from bank_audit.loophole.chat import clarify as clarify_mod
from bank_audit.loophole.chat import graph
from bank_audit.loophole.research_cases import ResearchCaseService

from . import test_authorization as auth_fixtures

app_session = auth_fixtures.app_session
client = auth_fixtures.client


@pytest.fixture
def durable_client(app_session):
    """Каждый HTTP-запрос использует новую транзакцию, как production dependency."""
    def session_dependency():
        with Session(app_session.get_bind()) as request_session:
            yield request_session
            request_session.commit()

    app = FastAPI()
    app.include_router(web.router, prefix="/api/loophole")
    app.dependency_overrides[web.get_session] = session_dependency
    app.dependency_overrides[web.get_user_id] = lambda: "author"
    with TestClient(app) as test_client:
        yield test_client

API = "/api/loophole"
AUTHOR = {"X-Authentik-Username": "author"}
READER = {"X-Authentik-Username": "reader"}


def create(client, name=None):
    response = client.post(f"{API}/workspace", json={"name": name}, headers=AUTHOR)
    assert response.status_code == 200
    return response.json()["workspace_id"]


def share(client, wid):
    response = client.post(f"{API}/workspace/{wid}/share", headers=AUTHOR)
    assert response.status_code == 200
    return parse_qs(urlsplit(response.json()["share_url"]).query)["share"][0]


def test_history_title_order_and_user_isolation(client, app_session):
    older, newer = create(client), create(client, "Название автора")
    repo.create_workspace("reader", name="Чужой запрос", session=app_session)
    repo.add_chat_message(older, "user", "  Исследование   кредитных карт  ", session=app_session)
    app_session.execute(text(
        "UPDATE loophole_workspace SET last_active_at = '2001-01-01' WHERE workspace_id = :id"
    ), {"id": newer})
    rows = client.get(f"{API}/workspaces", headers=AUTHOR).json()["workspaces"]
    assert [row["workspace_id"] for row in rows] == [older, newer]
    assert rows[0]["name"] == "Исследование кредитных карт"
    assert rows[1]["name"] == "Название автора"


def test_history_returns_every_message_and_legacy_reports(client, app_session):
    wid = create(client)
    for i in range(205):
        repo.add_chat_message(wid, "user", f"Вопрос {i}", session=app_session)
    report_id = ResearchCaseService(app_session).save_report_result(
        workspace_id=wid, run_id="old-run", query="Запрос", result="Старый отчёт",
    )
    response = client.get(f"{API}/history/{wid}", headers=AUTHOR)
    assert response.status_code == 200
    data = response.json()
    assert len(data["messages"]) == 205
    assert [m["content"] for m in data["messages"]] == [f"Вопрос {i}" for i in range(205)]
    assert data["workspace"]["workspace_id"] == wid
    assert data["read_only"] is False
    assert data["reports"][0]["report_id"] == report_id
    assert data["reports"][0]["result"] == "Старый отчёт"


def test_share_is_explicit_stable_private_and_readonly(client, app_session):
    wid = create(client, "Общий отчёт")
    repo.add_chat_message(wid, "user", "Сохранённый вопрос", session=app_session)
    assert client.get(f"{API}/shared/{wid}", headers=READER).status_code == 404
    token = share(client, wid)
    assert len(token) >= 32
    assert share(client, wid) == token
    response = client.get(f"{API}/shared/{token}", headers=READER)
    assert response.status_code == 200
    assert response.json()["read_only"] is True
    assert response.json()["messages"][0]["content"] == "Сохранённый вопрос"
    assert client.get(f"{API}/shared/{token}", headers=AUTHOR).json()["read_only"] is False
    own = client.get(f"{API}/workspaces", headers=READER).json()["workspaces"]
    assert own == []
    assert "share_token" not in response.text
    assert token not in client.get(f"{API}/workspaces", headers=AUTHOR).text
    assert client.get(f"{API}/history/{wid}", headers=READER).status_code == 403


@pytest.mark.parametrize("method,path,body", [
    ("post", "/workspace/{wid}/share", None),
    ("delete", "/workspace/{wid}", None),
    ("post", "/chat", {"workspace_id": "wid", "message": "Чужая запись"}),
    ("post", "/clarify", {"workspace_id": "wid", "question": "Чужой вопрос"}),
    ("post", "/clarify/answer", {
        "workspace_id": "wid", "question": "Чужой вопрос", "answers": [],
        "clarification_token": "чужой-токен",
    }),
])
def test_shared_reader_cannot_mutate(client, method, path, body):
    wid = create(client)
    token = share(client, wid)
    assert client.get(f"{API}/shared/{token}", headers=READER).status_code == 200
    payload = {k: wid if value == "wid" else value for k, value in (body or {}).items()}
    response = client.request(
        method, API + path.format(wid=wid), json=payload, headers=READER,
    )
    assert response.status_code == 403
    assert client.get(f"{API}/history/{wid}", headers=AUTHOR).json()["messages"] == []


def test_soft_delete_preserves_database_and_revokes_every_access(client, app_session):
    wid = create(client)
    repo.add_chat_message(wid, "user", "Не удалять физически", session=app_session)
    report_id = ResearchCaseService(app_session).save_report_result(
        workspace_id=wid, run_id="retained", query="Запрос", result="Сохранённый отчёт",
    )
    token = share(client, wid)
    response = client.delete(f"{API}/workspace/{wid}", headers=AUTHOR)
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get(f"{API}/workspaces", headers=AUTHOR).json()["workspaces"] == []
    assert app_session.execute(text(
        "SELECT deleted_at FROM loophole_workspace WHERE workspace_id = :id"
    ), {"id": wid}).scalar_one() is not None
    assert len(repo.list_chat_history(wid, session=app_session)) == 1
    assert ResearchCaseService(app_session).get_report_result(report_id)["result"]
    for headers in (AUTHOR, READER):
        assert client.get(f"{API}/history/{wid}", headers=headers).status_code == 404
        assert client.get(f"{API}/shared/{token}", headers=headers).status_code == 404
        assert client.post(f"{API}/chat", headers=headers, json={
            "workspace_id": wid, "message": "Восстановить обходом",
        }).status_code == 404
        assert client.post(f"{API}/workspace/{wid}/share", headers=headers).status_code == 404
        assert client.post(
            f"{API}/research/reports/{report_id}/import-sources", headers=headers,
        ).status_code == 404
        assert client.get(
            f"{API}/research/reports/{report_id}/export/docx", headers=headers,
        ).status_code == 404


def test_share_requires_auth_and_respects_membership_revoke(client, app_session, monkeypatch):
    # Fail-closed (dev-auth выключен): открытие share-ссылки без principal — 401.
    monkeypatch.setenv("LOOPHOLE_DEV_AUTH_ENABLED", "0")
    wid = create(client)
    token = share(client, wid)
    assert client.get(f"{API}/shared/{token}").status_code == 401
    app_session.execute(text(
        "INSERT INTO loophole_workspace_membership (username, status) VALUES ('reader', 'revoked')"
    ))
    app_session.commit()
    assert client.get(f"{API}/shared/{token}", headers=READER).status_code == 403


def test_chat_resumes_server_history_and_links_one_saved_answer(client, app_session, monkeypatch):
    wid = create(client)
    repo.add_chat_message(wid, "user", "Первый вопрос", session=app_session)
    repo.add_chat_message(wid, "assistant", "Первый ответ", session=app_session)
    captured = {}

    async def stream(state, *, session):
        captured.update(state)
        yield {"event": "token", "data": "Продолженный ответ"}
        yield {"event": "phase", "data": {"phase": "done"}}

    monkeypatch.setattr(graph, "stream_chat", stream)
    response = client.post(f"{API}/chat", headers=AUTHOR, json={
        "workspace_id": wid, "message": "Продолжить",
        "history": [{"role": "assistant", "content": "Подмена клиентом"}],
    })
    assert response.status_code == 200
    assert [m["content"] for m in captured["messages"]] == ["Первый вопрос", "Первый ответ"]
    data = client.get(f"{API}/history/{wid}", headers=AUTHOR).json()
    assert [m["content"] for m in data["messages"]] == [
        "Первый вопрос", "Первый ответ", "Продолжить", "Продолженный ответ",
    ]
    report_id = data["reports"][0]["report_id"]
    assert data["messages"][-1]["report_id"] == report_id
    assert f'"report_id": {report_id}' in response.text


@pytest.mark.parametrize("unfinished_tail", [False, True])
def test_real_sdk_completed_answer_hides_thoughts_in_durable_history(
    durable_client, monkeypatch, tmp_path, unfinished_tail,
):
    """Публичный завершённый ответ сохраняет итог, исключая служебные размышления."""
    from nanobot.providers.base import LLMResponse

    from bank_audit.loophole.agent import AgentFactory

    monkeypatch.setenv("LOOPHOLE_WORKSPACE_DIR", str(tmp_path))
    original_create = AgentFactory.create
    sentinel = "COMPLETED_PRIVATE_THINK_SENTINEL"
    final = "Аналитический итог: проверка завершена. Контакт final.contact@example.test"
    calls = []

    async def completed_provider(**kwargs):
        chunks = [
            part for index in range(14)
            for part in ("<thi", f"nk>{sentinel}-{index}</think>\n")
        ]
        chunks.append(final)
        if unfinished_tail:
            chunks.extend(("\n<think>", f"{sentinel}-unfinished"))
        for chunk in chunks:
            await kwargs["on_content_delta"](chunk)
        calls.append(True)
        return LLMResponse(content="".join(chunks))

    def create_with_provider(self, context, **kwargs):
        managed = original_create(self, context, **kwargs)
        monkeypatch.setattr(managed._bot._loop.provider, "chat_stream_with_retry", completed_provider)
        return managed

    async def ready(*args, **kwargs):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(AgentFactory, "create", create_with_provider)
    monkeypatch.setattr(clarify_mod, "generate_clarifications", ready)
    wid = create(durable_client)
    response = durable_client.post(f"{API}/chat", json={
        "workspace_id": wid, "message": "Найди 1 лазейку по кредитным картам за 2026 год",
    })
    assert response.status_code == 200 and calls == [True]
    assert sentinel not in response.text and "<think>" not in response.text
    assert "final.contact@example.test" not in response.text
    data = durable_client.get(f"{API}/history/{wid}").json()
    answer = data["messages"][-1]["content"]
    assert "Аналитический итог: проверка завершена." in answer
    assert sentinel not in answer and "<think>" not in answer
    assert "final.contact@example.test" not in answer
    assert "Ответ прерван." not in answer
    assert len(data["reports"]) == 1
    assert data["reports"][0]["result"] == answer


@pytest.mark.parametrize("has_candidate", [False, True])
def test_real_sdk_timeout_keeps_only_safe_results_in_durable_history(
    durable_client, app_session, monkeypatch, tmp_path, has_candidate,
):
    """14 незавершённых think-блоков не становятся публичным частичным отчётом."""
    from bank_audit.loophole.agent import AGENT_TIME_BUDGET_MESSAGE, AgentFactory
    from bank_audit.loophole.run_budget import ResearchBudget
    from tests.loophole.test_story_2_2_research_cases import _create_research_schema

    _create_research_schema(app_session)
    app_session.commit()
    monkeypatch.setenv("LOOPHOLE_WORKSPACE_DIR", str(tmp_path))
    original_create = AgentFactory.create
    completed_chunks = []
    sentinel = "PRIVATE_UNFINISHED_THINK_SENTINEL"
    email = "candidate.contact@example.test"
    phone = "+7 (999) 123-45-67"
    raw_marker = "FULL_SOURCE_BODY_SENTINEL"

    def create_with_waiting_provider(self, context, **kwargs):
        context = replace(context, budget=ResearchBudget(timeout_seconds=1, requested_count=2))
        managed = original_create(self, context, **kwargs)

        async def waiting_provider(**kwargs):
            if has_candidate:
                url = "https://example.test/current-source"
                raw_text = f"Цитата о механизме обхода комиссии. {raw_marker} {email} {phone}"
                context.fetched_sources[url] = {
                    "url": url, "title": "Прочитанный источник",
                    "extracted_text": raw_text,
                    "published_at": "2026-03-01T10:00:00+03:00",
                }
                context.pending_records.append({
                    "title": "Допустимый AI-кандидат", "url": url,
                    "description": f"Описание механизма, контакт {email} {phone}",
                    "snippet": "Цитата о механизме обхода комиссии.",
                    "evidence_quote": "Цитата о механизме обхода комиссии.",
                    "raw_text": raw_text,
                    "is_loophole": True, "published_at": "2026-03-01T10:00:00+03:00",
                })
            for index in range(14):
                await kwargs["on_content_delta"](f"<think>{sentinel}-{index}</think>\n")
                completed_chunks.append(index)
            await asyncio.sleep(30)

        monkeypatch.setattr(managed._bot._loop.provider, "chat_stream_with_retry", waiting_provider)
        return managed

    async def ready(*args, **kwargs):
        return {"complete": True, "questions": []}

    monkeypatch.setattr(AgentFactory, "create", create_with_waiting_provider)
    monkeypatch.setattr(clarify_mod, "generate_clarifications", ready)
    wid = create(durable_client)
    response = durable_client.post(f"{API}/chat", json={
        "workspace_id": wid, "message": "Найди 2 лазейки по кредитным картам за 2026 год",
    })
    assert response.status_code == 200
    assert completed_chunks == list(range(14))
    assert sentinel not in response.text and "<think>" not in response.text
    assert email not in response.text and phone not in response.text
    assert raw_marker not in response.text and '"raw_text"' not in response.text
    assert "event: report" not in response.text
    assert '"stop_reason": "time_budget"' in response.text
    assert '"partial": true' in response.text
    data = durable_client.get(f"{API}/history/{wid}").json()
    assert len(data["messages"]) == 2
    assert data["reports"] == []
    answer = data["messages"][-1]["content"]
    assert data["messages"][-1]["report_id"] is None
    assert AGENT_TIME_BUDGET_MESSAGE in answer
    assert "Ответ прерван." not in answer
    assert sentinel not in answer and "<think>" not in answer and email not in answer
    assert phone not in answer and raw_marker not in answer
    if has_candidate:
        assert "event: records" in response.text
        assert "Допустимый AI-кандидат" in answer
        assert "Цитата о механизме обхода комиссии." in answer
        assert "https://example.test/current-source" in answer
    else:
        assert "не получено AI-кандидатов" in answer
        assert "event: records" not in response.text
    assert app_session.execute(text(
        "SELECT count(*) FROM loophole_research_candidate"
    )).scalar_one() == int(has_candidate)
    assert app_session.execute(text(
        "SELECT count(*) FROM loophole_research_source"
    )).scalar_one() == int(has_candidate)
    if has_candidate:
        source_text = app_session.execute(text(
            "SELECT extracted_text FROM loophole_research_source"
        )).scalar_one()
        assert raw_marker in source_text and email in source_text and phone in source_text


def test_question_persists_without_reusable_token(client, monkeypatch):
    wid = create(client)

    async def stream(state, *, session):
        yield {"event": "question", "data": {
            "questions": [{"id": "period", "question": "Какой период?"}],
            "clarification_token": "ONE_TIME_TOKEN",
        }}

    monkeypatch.setattr(graph, "stream_chat", stream)
    response = client.post(f"{API}/chat", headers=AUTHOR, json={
        "workspace_id": wid, "message": "Проверить карты",
    })
    assert response.status_code == 200
    data = client.get(f"{API}/history/{wid}", headers=AUTHOR).json()
    assert len(data["messages"]) == 2
    assert "Какой период?" in data["messages"][-1]["content"]
    assert "ONE_TIME_TOKEN" not in json.dumps(data)
    assert data["reports"] == []


def test_migration_062_is_additive():
    sql = (Path(__file__).resolve().parents[2] / "migrations"
           / "062_loophole_research_history.sql").read_text(encoding="utf-8").lower()
    for column in ("deleted_at", "share_token", "report_id"):
        assert f"add column if not exists {column}" in sql
    assert "delete from" not in sql
    assert "drop table" not in sql


def test_standalone_clarification_keeps_entire_conversation(durable_client, monkeypatch):
    async def generate(question, history=None):
        return {"complete": False, "questions": [{"id": "product", "question": "Продукт?"}]}

    async def stream(state, *, session):
        yield {"event": "token", "data": "Готовый ответ"}

    monkeypatch.setattr(clarify_mod, "generate_clarifications", generate)
    monkeypatch.setattr(graph, "stream_chat", stream)
    wid = create(durable_client)
    challenge = durable_client.post(f"{API}/clarify", json={
        "workspace_id": wid, "question": "Исходный запрос",
    }).json()
    prepared = durable_client.post(f"{API}/clarify/answer", json={
        "workspace_id": wid, "question": "Исходный запрос",
        "clarification_token": challenge["clarification_token"],
        "answers": [{"question": "Продукт?", "selected": ["Карты"]}],
    })
    assert prepared.status_code == 200
    data = prepared.json()
    response = durable_client.post(f"{API}/chat", json={
        "workspace_id": wid, "message": data["enriched_question"],
        "clarify_token": data["execution_token"],
    })
    assert response.status_code == 200
    history = durable_client.get(f"{API}/history/{wid}").json()["messages"]
    assert [m["content"] for m in history] == [
        "Исходный запрос", "Продукт?", "Карты", "Готовый ответ",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [True, False])
async def test_received_partial_survives_disconnect_or_stream_error(app_session, monkeypatch, disconnect):
    wid = repo.create_workspace("author", session=app_session)
    app_session.commit()

    async def stream(state, *, session):
        yield {"event": "token", "data": "Уже полученный фрагмент"}
        raise RuntimeError("provider private error")

    monkeypatch.setattr(graph, "stream_chat", stream)
    with Session(app_session.get_bind()) as request_session:
        response = await web.chat(
            web.ChatRequest(workspace_id=wid, message="Запрос"),
            Request({"type": "http"}), user_id="author", session=request_session,
        )
        await anext(response.body_iterator)
        if disconnect:
            await response.body_iterator.aclose()
        else:
            with pytest.raises(RuntimeError):
                await anext(response.body_iterator)
    with Session(app_session.get_bind()) as reader_session:
        messages = repo.list_chat_history(wid, session=reader_session)
        assert len(messages) == 2
        assert "Уже полученный фрагмент" in messages[-1]["content"]
        assert "прерван" in messages[-1]["content"].lower()
        assert "private error" not in messages[-1]["content"]
        assert messages[-1]["report_id"] is None
        assert reader_session.execute(text("SELECT COUNT(*) FROM loophole_research_report")).scalar() == 0


@pytest.mark.asyncio
async def test_partial_retries_after_aborted_postgres_transaction(app_session, monkeypatch):
    """SQL-ошибка psycopg не обязана менять Session.is_active на False."""
    wid = repo.create_workspace("author", session=app_session)
    app_session.commit()

    async def stream(state, *, session):
        yield {"event": "token", "data": "Полученный ответ"}

    monkeypatch.setattr(graph, "stream_chat", stream)
    with Session(app_session.get_bind()) as request_session:
        original_execute = request_session.execute
        original_rollback = request_session.rollback
        aborted = False

        def fail_report(*args, **kwargs):
            nonlocal aborted
            aborted = True
            assert request_session.is_active is True
            raise DataError("SELECT 1/0", {}, RuntimeError("division by zero"))

        def execute(*args, **kwargs):
            if aborted:
                raise InternalError("INSERT", {}, RuntimeError("transaction aborted"))
            return original_execute(*args, **kwargs)

        def rollback():
            nonlocal aborted
            aborted = False
            original_rollback()

        monkeypatch.setattr(ResearchCaseService, "save_report_result", fail_report)
        monkeypatch.setattr(request_session, "execute", execute)
        monkeypatch.setattr(request_session, "rollback", rollback)
        response = await web.chat(
            web.ChatRequest(workspace_id=wid, message="Запрос"),
            Request({"type": "http"}), user_id="author", session=request_session,
        )
        await anext(response.body_iterator)
        # Ошибка сохранения отчёта не роняет SSE-ответ: генератор завершается
        # штатно, а текст сохраняется в историю после восстановления транзакции.
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
    with Session(app_session.get_bind()) as reader_session:
        messages = repo.list_chat_history(wid, session=reader_session)
        assert len(messages) == 2
        assert "Полученный ответ" in messages[-1]["content"]
        assert messages[-1]["report_id"] is None
