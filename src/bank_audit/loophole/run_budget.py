"""Общий бюджет времени и явное количество результатов одного исследования."""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field

_REQUESTED_COUNT = re.compile(
    r"\b(?:найди|найдите|найти|покажи|покажите|подбери|подберите|ищи)\s+"
    r"(?:мне\s+)?(?:(?:ровно|только|всего)\s+)?"
    r"(?P<count>[1-9]\d{0,2}|одну|одна|один|две|два|три|четыре|пять)\s+"
    r"(?:проверенн\w+\s+|подтвержд[её]нн\w+\s+)?лазей(?:ку|ки|ка|ек)\b",
    re.IGNORECASE,
)
_COUNT_WORDS = {"одну": 1, "одна": 1, "один": 1, "две": 2, "два": 2,
                "три": 3, "четыре": 4, "пять": 5}


def requested_finding_count(query: str) -> int | None:
    """Извлекает явно названное число лазеек, не принимая год за количество."""
    text = query or ""
    if re.search(
        r"\b(?:не\s+ограничивай\w*|широк\w+\s+поиск|исчерпывающ\w+|"
        r"все\s+лазейки|как\s+можно\s+больше)\b", text, re.IGNORECASE
    ):
        return None
    match = _REQUESTED_COUNT.search(text)
    if match is None:
        return None
    value = match.group("count").lower()
    return _COUNT_WORDS.get(value) or int(value)


@dataclass
class ResearchBudget:
    """Разделяемое состояние; меняется только в event loop владельца запуска."""

    timeout_seconds: float = 0.0
    requested_count: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    stop_reason: str | None = None
    phase: str = "waiting_model"
    model_timeout_seconds: float = field(default_factory=lambda: _limit(
        "LOOPHOLE_MODEL_TIMEOUT_SECONDS", 0, 300, minimum=0))
    no_progress_limit: int = field(default_factory=lambda: _limit(
        "LOOPHOLE_NO_PROGRESS_ROUNDS", 3, 10))
    search_limit: int = field(default_factory=lambda: _limit(
        "LOOPHOLE_SEARCH_LIMIT", 12, 60))
    fetch_limit: int = field(default_factory=lambda: _limit(
        "LOOPHOLE_FETCH_LIMIT", 12, 60))
    search_results: list[dict] = field(default_factory=list)
    search_cache: dict[str, object] = field(default_factory=dict)
    fetch_cache: dict[str, object] = field(default_factory=dict)
    source_failures: dict[str, str] = field(default_factory=dict)
    analysis_status: dict[str, str] = field(default_factory=dict)
    analysis_results: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def reserve_seconds(self) -> float:
        """Короткий резерв для закрытия ресурсов и детерминированного отчёта."""
        return min(10.0, self.timeout_seconds * 0.05)

    def research_seconds(self) -> float:
        return max(0.0, self.remaining_seconds() - self.reserve_seconds)

    def remaining_seconds(self) -> float:
        if self.timeout_seconds == 0:
            return float("inf")
        return max(0.0, self.timeout_seconds - (time.monotonic() - self.started_at))

    @property
    def elapsed_seconds(self) -> int:
        return max(0, int(time.monotonic() - self.started_at))

    @property
    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def ensure_active(self) -> None:
        """Отменённые и опоздавшие результаты не должны менять общий контекст."""
        if self.cancelled or self.stop_reason == "requested_count":
            raise asyncio.CancelledError
        if self.expired:
            raise TimeoutError("Исчерпан общий бюджет исследования")


def _limit(name: str, default: int, maximum: int, *, minimum: int = 1) -> int:
    """Проверяет ограниченные настройки исследования до начала сетевых вызовов."""
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} должен быть от {minimum} до {maximum}")
    return value
