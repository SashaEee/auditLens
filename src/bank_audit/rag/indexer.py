"""Document indexer: содержит pipeline ingest_document_from_url.

Pipeline:
  1. fetch (HTTP/Playwright/cache)
  2. parse (HTML/PDF/XLSX/PPTX)
  3. trust scoring (sponsored detect)
  4. compute content_sha256, idempotent insert в `document`
  5. chunk text + batch embed → bulk insert в `document_chunk`
  6. (optional) detect bank from URL/text, link bank_id

Используется:
  • при холодном fetch агентом (passive enrichment)
  • из bootstrap_bank_profiles (massiv crawl топ-30)
  • вручную: POST /api/rag/ingest-url
"""
from __future__ import annotations
import hashlib, json, logging, os, re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import text

from .. import db
from . import fetcher, embedder, chunker
from .parsers import parse_auto, detect_doc_type
from .trust import (
    domain_of, is_bank_official, detect_sponsored, compute_trust,
)

log = logging.getLogger(__name__)

DEFAULT_CHUNK_TOKENS = 500
DEFAULT_CHUNK_OVERLAP = 80


@dataclass
class IngestResult:
    document_id:    int | None
    url:            str
    bank_id:        int | None
    doc_type:       str
    chunks_added:   int
    trust_score:    float
    is_sponsored:   bool
    is_new:         bool                              # False если document существовал
    skipped_reason: str | None = None


def _resolve_source(session, url: str) -> tuple[int | None, float, dict]:
    """По URL находит запись в source_trust → (source_id, base_weight, kind)."""
    d = domain_of(url)
    if not d:
        return None, 0.5, {}
    # Сначала govt/regulator whitelist (приоритет выше bank_official —
    # ЦБ/Минобороны/pravo.gov.ru — первоисточники по ставкам/льготам/НПА).
    from .trust import is_govt_official
    is_govt, govt_kind, govt_w, govt_notes = is_govt_official(url)
    if is_govt:
        row = session.execute(text("""
            SELECT source_id, weight FROM source_trust
             WHERE domain = :d AND kind = :k
        """), {"d": d, "k": govt_kind}).first()
        if row:
            return row[0], float(row[1]), {"kind": govt_kind}
        new_id = session.execute(text("""
            INSERT INTO source_trust(kind, domain, weight, notes)
            VALUES (:k, :d, :w, :n)
            ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight
            RETURNING source_id
        """), {"k": govt_kind, "d": d, "w": govt_w, "n": govt_notes}).scalar()
        return new_id, govt_w, {"kind": govt_kind}
    # Потом bank_official по whitelist
    is_bank, slug = is_bank_official(url)
    if is_bank and slug:
        # Создадим запись в source_trust если нет
        row = session.execute(text("""
            SELECT source_id, weight FROM source_trust
             WHERE domain = :d AND kind = 'bank_official'
        """), {"d": d}).first()
        if row:
            return row[0], float(row[1]), {"slug": slug}
        # auto-insert
        bank_row = session.execute(text(
            "SELECT bank_id FROM bank WHERE slug = :s"
        ), {"s": slug}).first()
        bank_id = bank_row[0] if bank_row else None
        new_id = session.execute(text("""
            INSERT INTO source_trust(kind, domain, bank_id, weight, notes)
            VALUES ('bank_official', :d, :b, 0.95, 'Auto-detected bank official')
            ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight
            RETURNING source_id
        """), {"d": d, "b": bank_id}).scalar()
        return new_id, 0.95, {"slug": slug}
    # Иначе по domain
    row = session.execute(text("""
        SELECT source_id, weight, kind FROM source_trust
         WHERE domain = :d
         ORDER BY weight DESC LIMIT 1
    """), {"d": d}).first()
    if row:
        return row[0], float(row[1]), {"kind": row[2]}
    # Неизвестный → blog с малым весом
    new_id = session.execute(text("""
        INSERT INTO source_trust(kind, domain, weight, notes)
        VALUES ('blog', :d, 0.3, 'Auto-added unknown source')
        ON CONFLICT (kind, domain) DO NOTHING
        RETURNING source_id
    """), {"d": d}).scalar()
    if not new_id:
        # Снова достаём
        row = session.execute(text("""
            SELECT source_id FROM source_trust WHERE kind='blog' AND domain=:d
        """), {"d": d}).first()
        new_id = row[0] if row else None
    return new_id, 0.3, {"kind": "blog"}


