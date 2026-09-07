"""Чтение истории и мягкое удаление исследований без изменения доказательств."""
from __future__ import annotations

import secrets

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from . import repository as repo


def save_interrupted_answer(workspace_id: int, content: str, *, session) -> None:
    """Сохраняет фрагмент вместе с audit; после SQL-ошибки начинает новую транзакцию."""
    try:
        repo.add_chat_message(workspace_id, "assistant", content, session=session)
        session.commit()
    except SQLAlchemyError:
        # PostgreSQL может уже отклонять SQL при Session.is_active == True.
        # Здоровую транзакцию отмены не откатываем: в ней находится audit агента.
        session.rollback()
        repo.add_chat_message(workspace_id, "assistant", content, session=session)
        session.commit()


def history_payload(workspace: dict, user_id: str, *, session) -> dict:
    """Общий и авторский просмотр используют одни сохранённые сообщения и отчёты."""
    workspace_id = workspace["workspace_id"]
    messages = repo.list_chat_history(workspace_id, session=session)
    # Аргументы инструментов не являются пользовательской перепиской.
    public_messages = [
        {key: row[key] for key in ("message_id", "role", "content", "created_at", "report_id")}
        for row in messages if row["role"] in {"user", "assistant"}
    ]
    reports = session.execute(text(
        "SELECT report_id, query_text AS query, result_text AS result, created_at "
        "FROM loophole_research_report WHERE workspace_id = :id "
        "ORDER BY created_at, report_id"
    ), {"id": workspace_id}).mappings().all()
    return {
        "workspace": workspace,
        "messages": public_messages,
        "reports": [dict(row) for row in reports],
        "read_only": workspace["user_id"] != user_id,
    }


def soft_delete(workspace_id: int, user_id: str, *, session) -> bool:
    """Отметка не затрагивает сообщения, отчёты и evidence snapshots."""
    result = session.execute(text(
        "UPDATE loophole_workspace SET deleted_at = CURRENT_TIMESTAMP "
        "WHERE workspace_id = :id AND user_id = :user_id AND deleted_at IS NULL"
    ), {"id": workspace_id, "user_id": user_id})
    return result.rowcount == 1


def share_token(workspace_id: int, user_id: str, *, session) -> str | None:
    """Атомарно выдаёт стабильную непрогнозируемую ссылку только автору."""
    return session.execute(text(
        "UPDATE loophole_workspace SET share_token = COALESCE(share_token, :token) "
        "WHERE workspace_id = :id AND user_id = :user_id AND deleted_at IS NULL "
        "RETURNING share_token"
    ), {
        "id": workspace_id, "user_id": user_id, "token": secrets.token_urlsafe(32),
    }).scalar_one_or_none()


def shared_workspace(token: str, *, session) -> dict | None:
    """Токен разрешает только поиск неудалённой области; авторизация — в API."""
    if len(token) != 43 or any(c not in
                             "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                             for c in token):
        return None
    workspace_id = session.execute(text(
        "SELECT workspace_id FROM loophole_workspace "
        "WHERE share_token = :token AND deleted_at IS NULL"
    ), {"token": token}).scalar_one_or_none()
    return repo.get_workspace(workspace_id, session=session) if workspace_id else None
