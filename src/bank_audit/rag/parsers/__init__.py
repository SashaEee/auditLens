"""Document parsers: HTML/PDF/XLSX/PPTX → ParsedDoc.

Все парсеры возвращают одинаковую структуру для индексации.
Выбор парсера — по mime/расширению через `parse_auto`.
"""
from .base import ParsedDoc, parse_auto, detect_doc_type
from .html_parser import parse_html
from .pdf_parser import parse_pdf
from .xlsx_parser import parse_xlsx
from .pptx_parser import parse_pptx

__all__ = [
    "ParsedDoc", "parse_auto", "detect_doc_type",
    "parse_html", "parse_pdf", "parse_xlsx", "parse_pptx",
]
