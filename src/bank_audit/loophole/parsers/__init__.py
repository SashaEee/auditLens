"""Парсеры интернет-ресурсов для поиска лазеек.

Связка Playwright + Scrapy. Генерация кода LLM, запуск как subprocess,
сохранение результатов в loophole_record.
"""
from __future__ import annotations

from .generator import extract_targets, generate_parser, sanitize_filename
from .registry import delete_parser, get_parser, list_parsers
from .runner import _RUNNING, ParserRunner

__all__ = [
    "_RUNNING",
    "ParserRunner",
    "delete_parser",
    "extract_targets",
    "generate_parser",
    "get_parser",
    "list_parsers",
    "sanitize_filename",
]
