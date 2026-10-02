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


def test_research_progress_is_localized_and_chat_state_lives_in_the_tab():
    """Ход исследования — шаги по-русски; переписка — состояние вкладки, без боковой панели."""
    jsx = (STATIC / "loophole.jsx").read_text(encoding="utf-8")
    css = (STATIC / "loophole.css").read_text(encoding="utf-8")

    for label in ("Уточнение запроса", "Поиск источников", "Чтение страниц",
                  "Разметка материалов", "Итог", "Исследование идёт", "Исследование прервано"):
        assert label in jsx
    assert "const [chat, setChat] = useState([]);" in jsx
    assert "const [chatInput, setChatInput] = useState(\"\");" in jsx
    # Исследование — одна колонка на любой ширине: off-canvas чата больше нет.
    assert "window.innerWidth >= 1100" not in jsx
    assert ".lp-chat-backdrop" not in css
