"""Контракт markdown-рендерера отчётов «Лазеек» (UI-зеркало — SafeMarkdown)."""
from __future__ import annotations

from bank_audit.loophole.markdown_render import render_markdown_html


def test_headings_lists_and_inline_markup_render():
    html = render_markdown_html(
        "# Раздел\n"
        "Текст с **жирным**, *курсивом*, ~~зачёркнутым~~ и `кодом`.\n"
        "- пункт один\n"
        "- пункт два\n"
        "1. первый\n"
        "2. второй"
    )

    assert "<h3>Раздел</h3>" in html
    assert "<strong>жирным</strong>" in html
    assert "<em>курсивом</em>" in html
    assert "<s>зачёркнутым</s>" in html
    assert "<code>кодом</code>" in html
    assert "<ul><li>пункт один</li><li>пункт два</li></ul>" in html
    assert "<ol><li>первый</li><li>второй</li></ol>" in html


def test_links_render_only_for_http_and_stay_escaped():
    html = render_markdown_html(
        "[Правила банка](https://bank.example/rules) и [плохое](javascript:alert(1))"
    )

    assert '<a href="https://bank.example/rules" target="_blank" rel="noopener noreferrer">' in html
    assert "javascript:alert(1)" in html
    assert 'href="javascript:' not in html


def test_tables_blockquotes_hr_and_code_fences_render():
    html = render_markdown_html(
        "| Банк | Комиссия |\n"
        "| --- | --- |\n"
        "| А | **1%** |\n"
        "> дословная цитата\n"
        "---\n"
        "```\n"
        "сырой <b>текст</b> **без разметки**\n"
        "```"
    )

    assert "<table>" in html
    assert "<th>Банк</th>" in html
    assert "<td><strong>1%</strong></td>" in html
    assert "<blockquote>дословная цитата</blockquote>" in html
    assert "<hr>" in html
    assert "<pre><code>сырой &lt;b&gt;текст&lt;/b&gt; **без разметки**</code></pre>" in html


def test_untrusted_html_is_escaped_inside_markup():
    html = render_markdown_html("**<img src=x onerror=alert(1)>**\n\n<script>alert(1)</script>")

    assert "<img src=x onerror=alert(1)>" not in html
    assert "<script>alert(1)</script>" not in html
    assert "<strong>&lt;img src=x onerror=alert(1)&gt;</strong>" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_empty_and_unclosed_fence_do_not_lose_text():
    assert render_markdown_html("") == ""
    assert render_markdown_html(None) == ""
    html = render_markdown_html("```\nбез закрывающего fence")
    assert "без закрывающего fence" in html
