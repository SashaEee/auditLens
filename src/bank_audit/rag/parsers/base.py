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


# Сигнатуры контента: расширение и Content-Type регулярно врут (порталы отдают
# файлы ссылками вида /download?id=…, ЦБ — /Collection/File/NNN), и бинарник
# уходил в HTML-парсер: decode(errors="ignore") превращал его в кашу, а наверх
# улетал ложный диагноз «пустая страница».
def sniff_magic(content: bytes) -> str | None:
    """Тип по первым байтам: pdf | zip-офис (docx/xlsx/pptx) | ole | None."""
    if not content or len(content) < 8:
        return None
    if content[:5] == b"%PDF-":
        return "pdf"
    if content[:4] == b"PK\x03\x04":
        # zip: смотрим, чей это офисный пакет
        head = content[:4000]
        if b"word/" in head:
            return "docx"
        if b"xl/" in head:
            return "xlsx"
        if b"ppt/" in head:
            return "pptx"
        return "zip"
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"      # legacy .doc/.xls/.ppt
    return None


def parse_docx(content: bytes, url: str = "") -> ParsedDoc:
    """.docx стандартной библиотекой: zip → word/document.xml → текст.

    Регуляторы (ЦБ, ФАС, Минфин) публикуют указания и разъяснения в docx;
    парсер без внешних зависимостей достаёт абзацы и таблицы — достаточно,
    чтобы агент прочитал норму, а не получил «пустую страницу».
    """
    import io
    import re as _re
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception as e:
        return ParsedDoc(doc_type="docx",
                         meta={"url": url, "skipped_reason": f"docx не читается: {e}"})
    # Абзацы (без таблиц) — тело и заголовок; таблицы — отдельной секцией.
    tables: list[str] = []
    for tbl in _re.findall(r"<w:tbl\b.*?</w:tbl>", xml, _re.S):
        rows = []
        for tr in _re.findall(r"<w:tr\b.*?</w:tr>", tbl, _re.S):
            cells = ["".join(_re.findall(r"<w:t[^>]*>([^<]*)</w:t>", tc))
                     for tc in _re.findall(r"<w:tc\b.*?</w:tc>", tr, _re.S)]
            if any(c.strip() for c in cells):
                rows.append("| " + " | ".join(c.strip() for c in cells) + " |")
        if rows:
            tables.append("\n".join(rows))
    xml_wo_tbl = _re.sub(r"<w:tbl\b.*?</w:tbl>", " ", xml, flags=_re.S)
    paras: list[str] = []
    for para in _re.findall(r"<w:p\b.*?</w:p>", xml_wo_tbl, _re.S):
        txt = "".join(_re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para)).strip()
        if txt:
            paras.append(txt)
    body = "\n\n".join(paras)
    if tables:
        body += "\n\n# Таблицы документа\n\n" + "\n\n".join(tables)
    title = paras[0][:110] if paras else None
    return ParsedDoc(title=title, text=body, doc_type="docx",
                     meta={"url": url, "char_count": len(body)})


def parse_auto(content: bytes, url: str = "",
               content_type: str | None = None) -> ParsedDoc:
    """Авто-выбор парсера + защита от исключений (возвращает empty doc)."""
    doc_type = detect_doc_type(url, content_type)
    # Сигнатура контента сильнее расширения: она не умеет врать.
    magic = sniff_magic(content)
    if magic in ("pdf", "docx", "xlsx", "pptx") and magic != doc_type:
        doc_type = magic
    elif magic == "ole":
        # Legacy .doc/.xls: парсера нет — честно говорим об этом, а не
        # скармливаем бинарник HTML-парсеру с ложной «пустой страницей».
        return ParsedDoc(doc_type="doc" if doc_type == "docx" else "xls",
                         meta={"url": url, "skipped_reason":
                               "старый бинарный формат Office (.doc/.xls) "
                               "не поддержан — нужен файл в docx/xlsx"})
    try:
        if doc_type == "docx":
            return parse_docx(content, url=url)
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
        return ParsedDoc(doc_type=doc_type,
                         meta={"url": url,
                               "skipped_reason": f"парсер {doc_type} упал: "
                                                 f"{str(e)[:120]}"})
