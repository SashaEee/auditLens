"""PDF parser: pdfplumber → text + structured tables.

В банковских PDF (тарифные планы) львиная доля смысла — в таблицах. Поэтому:
  • Извлекаем text постранично с # заголовком "Страница N"
  • Таблицы конвертируем в markdown-формат (для LLM удобнее) и сохраняем
    также в meta.tables для structured access
  • Шапки/футеры повторяющиеся — детектим и удаляем (одинаковая первая
    строка на ≥3 страницах)
"""
from __future__ import annotations
import ctypes, gc, io, logging, os, re, threading, time
import pdfplumber

from .base import ParsedDoc

log = logging.getLogger(__name__)

_MAX_PAGES = 100  # кап на огромные PDF чтобы не висеть

# Память. pdfplumber держит разобранную страницу (каждая буква — словарь) до
# закрытия документа: обзор регулятора на 100 страниц занимал 1,3 ГБ, четыре
# таких в соседних потоках — 3,3 ГБ, и процесс эту память системе не отдавал.
# 12.11 отчёты о жалобах (они тянут PDF Банка России) раздували процесс до
# 2,5 ГБ за полторы минуты. Поэтому: страницу закрываем сразу после чтения,
# одновременно разбираем не больше двух документов (разбор — чистый Python,
# потоки друг друга не ускоряют, а память у каждого своя), а после большого
# документа просим систему забрать освободившееся.
_SLOTS = threading.BoundedSemaphore(max(1, int(os.getenv("PDF_PARSE_MAX", "2") or 2)))
_TRIM_FROM_PAGES = 15


def _trim() -> None:
    """Вернуть системе память, освободившуюся после большого документа."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001 — не Linux или другая libc: обойдёмся
        pass


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


def parse_pdf(content: bytes, url: str = "", *, max_seconds: float | None = None) -> ParsedDoc:
    """Текст и таблицы PDF. max_seconds — предел на разбор: что успели, то и отдаём
    (живому отчёту незачем минуту читать один документ)."""
    with _SLOTS:
        doc = _parse_pdf(content, url, max_seconds)
    if (doc.meta or {}).get("page_count", 0) >= _TRIM_FROM_PAGES:
        _trim()
    return doc


def _parse_pdf(content: bytes, url: str, max_seconds: float | None) -> ParsedDoc:
    # страница → (номер, строки текста, таблицы): всё лёгкое, сами страницы закрыты
    pages: list[tuple[int, list[str], list[tuple[int, str, int]]]] = []
    title = None
    cut_by_time = False
    t0 = time.monotonic()

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        # Если есть metadata.title
        meta = pdf.metadata or {}
        title = (meta.get("Title") or "").strip() or None

        total = len(pdf.pages)
        n_pages = min(total, _MAX_PAGES)
        for page_idx in range(1, n_pages + 1):
            if max_seconds and time.monotonic() - t0 > max_seconds:
                cut_by_time = True
                break
            page = pdf.pages[page_idx - 1]
            try:
                text = (page.extract_text() or "").strip()
                lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                try:
                    page_tables = page.extract_tables() or []
                except Exception as e:
                    log.debug("pdf table extract page %s failed: %s", page_idx, e)
                    page_tables = []
                tables = [(ti, md, len(tbl)) for ti, tbl in enumerate(page_tables)
                          if (md := _table_to_markdown(tbl))]
            finally:
                page.close()        # память страницы — сразу, а не в конце документа
            pages.append((page_idx, lines, tables))

    n_pages = len(pages)
    # Шапки/футеры: одинаковая первая (последняя) строка на многих страницах
    first_lines: dict[str, int] = {}
    last_lines: dict[str, int] = {}
    for _, lines, _t in pages:
        if lines:
            first_lines[lines[0]] = first_lines.get(lines[0], 0) + 1
            last_lines[lines[-1]] = last_lines.get(lines[-1], 0) + 1
    repeat_threshold = max(3, n_pages // 3)
    skip_first = {k for k, v in first_lines.items() if v >= repeat_threshold}
    skip_last  = {k for k, v in last_lines.items()  if v >= repeat_threshold}

    out_lines: list[str] = []
    tables_meta: list[dict] = []
    for page_idx, lines, tables in pages:
        # Удаляем повторяющиеся шапки/футеры
        if lines and lines[0] in skip_first:
            lines = lines[1:]
        if lines and lines[-1] in skip_last:
            lines = lines[:-1]
        if lines:
            out_lines.append(f"\n## Страница {page_idx}\n")
            out_lines.append("\n".join(lines))
        for ti, md, n_rows in tables:
            out_lines.append(f"\n### Таблица (стр. {page_idx})\n\n{md}\n")
            tables_meta.append({"page": page_idx, "idx": ti, "rows": n_rows})

    body = "\n".join(out_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if cut_by_time:
        log.info("pdf %s: разобрано %s из %s стр. за %.0f с — предел времени",
                 url[:80], n_pages, total, time.monotonic() - t0)

    return ParsedDoc(
        title=title, text=body, doc_type="pdf",
        tables=tables_meta,
        meta={"url": url, "page_count": n_pages, "char_count": len(body),
              **({"pages_total": total} if total != n_pages else {}),
              **({"cut_by_time": True} if cut_by_time else {})},
    )
