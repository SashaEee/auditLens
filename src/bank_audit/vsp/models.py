"""Единая модель данных сборщика: отделение в источнике, отзыв, страница выдачи.

Модель не зависит от источника: адаптеры (collectors/*) переводят ответы 2ГИС,
Яндекс Карт и Домклика в эти структуры, а хранилище (storage.py) работает только с
ними. Персональных данных — минимум: автор хранится как необратимая ссылка
(author_ref), имя в открытом виде не пишется.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def author_ref(source: str, raw_author_id: str | None, salt: str) -> str | None:
    """Ссылка на автора без персональных данных: хэш (источник, id автора, соль).
    Позволяет видеть «один автор — много отзывов» внутри источника, но не
    восстанавливает имя или профиль. Соль задаётся в настройках и не хранится в БД."""
    if not raw_author_id:
        return None
    h = hashlib.sha256(f"{salt}|{source}|{raw_author_id}".encode()).hexdigest()
    return h[:24]


@dataclass
class SourceBranch:
    """Отделение, как его видит источник (карточка организации)."""
    source: str
    source_branch_id: str
    name: str | None = None
    address: str | None = None
    city: str | None = None
    region: str | None = None
    lat: float | None = None
    lon: float | None = None
    url: str | None = None
    declared_reviews: int | None = None      # сколько отзывов заявляет источник
    declared_rating: float | None = None
    status: str | None = None                # open | closed | moved | unknown
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Review:
    """Отзыв в нормализованном виде (до привязки к отделению реестра)."""
    source: str
    source_review_id: str
    source_branch_id: str
    rating: float | None
    review_text: str | None
    review_date: datetime | None
    author_ref: str | None = None
    organization_reply: str | None = None
    reply_date: datetime | None = None
    source_url: str | None = None
    collection_method: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def review_id(self) -> str:
        """Устойчивый id отзыва во всей системе: источник + id отзыва в источнике."""
        return f"{self.source}:{self.source_review_id}"

    def content_hash(self) -> str:
        """Хэш содержимого: меняется, если отредактирован текст, оценка или ответ."""
        parts = [str(self.rating), self.review_text or "", self.organization_reply or "",
                 self.reply_date.isoformat() if self.reply_date else ""]
        return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()


@dataclass
class ReviewPage:
    """Одна страница выдачи отзывов."""
    reviews: list[Review]
    page_no: int
    next_cursor: str | None              # None — выдача кончилась (по словам источника)
    declared_total: int | None = None    # сколько отзывов источник заявил в этом ответе
    meta: dict[str, Any] = field(default_factory=dict)


class CollectorError(RuntimeError):
    """Ошибка источника, после которой можно повторить позже."""


class SourceBlocked(CollectorError):
    """Источник ограничил доступ (капча, 403, бан). Повторять в этом прогоне нельзя:
    сборщик останавливает источник и записывает причину, обходить защиту — нельзя."""

    def __init__(self, source: str, reason: str, url: str | None = None):
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason
        self.url = url


class NotFound(CollectorError):
    """Карточки отделения в источнике нет (закрыто, удалено, неверный id)."""
