"""Единый источник правды по категориям продуктов.

До этого знание жило в 4 рассинхронизированных местах: enum БД (001_init.sql),
CATS_ORDER/CAT_LABELS во фронте, _CAT_RU в digest/writer.py (с битыми ключами
autocredit/credit_card) и Literal в models.py. Фронт получает всё через
/api/meta/categories.

compare: "rate" — сравнение по ставке; None — сопоставимой метрики нет
(card_debit: fee_service не отдаётся источником — у всех 65 офферов NULL).
lower_is_better: для кредитных продуктов ниже ставка = лучше позиция.
"""
from __future__ import annotations

CATEGORIES: list[dict] = [
    {"id": "deposit",     "label": "Вклады",          "ru": "вклады",
     "lower_is_better": False, "compare": "rate"},
    {"id": "credit",      "label": "Кредиты",         "ru": "кредиты",
     "lower_is_better": True,  "compare": "rate"},
    {"id": "mortgage",    "label": "Ипотека",         "ru": "ипотека",
     "lower_is_better": True,  "compare": "rate"},
    {"id": "card_credit", "label": "Кредитные карты", "ru": "кредитные карты",
     "lower_is_better": True,  "compare": "rate"},
    {"id": "card_debit",  "label": "Дебетовые карты", "ru": "дебетовые карты",
     "lower_is_better": False, "compare": None},
    {"id": "auto_loan",   "label": "Автокредиты",     "ru": "автокредиты",
     "lower_is_better": True,  "compare": "rate"},
    {"id": "metals",      "label": "Драгметаллы",     "ru": "обезличенные металлические счета",
     "lower_is_better": False, "compare": "rate"},
]

CAT_IDS = [c["id"] for c in CATEGORIES]
LOWER_IS_BETTER = {c["id"] for c in CATEGORIES if c["lower_is_better"]}
CAT_LABEL = {c["id"]: c["label"] for c in CATEGORIES}

# Русские названия для LLM-промптов дайджеста (шире витринных категорий)
CAT_RU = {**{c["id"]: c["ru"] for c in CATEGORIES},
          "savings": "накопительные счета", "transfers": "переводы",
          "microloan": "микрозаймы", "other": "прочее"}
