"""Проверяемые критерии наблюдаемого AI-исследования Story 2.4."""
from __future__ import annotations

import asyncio
from pathlib import Path

from bank_audit.loophole.chat.graph import stream_chat

STATIC = Path(__file__).resolve().parents[2] / "src" / "bank_audit" / "loophole" / "static"


def test_first_localized_research_status_arrives_before_fifteen_seconds(session):
    async def first_event():
        events = stream_chat(
            {
                "user_id": "analyst",
                "workspace_id": 1,
                "query": "проверь комиссии",
                "run_id": "story-2-4-first-status",
                "messages": [],
            },
            session=session,
        )
        try:
            return await asyncio.wait_for(anext(events), timeout=1)
        finally:
            await events.aclose()

    assert asyncio.run(first_event()) == {"event": "phase", "data": {"phase": "clarify"}}


def test_research_panel_uses_localized_phases_and_keeps_chat_state_outside_panel():
    jsx = (STATIC / "loophole.jsx").read_text(encoding="utf-8")
    css = (STATIC / "loophole.css").read_text(encoding="utf-8")

    for label in ("Уточнение", "Выполнение", "Ответ"):
        assert label in jsx
    assert "const [chat, setChat] = useState([]);" in jsx
    assert "const [chatInput, setChatInput] = useState(\"\");" in jsx
    assert "window.innerWidth >= 1100" in jsx
    assert "window.innerWidth < 1100" in jsx
    assert ".lp-chat-backdrop" in css
    assert "@media (max-width: 1099px)" in css