def _resolve_bank(session, url: str, bank_slug_hint: str | None = None) -> int | None:
    """Достаёт bank_id по hint, либо по domain (если bank_official)."""
    if bank_slug_hint:
        r = session.execute(text("SELECT bank_id FROM bank WHERE slug=:s"),
                            {"s": bank_slug_hint}).first()
        if r:
            return r[0]
    is_bank, slug = is_bank_official(url)
    if is_bank and slug:
        r = session.execute(text("SELECT bank_id FROM bank WHERE slug=:s"),
                            {"s": slug}).first()
        if r:
            return r[0]
    return None


def ingest_document_from_url(
    url: str, *,
    bank_slug_hint: str | None = None,
    prefer_browser: bool = False,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    browser=None,
) -> IngestResult:
    """Основной pipeline: URL → проиндексированный document с chunks."""

    fr = fetcher.fetch(url, prefer_browser=prefer_browser, browser=browser)
    if not fr.content:
        reason = "captcha" if fr.captcha else "fetch_failed"
        return IngestResult(document_id=None, url=url, bank_id=None,
                            doc_type="unknown", chunks_added=0,
                            trust_score=0.0, is_sponsored=False,
                            is_new=False, skipped_reason=reason)

    # Парсим
    parsed = parse_auto(fr.content, url=fr.final_url, content_type=fr.content_type)
    if parsed.is_empty():
        return IngestResult(document_id=None, url=url, bank_id=None,
                            doc_type=parsed.doc_type, chunks_added=0,
                            trust_score=0.0, is_sponsored=False,
                            is_new=False, skipped_reason="empty_after_parse")

    sponsored, _ = detect_sponsored(fr.final_url, parsed.text)
    sha = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()

    with db.session() as s:
        source_id, base_weight, _ = _resolve_source(s, fr.final_url)
        bank_id = _resolve_bank(s, fr.final_url, bank_slug_hint)
        trust = compute_trust(base_weight, fr.final_url, parsed.text)

        # Idempotent insert
        existing = s.execute(text("""
            SELECT document_id FROM document
             WHERE url = :u AND content_sha256 = :sha
        """), {"u": fr.final_url, "sha": sha}).first()
        if existing:
            return IngestResult(document_id=existing[0], url=fr.final_url,
                                bank_id=bank_id, doc_type=parsed.doc_type,
                                chunks_added=0, trust_score=trust,
                                is_sponsored=sponsored, is_new=False,
                                skipped_reason="duplicate")

        doc_id = s.execute(text("""
            INSERT INTO document(
                source_id, bank_id, url, doc_type, title,
                headings_path, content_text, content_sha256,
                fetched_at, trust_score, is_sponsored, bytes
            )
            VALUES (:src, :b, :u, CAST(:dt AS doc_type), :t,
                    :hp, :ct, :sha,
                    now(), :tr, :sp, :bt)
            RETURNING document_id
        """), {
            "src": source_id, "b": bank_id, "u": fr.final_url,
            "dt": parsed.doc_type, "t": parsed.title or fr.final_url[:200],
            "hp": parsed.headings_path,
            "ct": parsed.text[:1_000_000],
            "sha": sha, "tr": trust, "sp": sponsored,
            "bt": len(fr.content),
        }).scalar()

        # Если sponsored или невалидный — chunks НЕ добавляем (нет смысла индексировать)
        if sponsored or trust < 0.05:
            return IngestResult(document_id=doc_id, url=fr.final_url,
                                bank_id=bank_id, doc_type=parsed.doc_type,
                                chunks_added=0, trust_score=trust,
                                is_sponsored=sponsored, is_new=True,
                                skipped_reason="sponsored_or_low_trust")

    # Chunk + embed (вне транзакции — embedder может быть медленным первый раз)
    base_path = f"{bank_slug_hint or ''} > {parsed.title or ''}".strip(" >")
    chunks = chunker.chunk_with_headings(
        parsed.text,
        chunk_tokens=chunk_tokens, overlap_tokens=chunk_overlap,
        base_headings=base_path or None,
    )
    if not chunks:
        return IngestResult(document_id=doc_id, url=fr.final_url, bank_id=bank_id,
                            doc_type=parsed.doc_type, chunks_added=0,
                            trust_score=trust, is_sponsored=sponsored,
                            is_new=True, skipped_reason="no_chunks")

    embeddings = embedder.embed_batch([c.text for c in chunks])

    with db.session() as s:
        for c, vec in zip(chunks, embeddings):
            s.execute(text("""
                INSERT INTO document_chunk(document_id, idx, text, tokens,
                                            headings_path, embedding)
                VALUES (:d, :i, :t, :tk, :hp, CAST(:e AS vector))
                ON CONFLICT (document_id, idx) DO NOTHING
            """), {
                "d": doc_id, "i": c.idx, "t": c.text, "tk": c.tokens,
                "hp": c.headings_path,
                "e": str(vec),
            })

    log.info("ingest %s: doc=%s, chunks=%s, trust=%.2f, doc_type=%s",
             fr.final_url[:80], doc_id, len(chunks), trust, parsed.doc_type)

    # Structured fact extraction — fire-and-forget в отдельном потоке.
    # НЕ блокирует ingest. Включается через RAG_FACT_EXTRACTION=1 (по умолчанию off).
    if (os.getenv("RAG_FACT_EXTRACTION") in ("1","true","yes")
        and trust >= 0.7 and parsed.text
        and re.search(r"\d{2}", parsed.text or "")):
        import threading
        def _bg_extract():
            try:
                from .fact_extractor import extract_and_store
                extract_and_store(doc_id, parsed.text, fr.final_url)
            except Exception as e:
                log.debug("bg fact extraction failed: %s", e)
        threading.Thread(target=_bg_extract, daemon=True).start()

    return IngestResult(document_id=doc_id, url=fr.final_url, bank_id=bank_id,
                        doc_type=parsed.doc_type, chunks_added=len(chunks),
                        trust_score=trust, is_sponsored=sponsored,
                        is_new=True)


