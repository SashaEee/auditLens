"""Состояние графа чата loophole (TypedDict).

Поля фаз ReAct-архитектуры добавлены поверх исходных полей retrieve→llm→tools.
"""
from __future__ import annotations

from typing import Any, TypedDict


class ChatState(TypedDict, total=False):
    # ── Исходные поля ────────────────────────────────────────────────────────
    messages: list[dict]           # история сообщений [{role, content, tool_name?, tool_args?}]
    query: str                     # текущий запрос пользователя
    bank_slugs: list[str]          # пул банков
    workspace_id: int | None
    user_id: str | None
    session: Any | None             # опциональная SQLAlchemy-сессия
    records: list[dict]            # найденные лазейки (retrieve)
    tool_calls: list[dict]         # запрошенные tool-calls
    tool_results: list[dict]       # результаты tools
    answer: str                    # финальный ответ LLM
    error: str | None

    # ── Поля ReAct-фаз ───────────────────────────────────────────────────────
    phase: str                     # clarify|await_clarify|plan|execute|aggregate|answer|done
    task_id: int | None            # id агентной задачи в БД (loophole_agent_task)
    subtasks: list[dict]           # план подзадач [{id, title, algorithm, tools, ...}]
    subtask_results: list[dict]    # результаты исполнения подзадач
    iterations: int                # счётчик итераций execute
    clarify_questions: list[dict]  # вопросы воронки clarify
    clarify_answers: list[dict]    # ответы пользователя на clarify
    pending_table_records: list[dict]  # записи для таблицы фронта (после aggregate)
