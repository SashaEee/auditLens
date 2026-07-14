"""Pydantic-модели обмена модуля loophole."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class LoopholeRecord(BaseModel):
    record_id: int | None = None
    sha256: str
    title: str | None = None
    url: str | None = None
    snippet: str | None = None
    domain: str | None = None
    trust_score: float | None = None
    fetched_at: datetime | None = None
    collected_at: datetime | None = None
    bank_slug: str | None = None
    keyword: str | None = None
    raw_text: str | None = None
    is_loophole: bool | None = None
    verdict_confidence: float | None = None
    verdict_reason: str | None = None
    verdict_model: str | None = None
    classified_at: datetime | None = None
    status: str = "new"


class SearchQuery(BaseModel):
    period_from: date | None = None
    period_to: date | None = None
    bank_slugs: list[str] = Field(default_factory=list)
    query_text: str = ""


class ChatMessage(BaseModel):
    role: str
    content: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    created_at: datetime | None = None


class ExportRequest(BaseModel):
    result_id: int | None = None
    records: list[int] | None = None
    format: str = "json"


class WorkspaceCreate(BaseModel):
    name: str | None = None


class KeywordOut(BaseModel):
    keyword_id: int | None = None
    keyword: str
    category: str | None = None
    source: str | None = None
    weight: float = 1.0
    is_active: bool = True


class Verdict(BaseModel):
    is_loophole: bool
    confidence: float
    reason: str