def discover_outbound_links(content: bytes, base_url: str,
                              whitelist_domains: list[str]) -> list[str]:
    """Из HTML извлекает <a href=> на whitelist-домены — для link-expansion.
    Возвращает дедуплицированный список абсолютных URLs.
    """
    if not content:
        return []
    try:
        from selectolax.parser import HTMLParser
        from urllib.parse import urljoin, urlparse
        text = content.decode("utf-8", errors="ignore")
        tree = HTMLParser(text)
        seen = set()
        out: list[str] = []
        wl_set = set(whitelist_domains)
        for a in tree.css("a[href]"):
            href = (a.attributes.get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            full = urljoin(base_url, href)
            if not full.startswith("http"):
                continue
            try:
                d = (urlparse(full).hostname or "").replace("www.", "").lower()
            except Exception:
                continue
            if not any(d == w or d.endswith("." + w) for w in wl_set):
                continue
            # отбрасываем якоря/паги на тот же URL
            if full == base_url:
                continue
            if full in seen:
                continue
            seen.add(full)
            out.append(full)
            if len(out) >= 25:
                break
        return out
    except Exception as e:
        log.debug("discover_outbound_links failed: %s", e)
        return []


def ingest_with_link_expansion(url: str, *,
                                  bank_slug_hint: str | None = None,
                                  prefer_browser: bool = False,
                                  expand_depth: int = 1,
                                  max_outbound: int = 3,
                                  whitelist_domains: list[str] | None = None,
                                  browser=None) -> IngestResult:
    """Ingest URL + рекурсивно вытащить ссылки на whitelist-домены, ингест top-N.
    Глубина 1 = индексируем основной + до max_outbound линкованных. Без cycles.
    """
    primary = ingest_document_from_url(
        url, bank_slug_hint=bank_slug_hint,
        prefer_browser=prefer_browser, browser=browser,
    )
    if expand_depth <= 0 or not primary.document_id:
        return primary

    # Достаём raw для парсинга outbound ссылок
    try:
        from . import fetcher
        fr = fetcher.fetch(primary.url, prefer_browser=prefer_browser, browser=browser)
        wl = whitelist_domains or []
        outbound = discover_outbound_links(fr.content, primary.url, wl)
    except Exception as e:
        log.info("link expansion fetch failed: %s", e)
        outbound = []

    if not outbound:
        return primary

    log.info("link-expansion %s: %s outbound on whitelist (top %s)",
             primary.url[:60], len(outbound), max_outbound)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    expanded = 0
    def _do(u):
        try:
            r = ingest_document_from_url(u, bank_slug_hint=bank_slug_hint)
            return 1 if r.is_new else 0
        except Exception:
            return 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_do, u) for u in outbound[:max_outbound]]
        for f in as_completed(futures, timeout=60):
            try:
                expanded += f.result()
            except Exception:
                pass
    log.info("link-expansion %s: +%s expanded docs", primary.url[:60], expanded)
    return primary
