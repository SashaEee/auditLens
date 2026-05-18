"""PDF parser: pdfplumber → text + structured tables.

В банковских PDF (тарифные планы) львиная доля смысла — в таблицах. Поэтому:
  • Извлекаем text постранично с # заголовком "Страница N"
  • Таблицы конвертируем в markdown-формат (для LLM удобнее) и сохраняем
    также в meta.tables для structured access
  • Шапки/футеры повторяющиеся — детектим и удаляем (одинаковая первая
    строка на ≥3 страницах)
"""
from __future__ import annotations
import io, logging, re
import pdfplumber

from .base import ParsedDoc

log = logging.getLogger(__name__)

_MAX_PAGES = 100  # кап на огромные PDF чтобы не висеть


def _table_to_markdown(table: list[list[str | None]]) -> str:
    """Конвертирует pdfplumber-table (list of rows) в markdown table."""
    if not table or not table[0]:
        return ""
    header = [str(c or "").strip() for c in table[0]]
    if not any(header):
        return ""
    sep = ["---"] * len(header)
    rows = []
    for row in table[1:]:
        cells = [(str(c or "").strip().replace("\n", " ")) for c in row]
        # Дополняем до длины header
        while len(cells) < len(header):
            cells.append("")
        rows.append("| " + " | ".join(cells[:len(header)]) + " |")
    if not rows:
        return ""
    return ("| " + " | ".join(header) + " |\n"
            "| " + " | ".join(sep) + " |\n"
            + "\n".join(rows))


def parse_pdf(content: bytes, url: str = "") -> ParsedDoc:
    out_lines: list[str] = []
    tables_meta: list[dict] = []
    title = None

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        # Если есть metadata.title
        meta = pdf.metadata or {}
        title = (meta.get("Title") or "").strip() or None

        n_pages = min(len(pdf.pages), _MAX_PAGES)
        # Кандидаты в шапки/футеры
        first_lines: dict[str, int] = {}
        last_lines: dict[str, int] = {}
        for p in pdf.pages[:n_pages]:
            t = (p.extract_text() or "").strip()
            lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
            if lines:
                first_lines[lines[0]] = first_lines.get(lines[0], 0) + 1
                last_lines[lines[-1]] = last_lines.get(lines[-1], 0) + 1
        repeat_threshold = max(3, n_pages // 3)
        skip_first = {k for k, v in first_lines.items() if v >= repeat_threshold}
        skip_last  = {k for k, v in last_lines.items()  if v >= repeat_threshold}

        for page_idx, page in enumerate(pdf.pages[:n_pages], start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                lines = [ln.strip() for ln in page_text.split("\n") if ln.strip()]
                # Удаляем повторяющиеся шапки/футеры
                if lines and lines[0] in skip_first:
                    lines = lines[1:]
                if lines and lines[-1] in skip_last:
                    lines = lines[:-1]
                if lines:
                    out_lines.append(f"\n## Страница {page_idx}\n")
                    out_lines.append("\n".join(lines))

            # Таблицы
            try:
                page_tables = page.extract_tables() or []
            except Exception as e:
                log.debug("pdf table extract page %s failed: %s", page_idx, e)
                page_tables = []
            for ti, tbl in enumerate(page_tables):
                md = _table_to_markdown(tbl)
                if md:
                    out_lines.append(f"\n### Таблица (стр. {page_idx})\n\n{md}\n")
                    tables_meta.append({"page": page_idx, "idx": ti, "rows": len(tbl)})

    body = "\n".join(out_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    return ParsedDoc(
        title=title, text=body, doc_type="pdf",
        tables=tables_meta,
        meta={"url": url, "page_count": n_pages, "char_count": len(body)},
    )
