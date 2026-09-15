"""Общий бюджет времени и серверная стратегия охвата исследования."""
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
    r"(?:проверенн\w+\s+|подтвержд[её]нн\w+\s+)?(?:"
    r"лазей(?:ку|ки|ка|ек)|(?:мошенническ\w*\s+)?схем\w*)\b",
    re.IGNORECASE,
)
_FRAUD_QUERY = re.compile(r"\b(?:мошеннич\w*\s+)?схем\w*|fraud\b", re.IGNORECASE)
_COUNT_WORDS = {"одну": 1, "одна": 1, "один": 1, "две": 2, "два": 2,
                "три": 3, "четыре": 4, "пять": 5}
DEFAULT_FINDING_COUNT = 5
MAX_SUCCESSFUL_PAGES = 100


def requested_finding_count(query: str) -> int | None:
    """Извлекает явно названное число лазеек или мошеннических схем, не принимая год."""
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


def requested_finding_kind(query: str) -> str:
    """Определяет отдельную цель: мошеннические схемы не смешиваются с лазейками."""
    return "fraud" if _FRAUD_QUERY.search(query or "") else "loophole"


@dataclass
class ResearchBudget:
    """Разделяемое состояние; меняется только в event loop владельца запуска."""

    timeout_seconds: float = 0.0
    requested_count: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    stop_reason: str | None = None
    phase: str = "waiting_model"
    # Read-таймаут ответа основной модели и потолок всего вызова SDK с повторами.
    # Явный 0 по-прежнему отключает read-таймаут; connect-timeout от него не зависит.
    model_timeout_seconds: float = field(default_factory=lambda: _limit(
        "LOOPHOLE_MODEL_TIMEOUT_SECONDS", 600, 3600, minimum=0))
    no_progress_limit: int = field(default_factory=lambda: _limit(
        "LOOPHOLE_NO_PROGRESS_ROUNDS", 3, 10))
    # Устаревшие квоты 12/12 останавливали исследование по числу попыток,
    # а не по реально прочитанным страницам. Стратегия ограничивает только
    # успешные уникальные страницы; параметр сохранён для совместимости,
    # но не является терминальным условием поиска.
    search_limit: int = field(default_factory=lambda: _limit(
        "LOOPHOLE_SEARCH_LIMIT", 100, 100))
    fetch_limit: int = field(default_factory=lambda: _limit(
        "LOOPHOLE_FETCH_LIMIT", 100, 100))
    search_results: list[dict] = field(default_factory=list)
    search_clusters: list[str] = field(default_factory=list)
    search_cache: dict[str, object] = field(default_factory=dict)
    fetch_cache: dict[str, object] = field(default_factory=dict)
    successful_page_urls: set[str] = field(default_factory=set)
    source_failures: dict[str, str] = field(default_factory=dict)
    # Счётчик поисковых запросов с момента последней успешно прочитанной страницы.
    searches_since_read: int = 0
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
        if self.cancelled or self.stop_reason in {"requested_count", "page_limit"}:
            raise asyncio.CancelledError
        if self.expired:
            raise TimeoutError("Исчерпан общий бюджет исследования")

    @property
    def target_finding_count(self) -> int:
        """Цель: явное число пользователя либо контролируемое сервером значение 5."""
        return self.requested_count if self.requested_count is not None else DEFAULT_FINDING_COUNT

    @property
    def successful_page_count(self) -> int:
        """Количество уникальных канонических страниц с непустым извлечённым текстом."""
        return len(self.successful_page_urls)

    @property
    def page_limit_reached(self) -> bool:
        return self.successful_page_count >= MAX_SUCCESSFUL_PAGES

    def register_successful_page(self, canonical_url: str) -> bool:
        """Регистрирует страницу ровно один раз; не засчитывает URL-попытки."""
        if not canonical_url or canonical_url in self.successful_page_urls:
            return False
        if self.page_limit_reached:
            return False
        self.successful_page_urls.add(canonical_url)
        return True


def _limit(name: str, default: int, maximum: int, *, minimum: int = 1) -> int:
    """Проверяет ограниченные настройки исследования до начала сетевых вызовов."""
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} должен быть от {minimum} до {maximum}")
    return value


# Сколько поисковых запросов подряд допустимо без единой успешно прочитанной
# страницы, прежде чем сервер начнёт требовать чтения найденных URL.
READ_NUDGE_AFTER = _limit("LOOPHOLE_READ_NUDGE_AFTER", 8, 50, minimum=2)
