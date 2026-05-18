"""Pydantic-модели для обмена между collector → normalizer → storage.
   ORM-моделей не делаем: пишем чистым SQL через SQLAlchemy Core/text() —
   меньше магии, проще аудит."""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

ProductCategory = Literal[
    "deposit", "savings_account", "credit", "refinance",
    "card_debit", "card_credit",
    "mortgage", "mortgage_refinance", "auto_loan", "microloan",
    "metals", "investment", "invest_broker", "invest_pif", "npf",
    "insurance", "osago", "kasko",
    "insurance_mortgage", "insurance_travel", "insurance_life",
    "rko", "business_loan", "leasing", "factoring", "acquiring",
    "currency_exchange", "bank_rating", "other",
]

class FilterContext(BaseModel):
    amount: Optional[Decimal] = None
    period_months: Optional[int] = None
    region: Optional[str] = None
    client_type: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)

class RawSnapshot(BaseModel):
    source: str
    target_name: str
    url: str
    fetched_at: datetime
    http_status: int
    content_sha256: str
    storage_path: str
    bytes: int
    filter_context: FilterContext | None = None
    category: Optional[ProductCategory] = None

class OfferDraft(BaseModel):
    """Универсальный «черновик предложения» от любого адаптера."""
    bank_name_raw: str
    category: ProductCategory
    external_id: str               # стабильный ID источника или хеш ключевых полей
    title: str
    url: Optional[str] = None
    rate_pct: Optional[Decimal] = None
    rate_kind: Optional[str] = None
    currency: str = "RUB"
    amount_min: Optional[Decimal] = None
    amount_max: Optional[Decimal] = None
    term_months_min: Optional[int] = None
    term_months_max: Optional[int] = None
    fee_open: Optional[Decimal] = None
    fee_service: Optional[Decimal] = None
    early_withdraw: Optional[bool] = None
    capitalization: Optional[bool] = None
    replenishable: Optional[bool] = None
    conditions: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)

class ReviewDraft(BaseModel):
    source: str
    source_review_id: str
    source_url: str
    bank_name_raw: str
    product_category: Optional[ProductCategory] = None
    posted_at: Optional[datetime] = None
    rating: Optional[Decimal] = None
    title: Optional[str] = None
    text: str
    author_raw: Optional[str] = None
    status: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)
