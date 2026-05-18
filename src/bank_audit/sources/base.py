from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterable
from ..models import OfferDraft, ReviewDraft, RawSnapshot

@dataclass
class FetchResult:
    snapshot: RawSnapshot
    html: bytes

class SourceAdapter(ABC):
    """Базовый интерфейс источника. Адаптер делает три вещи:
       1) fetch(target) — забирает контент (через http/browser);
       2) parse_offers(html, target) — извлекает офферы (если применимо);
       3) parse_reviews(html, target) — извлекает отзывы (если применимо).
       Любая из 2/3 может вернуть пустой список — это нормально."""
    name: str

    def __init__(self, settings, raw_store, http=None, browser=None):
        self.settings = settings
        self.raw = raw_store
        self.http = http
        self.browser = browser

    @abstractmethod
    def fetch(self, target: dict[str, Any]) -> FetchResult: ...

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        return []

    def parse_reviews(self, html: bytes, target: dict[str, Any]) -> Iterable[ReviewDraft]:
        return []
