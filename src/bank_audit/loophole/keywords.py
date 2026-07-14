"""Управление ключевыми словами модуля loophole: seed, activate/deactivate, refine.

Seed — стартовые ключевые слова из config.SEED_KEYWORDS. Refine — через LLM
(см. refine.py), здесь только интерфейс сохранения уточнённых слов.
"""
from __future__ import annotations

import logging

from . import repository as repo
from .config import SEED_KEYWORDS

log = logging.getLogger(__name__)


def seed_keywords(*, session=None) -> int:
    """Вносит стартовые ключевые слова (категория 'seed'). Идемпотентно (дедуп в repo).

    Возвращает количество добавленных (новых) слов.
    """
    added = 0
    for kw, source in SEED_KEYWORDS:
        # Проверяем, существует ли уже — add_keyword дедуплит, но не отличает new/existing.
        existing = repo.list_keywords(session=session)
        if any(k["keyword"] == kw for k in existing):
            continue
        repo.add_keyword(kw, category="seed", source=source, session=session)
        added += 1
    return added


def activate(keyword_id: int, *, session=None) -> None:
    repo.set_keyword_active(keyword_id, True, session=session)


def deactivate(keyword_id: int, *, session=None) -> None:
    repo.set_keyword_active(keyword_id, False, session=session)


def add_manual(keyword: str, *, source: str = "manual", weight: float = 1.0,
               session=None) -> int | None:
    return repo.add_keyword(
        keyword, category="manual", source=source, weight=weight, session=session
    )


def add_refined(keyword: str, *, source: str = "auto", weight: float = 1.0,
                session=None) -> int | None:
    return repo.add_keyword(
        keyword, category="refined", source=source, weight=weight, session=session
    )


def active_keywords(*, session=None) -> list[str]:
    """Список активных ключевых слов (только текст)."""
    return [k["keyword"] for k in repo.list_keywords(only_active=True, session=session)]
