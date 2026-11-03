"""Направления аудита — общая ось разделов (directions.py). Тест не даёт добавить
направление, не связав его со всеми разделами, и не даёт пробелу появиться молча."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit import directions as D  # noqa: E402
from bank_audit.rag import review_codebook as cb  # noqa: E402
from bank_audit.rag import review_slices as RS  # noqa: E402
from bank_audit.web import userdata as U  # noqa: E402


def test_twelve_directions_with_unique_codes_and_topics():
    assert len(D.DIRECTIONS) == 12
    assert len({d.code for d in D.DIRECTIONS}) == 12 and len({d.topic for d in D.DIRECTIONS}) == 12


def test_every_direction_is_mapped_in_reviews_or_gap_is_explicit():
    labels = set(v for v in cb.PRODUCTS.values() if v)
    for d in D.DIRECTIONS:
        assert d.review_products or d.review_slices or d.review_vulnerable or d.review_gap, d.code
        for p in d.review_products:
            assert p in labels, (d.code, p)
        for s in d.review_slices:
            assert s in RS.SLICES, (d.code, s)
        for v in d.review_vulnerable:
            assert v in cb.VULNERABLE, (d.code, v)


def test_profile_and_news_know_every_direction():
    """Описание профиля «самозанятые, сервис "Своё дело"» разбиралось в пустой список
    (03.11.2026): теперь каждое направление узнаётся словами аудитора."""
    probes = {"mortgage": "ипотека", "metals": "обезличенные металлические счета", "field": "курьер не привёз",
              "youth": "детская карта", "selfemp": "самозанятые, сервис «Своё дело»", "credit_card": "кредитка",
              "acquiring": "эквайринг", "atm": "банкомат съел карту", "invest": "брокерский счёт",
              "consumer": "кредит наличными", "svo": "участник СВО", "payroll": "зарплатный проект"}
    for d in D.DIRECTIONS:
        got = U.parse_query_signals(probes[d.code])["products"]
        assert d.topic in got, (d.code, got)


def test_review_maps_cover_slice_labels():
    prod, sl, vul = D.review_maps()
    assert prod["Зарплатные проекты"] == "payroll" and prod["Ипотека"] == "mortgage"
    assert sl["us"] == "atm" and vul["minor"] == "youth"


def test_ai_tools_can_filter_by_every_slice():
    """Инструменты ИИ видят срезы в перечне продуктов: вопрос о «Своём деле» без среза
    уходил в продукт «Бизнес» и приносил жалобы на СберБизнес (03.11.2026)."""
    from bank_audit.ai import agent_tools as T
    for label in RS.LABELS.values():
        assert label in T.PRODUCT_LABELS, label
    assert len(set(T.PRODUCT_LABELS)) == len(T.PRODUCT_LABELS)
