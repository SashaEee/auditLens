"""Markdown → безопасный HTML для отчётов «Лазеек» (PDF-экспорт исследования).

Подмножество markdown — зеркало JS-рендерера SafeMarkdown
(``static/loophole.jsx``): заголовки, списки, таблицы, цитаты, код-блоки,
ссылки, inline-разметка. Весь вход сначала экранируется: в markdown попадает
недоверенный вывод LLM, сырой HTML исполняться не должен (stored XSS).
Ссылки принимаются только со схемой http(s).
"""
from __future__ import annotations

import re
from html import escape

_WORD_CHARS = "A-Za-zА-Яа-яЁё0-9_"
_RE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_RE_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*")
_RE_BOLD_UNDER = re.compile(rf"(^|[^{_WORD_CHARS}])__([^_]+?)__(?![A-Za-zА-Яа-яЁё0-9])")
_RE_ITALIC_STAR = re.compile(r"\*(.+?)\*")
_RE_ITALIC_UNDER = re.compile(rf"(^|[^{_WORD_CHARS}])_([^_]+?)_(?![A-Za-zА-Яа-яЁё0-9])")
_RE_CODE = re.compile(r"`([^`]+)`")
_RE_STRIKE = re.compile(r"~~(.+?)~~")
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_RE_OL_ITEM = re.compile(r"^\d+\.\s+(.+)$")
_RE_UL_ITEM = re.compile(r"^[-*•]\s+(.+)$")
_RE_QUOTE = re.compile(r"^>\s?(.*)$")
_RE_HR = re.compile(r"^---+$")
_RE_FENCE = re.compile(r"^\s*```")
_RE_TABLE_SEPARATOR = re.compile(r"^[-:\s|]+$")


def render_inline(text: str) -> str:
    """Экранирование + inline-markdown (ссылки, выделение, код)."""
    out = escape(text, quote=False)
    out = _RE_LINK.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        out,
    )
    out = _RE_BOLD_STAR.sub(r"<strong>\1</strong>", out)
    out = _RE_BOLD_UNDER.sub(r"\1<strong>\2</strong>", out)
    out = _RE_ITALIC_STAR.sub(r"<em>\1</em>", out)
    out = _RE_ITALIC_UNDER.sub(r"\1<em>\2</em>", out)
    out = _RE_CODE.sub(r"<code>\1</code>", out)
    out = _RE_STRIKE.sub(r"<s>\1</s>", out)
    return out


def render_markdown_html(text: object) -> str:
    """Рендерит markdown-текст в безопасный HTML-фрагмент.

    Пустой вход даёт пустую строку — заполнитель («—») решает вызывающий код.
    """
    lines = str(text or "").splitlines()
    out: list[str] = []
    list_items: list[str] = []
    list_ordered = False
    table_head: list[str] | None = None
    table_rows: list[list[str]] = []
    quote_lines: list[str] = []
    code_lines: list[str] | None = None

    def flush_list() -> None:
        nonlocal list_items, list_ordered
        if not list_items:
            return
        tag = "ol" if list_ordered else "ul"
        items = "".join(f"<li>{render_inline(item)}</li>" for item in list_items)
        out.append(f"<{tag}>{items}</{tag}>")
        list_items = []
        list_ordered = False

    def flush_table() -> None:
        nonlocal table_head, table_rows
        if table_head is None:
            return
        head = "".join(f"<th>{render_inline(cell)}</th>" for cell in table_head)
        rows = "".join(
            "<tr>" + "".join(f"<td>{render_inline(cell)}</td>" for cell in row) + "</tr>"
            for row in table_rows
        )
        out.append(f'<div class="md-table-wrap"><table><thead><tr>{head}</tr></thead>'
                   f"<tbody>{rows}</tbody></table></div>")
        table_head = None
        table_rows = []

    def flush_quote() -> None:
        nonlocal quote_lines
        if not quote_lines:
            return
        body = "<br>".join(render_inline(line) for line in quote_lines)
        out.append(f"<blockquote>{body}</blockquote>")
        quote_lines = []

    def flush_blocks() -> None:
        flush_list()
        flush_table()
        flush_quote()

    for line in lines:
        if code_lines is not None:
            if _RE_FENCE.match(line):
                out.append(f"<pre><code>{escape(chr(10).join(code_lines), quote=False)}</code></pre>")
                code_lines = None
            else:
                code_lines.append(line)
            continue
        if _RE_FENCE.match(line):
            flush_blocks()
            code_lines = []
            continue

        quote = _RE_QUOTE.match(line)
        if quote:
            flush_list()
            flush_table()
            quote_lines.append(quote.group(1))
            continue
        flush_quote()

        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.split("|")][1:-1]
            if _RE_TABLE_SEPARATOR.match(stripped.replace("|", "")):
                continue
            flush_list()
            if table_head is None:
                table_head = cells
            else:
                table_rows.append(cells)
            continue
        flush_table()

        heading = _RE_HEADING.match(stripped)
        if heading:
            flush_list()
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            continue
        if _RE_HR.match(stripped):
            flush_list()
            out.append("<hr>")
            continue

        ol_item = _RE_OL_ITEM.match(line)
        if ol_item:
            if list_items and not list_ordered:
                flush_list()
            list_ordered = True
            list_items.append(ol_item.group(1))
            continue
        ul_item = _RE_UL_ITEM.match(line)
        if ul_item:
            if list_items and list_ordered:
                flush_list()
            list_ordered = False
            list_items.append(ul_item.group(1))
            continue
        flush_list()

        if not stripped:
            continue
        out.append(f"<p>{render_inline(line)}</p>")

    if code_lines is not None:
        # Незакрытый fence: отдаём накопленное как код, не теряем текст.
        out.append(f"<pre><code>{escape(chr(10).join(code_lines), quote=False)}</code></pre>")
    flush_blocks()
    return "\n".join(out)
