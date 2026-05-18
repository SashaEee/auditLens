"""Chunker: разрезает длинные тексты на семантические chunk'и для RAG.

Стратегия:
  • Размер целевого chunk'а — ~500 токенов (по rough heuristic 1 token ≈ 3 char)
  • Overlap 80 токенов между соседями — сохраняет контекст на границах
  • Уважает структуру: не режем посредине предложения, по возможности
    границу проводим по абзацу/заголовку
  • Сохраняем breadcrumb headings_path для UI

Не используем tiktoken/spacy ради простоты — chars/3 даёт оценку ±15%, что
для retrieval-качества достаточно.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Iterable


@dataclass
class Chunk:
    text:           str
    idx:            int                              # порядковый номер
    tokens:         int                              # rough estimate
    headings_path:  str | None = None                # "Раздел > Подраздел"
    char_start:     int = 0
    char_end:       int = 0


_TOKEN_RATIO = 3                                     # 1 token ≈ 3 chars (ru)
DEFAULT_CHUNK_TOKENS = 500
DEFAULT_OVERLAP_TOKENS = 80
MIN_CHUNK_CHARS = 80                                 # отбрасываем огрызки

_PARA_RE = re.compile(r"\n\s*\n")
_SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[А-ЯA-Z])")
_WS_RE   = re.compile(r"[ \t]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)  # markdown


def _tokens(text: str) -> int:
    return max(1, len(text) // _TOKEN_RATIO)


def _normalize_whitespace(text: str) -> str:
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    text: str,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    base_headings: str | None = None,
) -> list[Chunk]:
    """Режет text на chunks. Возвращает список Chunk.
    base_headings — префикс пути (например "Сбербанк > Переводы").
    """
    if not text or not text.strip():
        return []

    text = _normalize_whitespace(text)
    chunk_chars = chunk_tokens * _TOKEN_RATIO
    overlap_chars = overlap_tokens * _TOKEN_RATIO

    # Сначала разбиваем по абзацам — каждый абзац атомарен (если влезает).
    # Если параграф длиннее chunk_chars — режем его по предложениям.
    paragraphs = _PARA_RE.split(text)
    units: list[str] = []                            # атомарные единицы для упаковки
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(p) <= chunk_chars:
            units.append(p)
        else:
            # Длинный абзац → бьём по предложениям
            sents = _SENT_RE.split(p)
            for s in sents:
                s = s.strip()
                if len(s) <= chunk_chars:
                    units.append(s)
                else:
                    # Сверхдлинное предложение — bytes-режем
                    for off in range(0, len(s), chunk_chars):
                        units.append(s[off:off + chunk_chars])

    # Жадно упаковываем units в chunks с overlap
    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_len = 0
    pos = 0
    idx = 0
    for u in units:
        u_len = len(u)
        if cur_len + u_len + 2 > chunk_chars and cur:
            joined = "\n\n".join(cur)
            chunks.append(Chunk(
                text=joined, idx=idx, tokens=_tokens(joined),
                headings_path=base_headings,
                char_start=pos, char_end=pos + len(joined),
            ))
            idx += 1
            # Overlap: оставляем последние overlap_chars
            tail_chars = 0
            tail_units = []
            for back in reversed(cur):
                tail_chars += len(back) + 2
                tail_units.insert(0, back)
                if tail_chars >= overlap_chars:
                    break
            cur = tail_units
            cur_len = sum(len(x) + 2 for x in cur)
            pos += len(joined) - sum(len(x) + 2 for x in cur)
        cur.append(u)
        cur_len += u_len + 2

    if cur:
        joined = "\n\n".join(cur)
        if len(joined) >= MIN_CHUNK_CHARS:
            chunks.append(Chunk(
                text=joined, idx=idx, tokens=_tokens(joined),
                headings_path=base_headings,
                char_start=pos, char_end=pos + len(joined),
            ))

    return chunks


def chunk_with_headings(text: str, **kwargs) -> list[Chunk]:
    """Если в тексте markdown-заголовки — обновляет headings_path для каждого
    chunk. Полезно для PDF/HTML после парсинга — даёт breadcrumb в UI.
    """
    base_headings = kwargs.pop("base_headings", None)

    # Простая стратегия: разбиваем на секции по заголовкам, chunk'aем каждую
    # секцию, добавляем path заголовка.
    sections: list[tuple[str | None, str]] = []  # (heading_path, section_body)
    last_headings: list[str] = []
    last_pos = 0
    for m in _HEADING_RE.finditer(text):
        before = text[last_pos:m.start()].strip()
        if before:
            sections.append((" > ".join(last_headings) if last_headings else None,
                             before))
        level = len(m.group(1))
        title = m.group(2).strip()
        # Обрезаем стек заголовков до текущего уровня
        last_headings = last_headings[:level - 1]
        last_headings.append(title)
        last_pos = m.end()
    tail = text[last_pos:].strip()
    if tail:
        sections.append((" > ".join(last_headings) if last_headings else None, tail))

    if not sections:
        return chunk_text(text, base_headings=base_headings, **kwargs)

    out: list[Chunk] = []
    global_idx = 0
    for heads, body in sections:
        full_path = base_headings
        if heads:
            full_path = f"{base_headings} > {heads}" if base_headings else heads
        for c in chunk_text(body, base_headings=full_path, **kwargs):
            c.idx = global_idx
            global_idx += 1
            out.append(c)
    return out
