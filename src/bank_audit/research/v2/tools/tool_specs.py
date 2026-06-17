"""Каталог tool-спецификаций для function-calling.

Описания (description, parameters) переиспользуются всеми агентами. Реализации
(fn) — в web_tools.py. Агент выбирает подмножество через AGENT_TOOLS.
"""
from __future__ import annotations

from .web_tools import (tool_web_search, tool_read_url, tool_semantic_search,
                         tool_run_sql)
from ..base_agent import ToolSpec


# ── WEB SEARCH ────────────────────────────────────────────────────────────
WEB_SEARCH = ToolSpec(
    name="web_search",
    description=(
        "Поиск в интернете (Google/Bing/Yandex через multi-backend). "
        "Возвращает список результатов: {title, url, snippet, domain, trust}. "
        "НЕ скачивает содержимое страниц — только метаданные SERP. "
        "Для чтения страницы вызови read_url. "
        "Используй site: в query для ограничения по домену "
        "(напр. 'site:sberbank.ru автоперевод', 'site:banki.ru отзыв автоплатёж')."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Поисковый запрос. Можно с site: оператором."},
            "max_results": {"type": "integer", "default": 8},
            "site_filter": {
                "type": "array", "items": {"type": "string"},
                "description": "Ограничить результат доменами (опционально)",
            },
        },
        "required": ["query"],
    },
    fn=tool_web_search,
)


# ── READ URL ──────────────────────────────────────────────────────────────
READ_URL = ToolSpec(
    name="read_url",
    description=(
        "Скачать страницу/PDF по URL и вернуть текст. "
        "Документ автоматически индексируется в БД (future requests найдут его "
        "через semantic_search). Источник регистрируется для цитирования [N]. "
        "Возвращает {url, title, text, domain, source_n, trust}. "
        "Используй после web_search для конкретных релевантных URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL страницы или PDF"},
            "query": {"type": "string",
                      "description": "Подсказка для релевантной выборки фрагментов больших страниц"},
            "budget_chars": {"type": "integer", "default": 12000},
            "bank_slug": {"type": "string", "description": "опционально — для индексации"},
        },
        "required": ["url"],
    },
    fn=tool_read_url,
)


# ── SEMANTIC SEARCH (кэш БД) ──────────────────────────────────────────────
SEMANTIC_SEARCH = ToolSpec(
    name="semantic_search",
    description=(
        "Семантический поиск по УЖЕ проиндексированным документам в БД (кэш). "
        "Быстро и бесплатно. ИСПОЛЬЗУЙ ПЕРВЫМ — данные могут быть в кэше от "
        "предыдущих запросов. Если результатов <3 — дополнительно web_search. "
        "Возвращает фрагменты документов с {text, url, source_n, trust}."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "bank_slugs": {"type": "array", "items": {"type": "string"},
                           "description": "Фильтр по банкам (опционально)"},
            "doc_types": {"type": "array", "items": {"type": "string"},
                          "description": "Фильтр: html|pdf (опционально)"},
            "trust_min": {"type": "number", "default": 0.5},
            "top_k": {"type": "integer", "default": 6},
        },
        "required": ["query"],
    },
    fn=tool_semantic_search,
)


# ── RUN SQL (БД: offers, reviews, change_history) ─────────────────────────
RUN_SQL = ToolSpec(
    name="run_sql",
    description=(
        "Read-only SELECT по предзаданным таблицам/представлениям БД платформы. "
        "Доступно: v_offer_current, v_sber_vs_market, v_review_topics, "
        "v_review_sentiment_share, v_bank_coverage, bank, review, review_topic, "
        "review_sentiment, product_offer, product_terms, quality_flag, "
        "change_history. "
        "Запрещено: всё кроме SELECT/WITH. LIMIT обязателен."
    ),
    parameters={
        "type": "object",
        "properties": {
            "sql": {"type": "string",
                    "description": "Один SELECT-запрос. Без ; в конце. С LIMIT."},
        },
        "required": ["sql"],
    },
    fn=tool_run_sql,
)


# ── НАБОРЫ ДЛЯ АГЕНТОВ ────────────────────────────────────────────────────

# Researcher: всё для поиска фактов
RESEARCHER_TOOLS = [SEMANTIC_SEARCH, WEB_SEARCH, READ_URL, RUN_SQL]

# Reviews: поиск отзывов + SQL по review таблице
REVIEWS_TOOLS = [SEMANTIC_SEARCH, WEB_SEARCH, READ_URL, RUN_SQL]

# Regulatory: акцент на gov.ru + законы (через web_search + read_url)
REGULATORY_TOOLS = [SEMANTIC_SEARCH, WEB_SEARCH, READ_URL]

# Market: тренды/доли/реформы — web-first
MARKET_TOOLS = [WEB_SEARCH, READ_URL, SEMANTIC_SEARCH, RUN_SQL]
