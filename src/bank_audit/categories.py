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

# rate_label/show_* — семантика витрины (аудит данных 22.07.2026):
#   card_credit: sravni отдаёт промо-минимумы («от 0%» у рассрочек) — ранжировать
#     по ним нельзя, ставку показываем с честной пометкой «промо от»;
#   card_debit: в rate_pct лежит кредитная ставка овердрафта/грейс-кредиток
#     (30–60%) — для дебетовой витрины это дезинформация, скрываем;
#   mortgage: минимумы мешают господдержку с рыночными — сравнение с оговоркой;
#   show_terms: фильтр срока только там, где сроки собираются осмысленно.
CATEGORIES: list[dict] = [
    {"id": "deposit",     "label": "Вклады",          "ru": "вклады",
     "lower_is_better": False, "compare": "rate",
     "rate_label": "Ставка",        "show_rate": True,  "show_bar": True,  "show_terms": True},
    {"id": "credit",      "label": "Кредиты",         "ru": "кредиты",
     "lower_is_better": True,  "compare": "rate",
     "rate_label": "Ставка от",     "show_rate": True,  "show_bar": True,  "show_terms": True},
    {"id": "mortgage",    "label": "Ипотека",         "ru": "ипотека",
     "lower_is_better": True,  "compare": "rate",
     "rate_label": "Ставка от",     "show_rate": True,  "show_bar": True,  "show_terms": False,
     "caveat": "Минимальные ставки программ: господдержка и рыночные смешаны — сравнивайте внутри программы"},
    {"id": "card_credit", "label": "Кредитные карты", "ru": "кредитные карты",
     "lower_is_better": True,  "compare": None,
     "rate_label": "Промо от",      "show_rate": True,  "show_bar": False, "show_terms": False,
     "caveat": "Источник отдаёт промо-минимумы («от 0%» у рассрочек) — ранжирование по ставке некорректно"},
    {"id": "card_debit",  "label": "Дебетовые карты", "ru": "дебетовые карты",
     "lower_is_better": False, "compare": None,
     "rate_label": None,            "show_rate": False, "show_bar": False, "show_terms": False,
     "caveat": "Сопоставимая метрика (стоимость обслуживания/кешбэк) источником не отдаётся"},
    {"id": "auto_loan",   "label": "Автокредиты",     "ru": "автокредиты",
     "lower_is_better": True,  "compare": "rate",
     "rate_label": "Ставка от",     "show_rate": True,  "show_bar": True,  "show_terms": False},
    {"id": "metals",      "label": "Драгметаллы",     "ru": "обезличенные металлические счета",
     "lower_is_better": False, "compare": "rate",
     "rate_label": "Ставка",        "show_rate": True,  "show_bar": True,  "show_terms": False,
     "caveat": "Источник по ОМС не подключён — прежние данные были синтетическим сидом и деактивированы"},
]

CAT_IDS = [c["id"] for c in CATEGORIES]
LOWER_IS_BETTER = {c["id"] for c in CATEGORIES if c["lower_is_better"]}
CAT_LABEL = {c["id"]: c["label"] for c in CATEGORIES}

# Русские названия для LLM-промптов дайджеста (шире витринных категорий)
CAT_RU = {**{c["id"]: c["ru"] for c in CATEGORIES},
          "savings": "накопительные счета", "transfers": "переводы",
          "microloan": "микрозаймы", "other": "прочее"}
