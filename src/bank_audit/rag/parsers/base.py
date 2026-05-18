"""Базовая структура ParsedDoc + автодиспетчер по mime/расширению."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger(__name__)


@dataclass
class ParsedDoc:
    """Унифицированный результат парсинга для всех типов документов."""
    title:         str | None = None
    text:          str = ""                          # markdown-стиль с # заголовками
    headings_path: str | None = None                 # breadcrumb для UI
    doc_type:      str = "html"                      # html|pdf|xlsx|pptx|docx|txt|json
    tables:        list[dict] = field(default_factory=list)  # для PDF/XLSX — структурированные таблицы
    meta:          dict = field(default_factory=dict)        # author, page_count, sheet_names, ...

    def is_empty(self) -> bool:
        return not self.text or len(self.text.strip()) < 80


def detect_doc_type(url: str, content_type: str | None = None) -> str:
    """Определяет тип документа по URL и Content-Type."""
    if content_type:
        ct = content_type.lower()
        if "pdf" in ct: return "pdf"
        if "spreadsheet" in ct or "excel" in ct: return "xlsx"
        if "presentation" in ct or "powerpoint" in ct: return "pptx"
        if "msword" in ct or "wordprocessing" in ct: return "docx"
        if "html" in ct or "xml" in ct: return "html"
        if "json" in ct: return "json"
        if "text/" in ct: return "txt"
    # По расширению URL
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):  return "pdf"
    if path.endswith((".xlsx", ".xls", ".xlsm")): return "xlsx"
    if path.endswith((".pptx", ".ppt")): return "pptx"
    if path.endswith((".docx", ".doc")): return "docx"
    if path.endswith(".json"): return "json"
    if path.endswith(".txt"):  return "txt"
    return "html"


def parse_auto(content: bytes, url: str = "",
               content_type: str | None = None) -> ParsedDoc:
    """Авто-выбор парсера + защита от исключений (возвращает empty doc)."""
    doc_type = detect_doc_type(url, content_type)
    try:
        if doc_type == "pdf":
            from .pdf_parser import parse_pdf
            return parse_pdf(content, url=url)
        if doc_type == "xlsx":
            from .xlsx_parser import parse_xlsx
            return parse_xlsx(content, url=url)
        if doc_type == "pptx":
            from .pptx_parser import parse_pptx
            return parse_pptx(content, url=url)
        if doc_type == "json":
            from .html_parser import parse_html  # JSON в текст просто
            text = content.decode("utf-8", errors="ignore")
            return ParsedDoc(text=text, doc_type="json")
        if doc_type == "txt":
            return ParsedDoc(text=content.decode("utf-8", errors="ignore"),
                             doc_type="txt")
        # Default: HTML
        from .html_parser import parse_html
        return parse_html(content, url=url)
    except Exception as e:
        log.warning("parse_auto failed for %s (%s): %s", url, doc_type, e)
        return ParsedDoc(doc_type=doc_type)
