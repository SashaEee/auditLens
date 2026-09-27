"""Статический контракт вкладки «Уязвимости» после перехода на макет AuditLens.

Принципы прежних контрактов модуля (фокус, мишени, диалоги, прокрутка, высота
от iframe, контраст заливок) перенесены на новые компоненты; проверки, которые
описывали удалённые боковой чат, таблицу и CSV, заменены браузерными тестами
test_vulnerabilities_redesign_ui.py.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "bank_audit" / "loophole" / "static"
JSX = (STATIC / "loophole.jsx").read_text(encoding="utf-8")
CSS_RAW = (STATIC / "loophole.css").read_text(encoding="utf-8")
CSS = re.sub(r"/\*.*?\*/", "", CSS_RAW, flags=re.S)
NEW_CSS = CSS[:CSS.index(".lp-sources-surface")] if ".lp-sources-surface" in CSS else CSS


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _rule(selector: str) -> str:
    """Тело первого правила, список селекторов которого содержит selector."""
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        if selector in [s.strip() for s in selectors.split(",")]:
            return body
    raise AssertionError(f"нет правила {selector}")


def _px(body: str, prop: str) -> int | None:
    match = re.search(rf"(?<![-\w]){prop}\s*:\s*(\d+)px", body)
    return int(match.group(1)) if match else None


def test_root_height_comes_from_iframe_and_root_never_hides_overflow():
    """Высота корня — от геометрии iframe; корень не режет содержимое."""
    assert "min-height: 100%" in _rule("#loophole-root")
    assert "height: 100%" in _rule("html")
    for selector in ("html", "body", "#loophole-root", ".lp-app"):
        try:
            body = _rule(selector)
        except AssertionError:
            continue
        assert "overflow: hidden" not in body
    # vh — только у ограничителей липких панелей внутри документа iframe.
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", NEW_CSS):
        if "vh" in body:
            assert re.search(r"(max-height|min-height)\s*:\s*calc\(100vh", body), selectors


def test_horizontal_scroll_only_inside_strips_and_markdown():
    """Горизонтально прокручиваются только полоса вкладок, сегменты и широкий markdown."""
    scrollers = {
        selectors.strip()
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", NEW_CSS)
        if re.search(r"\boverflow(?:-x)?\s*:\s*(?:auto|scroll)\b", body)
    }
    assert scrollers == {".lp-tabs-l", ".lp-seg", ".lp-md-table-wrap", ".lp-safe-markdown pre"}


def test_every_layer_traps_focus_and_every_modal_is_labelled():
    jsx = _norm(JSX)
    for layer in ("useFocusLayer(!!verdictModal,", "useFocusLayer(!!deleteConfirm,",
                  "useFocusLayer(researchDeleteConfirm,", "useFocusLayer(accessOpen,"):
        assert _norm(layer) in jsx, layer
    for title in ("lp-verdict-title", "lp-research-delete-title", "lp-confirm-title", "lp-access-title"):
        assert f'aria-labelledby="{title}"' in JSX
        assert f'id="{title}"' in JSX
    assert JSX.count('aria-modal="true"') >= 4
    # Всплывающие панели — немодальные диалоги с именем.
    for label in ("Банки", "Аудит-дела", "Как читать вердикт"):
        assert f'role="dialog" aria-label="{label}"' in JSX


def test_backdrops_are_named_native_buttons_outside_tab_order():
    for match in re.finditer(r'<button type="button" className="lp-scrim"(.*?)/>', JSX, re.S):
        attrs = match.group(1)
        assert 'aria-label="' in attrs and "tabIndex={-1}" in attrs
    assert JSX.count('className="lp-scrim"') >= 3


def test_focus_ring_is_graphite_and_hidden_outlines_are_replaced():
    assert "outline: 2px solid var(--select)" in _rule(":focus-visible")
    # Поля без собственного outline показывают фокус рамкой и ореолом.
    for selector in (".lp-search:focus-within", ".lp-cmp:focus-within", ".lp-dcom:focus",
                     ".lp-period input:focus", ".lp-grant input:focus",
                     ".lp-question-other textarea:focus", ".lp-pop-new input:focus"):
        assert "border-color: var(--select)" in _rule(selector), selector
    assert "outline: 2px solid var(--select)" in _rule(".lp-opt:has(input:focus-visible)")


def test_interactive_targets_are_at_least_28px():
    sizes = {
        ".lp-btn": "height", ".lp-btn-sm": "height", ".lp-btn-text": "height",
        ".lp-tab": "height", ".lp-seg-btn": "height", ".lp-dd": "height", ".lp-icb": "height",
        ".lp-hi-del": "height", ".lp-send": "height", ".lp-toast-action": "height",
        ".lp-sg": "height", ".lp-pi": "min-height", ".lp-opt": "min-height",
        ".lp-dopt": "min-height", ".lp-c-box": "height",
    }
    for selector, prop in sizes.items():
        value = _px(_rule(selector), prop)
        assert value is not None and value >= 28, f"{selector}: {prop}={value}"
    # Крестик чипа фильтра маленький визуально, но мишень расширена псевдоэлементом.
    assert "inset: -4px" in _rule(".lp-fchip button::after")


def test_solid_fills_keep_text_readable():
    assert "background: var(--ink)" in _rule(".lp-btn-primary") and "color: var(--paper)" in _rule(".lp-btn-primary")
    assert "background: var(--ink)" in _rule(".lp-send") and "color: var(--paper)" in _rule(".lp-send")
    assert "background: var(--accent)" in _rule(".lp-btn-danger")
    assert "color: var(--on-danger-solid)" in _rule(".lp-btn-danger")
    assert "background: var(--accent)" in _rule(".lp-toast-error")
    assert "color: var(--on-danger-solid)" in _rule(".lp-toast-error")


def test_tool_progress_shows_counts_not_payloads():
    """Ход исследования — счётчики шагов; аргументы и ответы инструментов не выводятся."""
    assert "Чтение страниц" in JSX and "Поиск источников" in JSX
    assert "ev.args" not in JSX and "ev.result" not in JSX
    assert "payload.args" not in JSX and "payload.result" not in JSX


def test_internal_trust_never_reaches_the_interface():
    assert "trust_score" not in JSX
    assert "Надёжность" not in JSX


def test_summary_is_requested_only_for_findings():
    jsx = _norm(JSX)
    assert _norm('const POSITIVE_KINDS = new Set(["vulnerability", "fraud_scheme"]);') in jsx
    assert _norm("if (!POSITIVE_KINDS.has(kind)) return;") in jsx


def test_loophole_js_is_built_from_current_jsx():
    """Страница грузит предсобранный loophole.js: он должен совпадать с исходником."""
    import hashlib

    built = (STATIC / "loophole.js").read_text(encoding="utf-8")
    sha = hashlib.sha256(JSX.encode("utf-8")).hexdigest()
    assert f"sha256 {sha}" in built.splitlines()[0], (
        "loophole.jsx изменён без пересборки: node scripts/build_loophole_js.mjs")
    html = (STATIC / "loophole.html").read_text(encoding="utf-8")
    assert '<script src="/static/loophole/loophole.js"></script>' in html
    assert "babel.min.js" not in html and "text/babel" not in html
