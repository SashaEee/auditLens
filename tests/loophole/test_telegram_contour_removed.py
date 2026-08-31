"""Регрессия удаления неиспользуемого Telegram-контура «Лазеек»."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bank_audit.loophole import web
from bank_audit.loophole.web import get_user_id, router


def test_legacy_telegram_endpoint_is_not_registered(monkeypatch):
    """Старый endpoint не раскрывает состояние исторических таблиц."""
    def fake_session():
        yield object()

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_user_id] = lambda: "test-user"
    app.dependency_overrides[web.get_session] = fake_session

    with TestClient(app) as client:
        response = client.get("/api/loophole/admin/telegram-targets")

    assert response.status_code == 404


def test_telegram_runtime_boundaries_are_absent():
    modules = (
        "telegram_targets",
        "target_access",
        "telegram_ingestion",
        "telegram_worker",
        "telegram_worker_sqlite",
        "telegram_perimeter",
    )

    for module in modules:
        assert importlib.util.find_spec(f"bank_audit.loophole.{module}") is None


def test_loophole_ui_has_no_telegram_actions_or_links():
    jsx = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "bank_audit"
        / "loophole"
        / "static"
        / "loophole.jsx"
    ).read_text(encoding="utf-8").lower()

    assert "telegram" not in jsx
    assert "t.me" not in jsx


def test_managed_agent_prompt_has_no_telegram_source_guidance():
    prompt = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "bank_audit"
        / "loophole"
        / "chat"
        / "prompt"
        / "07_nanobot_system.md"
    ).read_text(encoding="utf-8").lower()

    assert "telegram" not in prompt
