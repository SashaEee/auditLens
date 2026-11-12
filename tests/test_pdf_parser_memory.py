"""Разбор PDF не раздувает память процесса.

12.11.2026 отчёты о жалобах (они читают PDF Банка России) за полторы минуты
доводили процесс до 2,5 ГБ: pdfplumber держал разобранные страницы до закрытия
документа, несколько документов разбирались разом, память системе не
возвращалась. Замер на обзоре в 100 страниц: 1345 МБ → 97 МБ, текст тот же.
"""
from __future__ import annotations

import threading
import time

import pdfplumber

from bank_audit.rag.parsers import pdf_parser as P


def _pdf(pages: list[list[str]]) -> bytes:
    """Минимальный PDF: по странице на список строк (латиница, Helvetica)."""
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = 1 + 2 * len(pages) + 1
    kids = []
    for lines in pages:
        ops = ["BT /F1 12 Tf 72 760 Td 16 TL"] + [f"({ln}) Tj T*" for ln in lines] + ["ET"]
        stream = "\n".join(ops).encode("latin-1")
        c = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                        b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (pages_id, c, font)))
    assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
               + b"] /Count %d >>" % len(kids)) == pages_id
    cat = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, cat, xref)
    return bytes(out)


DOC = _pdf([["Annual review", f"Body of page {i}", "Bank of Russia 2026"] for i in range(1, 7)])


def test_pages_are_released_one_by_one(monkeypatch):
    """К чтению следующей страницы предыдущая уже закрыта — память не копится."""
    held = []
    real = pdfplumber.page.Page.extract_text

    def spy(self, *a, **kw):
        held.append(sum(1 for p in self.pdf.pages
                        if p is not self and (hasattr(p, "_layout") or hasattr(p, "_objects"))))
        return real(self, *a, **kw)

    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", spy)
    doc = P.parse_pdf(DOC, "u")
    assert len(held) == 6 and max(held) == 0
    assert doc.meta["page_count"] == 6 and "pages_total" not in doc.meta


def test_text_and_repeating_header_footer_are_as_before():
    doc = P.parse_pdf(DOC, "u")
    assert doc.doc_type == "pdf" and doc.meta["url"] == "u"
    assert [f"## Страница {i}" in doc.text for i in range(1, 7)] == [True] * 6
    assert "Body of page 3" in doc.text and doc.meta["char_count"] == len(doc.text)
    # шапка и подвал, одинаковые на всех страницах, убраны
    assert "Annual review" not in doc.text and "Bank of Russia" not in doc.text


def test_time_limit_returns_what_was_read(monkeypatch):
    ticks = iter(range(0, 1000, 10))                 # каждое обращение к часам — +10 с
    monkeypatch.setattr(P.time, "monotonic", lambda: next(ticks))
    doc = P.parse_pdf(DOC, "u", max_seconds=15)
    assert doc.meta["cut_by_time"] is True and doc.meta["pages_total"] == 6
    assert 1 <= doc.meta["page_count"] < 6 and "Body of page 1" in doc.text
    # без предела документ читается целиком
    monkeypatch.setattr(P.time, "monotonic", time.monotonic)
    assert P.parse_pdf(DOC, "u").meta["page_count"] == 6


def test_no_more_than_two_documents_at_once(monkeypatch):
    now, peak, lock = [0], [0], threading.Lock()
    real = P._parse_pdf

    def slow(content, url, max_seconds):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        try:
            time.sleep(0.05)
            return real(content, url, max_seconds)
        finally:
            with lock:
                now[0] -= 1

    monkeypatch.setattr(P, "_parse_pdf", slow)
    ths = [threading.Thread(target=P.parse_pdf, args=(DOC, "u")) for _ in range(6)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert peak[0] == 2


def test_big_document_asks_to_return_memory(monkeypatch):
    calls = []
    monkeypatch.setattr(P, "_trim", lambda: calls.append(1))
    P.parse_pdf(DOC, "u")
    assert not calls                                  # шесть страниц — мелочь
    monkeypatch.setattr(P, "_TRIM_FROM_PAGES", 5)
    P.parse_pdf(DOC, "u")
    assert calls == [1]
