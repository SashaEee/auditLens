"""Парсеры интернет-ресурсов для поиска лазеек.

Связка Playwright + Scrapy. Генерация кода LLM, запуск как subprocess,
сохранение результатов в loophole_record.
"""
from __future__ import annotations

from .generator import generate_parser, sanitize_filename
from .runner import ParserRunner, _RUNNING
from .registry import list_parsers, get_parser, delete_parser

__all__ = [
    "generate_parser",
    "sanitize_filename",
    "ParserRunner",
    "_RUNNING",
    "list_parsers",
    "get_parser",
    "delete_parser",
]
