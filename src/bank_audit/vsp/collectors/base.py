"""Единый интерфейс адаптера источника.

Каждый адаптер (TwoGisCollector, YandexMapsCollector, DomclickCollector) умеет три
вещи, и только их:
  • discover(area)      — найти отделения Сбербанка в городе/регионе и их id;
  • branch(source_id)   — карточка отделения: адрес, координаты, заявленное число отзывов;
  • pages(source_id)    — постраничная выдача отзывов в выбранной сортировке.

Всё остальное — частота, повторы, прогресс, дедупликация, полнота — общее и живёт
в sync.py и storage.py. Адаптер честно описывает свои ограничения в `capabilities`:
сколько отзывов источник реально отдаёт, есть ли пагинация до конца истории.
Предполагаемая полнота не выдаётся за фактическую — её считает sync по факту.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..config import Settings, SourceSettings
from ..models import ReviewPage, SourceBranch


@dataclass(frozen=True)
class Area:
    """Где искать отделения: город (и/или регион), опционально — центр и радиус."""
    city: str
    region: str | None = None
    lat: float | None = None
    lon: float | None = None
    radius_m: int | None = None


@dataclass(frozen=True)
class Capabilities:
    method: str                          # public_api | page_state | browser_xhr
    sorts: tuple[str, ...]               # доступные сортировки, первая — «сначала новые»
    full_history: bool                   # можно ли дойти до самого старого отзыва
    max_reachable: int | None = None     # потолок выдачи на отделение, если известен
    replies: bool = True                 # отдаёт ли ответы организации
    notes: str = ""
    extra: dict = field(default_factory=dict)


class Collector(ABC):
    source: str = ""
    capabilities: Capabilities

    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg: SourceSettings = settings.source(self.source)

    async def close(self) -> None:  # noqa: B027 — по умолчанию нечего закрывать
        pass

    @abstractmethod
    def discover(self, area: Area) -> AsyncIterator[SourceBranch]:
        """Отделения Сбербанка в области поиска (без отзывов)."""

    @abstractmethod
    async def branch(self, source_branch_id: str) -> SourceBranch:
        """Карточка одного отделения; NotFound — если карточки нет."""

    @abstractmethod
    def pages(self, source_branch_id: str, *, sort: str | None = None,
              cursor: str | None = None) -> AsyncIterator[ReviewPage]:
        """Страницы отзывов от cursor (None — с начала) до конца выдачи источника."""
