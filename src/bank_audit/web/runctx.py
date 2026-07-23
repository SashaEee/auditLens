"""Контекст текущего разбора: кто спрашивает и ради чего.

Документы попадают в базу знаний глубоко внутри — из инструмента агента,
который вызывается из оркестратора, который запущен из потока ответа. Тащить
«кто пользователь» и «какой вопрос» через все эти сигнатуры значило бы править
шесть функций подряд ради одного поля журнала.

contextvars дают то же самое без правки сигнатур и корректно ведут себя с
asyncio: каждая задача видит своё значение, а не соседкино.
"""
from __future__ import annotations

import contextvars
import uuid

_origin: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "auditlens_origin", default={})


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def set_origin(*, kind: str, username: str | None = None,
               session_id: int | None = None, question: str | None = None,
               run_id: str | None = None) -> str:
    """Пометить текущий разбор. Возвращает run_id — по нему потом к записям
    происхождения привязывается сохранённый отчёт (в момент загрузки страниц
    отчёта ещё не существует, он сохраняется в конце потока)."""
    rid = run_id or new_run_id()
    _origin.set({"kind": kind, "username": username, "session_id": session_id,
                 "question": question, "run_id": rid})
    return rid


def current_origin() -> dict:
    o = _origin.get()
    return dict(o) if o else {"kind": "report"}


def current_run_id() -> str | None:
    return (_origin.get() or {}).get("run_id")
