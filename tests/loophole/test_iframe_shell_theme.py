"""Story 1.2 — единая оболочка iframe и синхронизация темы.

Спека: docs/loophole/bmad/implementation-artifacts/
spec-1-2-единая-оболочка-iframe-и-синхронизация-темы.md

Фронт без сборки и без UI-стенда — проверки текстовые (по образцу
test_refresh_button.py / test_static_bust.py) плюс вычислительная проверка
WCAG-контраста токенов (чистый math, без зависимостей).

Покрытые критерии приёмки:
- только саморазмещённые React/ReactDOM/Babel/шрифты из /static/vendor/,
  без внешних CDN и локальных hex-палитр;
- синхронизация html.dark с родительским документом через MutationObserver,
  при прямом открытии — prefers-color-scheme;
- все поверхности на единых токенах AuditLens, контраст текста >= 4.5:1.
"""

import math
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bank_audit"
LOOPHOLE_HTML = SRC / "loophole" / "static" / "loophole.html"
LOOPHOLE_CSS = SRC / "loophole" / "static" / "loophole.css"
INDEX_HTML = SRC / "web" / "static" / "index.html"

# Токены визуальной системы AuditLens (index.html основного сайта).
AUDITLENS_TOKENS = (
    "--paper", "--paper-2", "--surface",
    "--ink", "--ink-2", "--ink-3", "--ink-4",
    "--hair", "--hair-2",
    "--accent", "--accent-soft", "--pos", "--warn", "--neg",
)


def _html() -> str:
    return LOOPHOLE_HTML.read_text(encoding="utf-8")


def _css() -> str:
    return LOOPHOLE_CSS.read_text(encoding="utf-8")


def _main_jsx() -> str:
    return (SRC / "web" / "static" / "app.jsx").read_text(encoding="utf-8")


def test_loophole_page_fills_the_main_workspace():
    """Iframe «Лазеек» берёт высоту рабочей области, а не фиксированный viewport-calc."""
    jsx = _norm(_main_jsx())
    shell_css = INDEX_HTML.read_text(encoding="utf-8")

    assert 'className="surface loophole-page"' in _main_jsx()
    assert 'height:"100%"' in jsx
    assert 'height:"calc(100vh-120px)"' not in jsx
    assert ".content:has(.loophole-host--active)" in shell_css


_CSS_NAMED_COLORS = frozenset(
    ["aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige", "bisque", "black", "blanchedalmond", "blue", "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse", "chocolate", "coral", "cornflowerblue", "cornsilk", "crimson", "cyan", "darkblue", "darkcyan", "darkgoldenrod", "darkgray", "darkgreen", "darkgrey", "darkkhaki", "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon", "darkseagreen", "darkslateblue", "darkslategray", "darkslategrey", "darkturquoise", "darkviolet", "deeppink", "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick", "floralwhite", "forestgreen", "fuchsia", "gainsboro", "ghostwhite", "gold", "goldenrod", "gray", "green", "greenyellow", "grey", "honeydew", "hotpink", "indianred", "indigo", "ivory", "khaki", "lavender", "lavenderblush", "lawngreen", "lemonchiffon", "lightblue", "lightcoral", "lightcyan", "lightgoldenrodyellow", "lightgray", "lightgreen", "lightgrey", "lightpink", "lightsalmon", "lightseagreen", "lightskyblue", "lightslategray", "lightslategrey", "lightsteelblue", "lightyellow", "lime", "limegreen", "linen", "magenta", "maroon", "mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple", "mediumseagreen", "mediumslateblue", "mediumspringgreen", "mediumturquoise", "mediumvioletred", "midnightblue", "mintcream", "mistyrose", "moccasin", "navajowhite", "navy", "oldlace", "olive", "olivedrab", "orange", "orangered", "orchid", "palegoldenrod", "palegreen", "paleturquoise", "palevioletred", "papayawhip", "peachpuff", "peru", "pink", "plum", "powderblue", "purple", "rebeccapurple", "red", "rosybrown", "royalblue", "saddlebrown", "salmon", "sandybrown", "seagreen", "seashell", "sienna", "silver", "skyblue", "slateblue", "slategray", "slategrey", "snow", "springgreen", "steelblue", "tan", "teal", "thistle", "tomato", "turquoise", "violet", "wheat", "white", "whitesmoke", "yellow", "yellowgreen"]
)


def _component_css(css: str) -> str:
    """Исключает палитры темы, URL и комментарии из проверки компонентов."""
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    without_theme_tokens = without_comments
    for selector in (":root", "html.dark"):
        without_theme_tokens = re.sub(
            re.escape(selector) + r"\s*\{[^{}]*\}", "", without_theme_tokens
        )
    return re.sub(r"url\([^)]*\)", "", without_theme_tokens)


def _direct_component_color_literals(css: str) -> list[str]:
    """Возвращает запрещённые литералы цветов из правил компонентов.

    ``transparent`` разрешён явно: он означает отсутствие заливки, а не цвет.
    """
    component_css = _component_css(css)
    literals = re.findall(
        r"#[0-9a-fA-F]{3,8}\b|(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklch|oklab|color)\([^)]*\)",
        component_css,
    )
    named_pattern = r"(?<![\w-])(" + "|".join(_CSS_NAMED_COLORS) + r")(?![\w-])"
    literals.extend(match.group(1) for match in re.finditer(named_pattern, component_css, re.IGNORECASE))
    return literals

def _norm(s: str) -> str:
    """Схлопывает весь whitespace — сравнение не зависит от форматирования."""
    return re.sub(r"\s+", "", s)


def _blocks(text: str, selector: str) -> list[str]:
    """Тела блоков `selector { ... }` (невложенные — для палитр достаточно)."""
    return re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", text)


def _tokens(block: str) -> dict[str, str]:
    """`--name: value;` → dict; первая встреча побеждает (light-значение)."""
    out: dict[str, str] = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block):
        out.setdefault(name, value.strip())
    return out


def _palette(css: str) -> dict[str, str]:
    """Светлая палитра модуля: все :root-блоки, слитые по порядку."""
    pal: dict[str, str] = {}
    for block in _blocks(css, ":root"):
        for name, value in _tokens(block).items():
            pal.setdefault(name, value)
    return pal


def _dark_palette(css: str) -> dict[str, str]:
    """Тёмная палитра: светлая + переопределения из html.dark."""
    pal = _palette(css)
    for block in _blocks(css, "html.dark"):
        pal.update(_tokens(block))
    return pal


# ── oklch → sRGB → относительная яркость → контраст WCAG ─────────────────────

def _oklch_luminance(value: str) -> float:
    m = re.match(r"oklch\(\s*([\d.]+)%\s+([\d.]+)\s+([\d.]+)", value)
    assert m, f"ожидался oklch-токен, получено: {value!r}"
    # L в oklch задан в процентах (98.5%), формулы OKLab→sRGB ждут диапазон 0–1.
    L = float(m.group(1)) / 100
    c, h = float(m.group(2)), float(m.group(3))
    hr = math.radians(h)
    a, b = c * math.cos(hr), c * math.sin(hr)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l3, m3, s3 = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
    g = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
    b_ = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3
    return 0.2126 * r + 0.7152 * g + 0.0722 * b_


def _resolve(pal: dict[str, str], name: str) -> str:
    """Разворачивает цепочку алиасов var(--x) до конкретного значения."""
    value = pal[name]
    while True:
        m = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if not m:
            return value
        value = pal[m.group(1)]


def _contrast(pal: dict[str, str], fg: str, bg: str) -> float:
    l1 = _oklch_luminance(_resolve(pal, fg))
    l2 = _oklch_luminance(_resolve(pal, bg))
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


# ── AC1: только саморазмещённые vendor-ресурсы ────────────────────────────────

def test_vendor_scripts_selfhosted():
    """React, ReactDOM и Babel — из /static/vendor/, как у основного сайта."""
    html = _html()
    assert 'src="/static/vendor/react.min.js"' in html
    assert 'src="/static/vendor/react-dom.min.js"' in html
    assert 'src="/static/vendor/babel.min.js"' in html


def test_vendor_fonts_selfhosted():
    """Шрифты — саморазмещённый /static/vendor/fonts.css."""
    assert 'href="/static/vendor/fonts.css"' in _html()


def test_no_external_cdn():
    """Внешних CDN нет: ни в разметке, ни в стилях (unpkg, Google Fonts, @import)."""
    assert "https://" not in _html()
    css = _css()
    assert "https://" not in css
    assert "@import" not in css


# ── AC2: синхронизация темы ───────────────────────────────────────────────────

def test_theme_sync_mutation_observer_on_parent():
    """В same-origin iframe html.dark следует за родительским документом
    через MutationObserver по атрибуту class."""
    html = _norm(_html())
    assert "MutationObserver" in html
    assert _norm("window.parent.document.documentElement") in html
    assert _norm('attributeFilter:["class"]') in html
    assert _norm('classList.toggle("dark"') in html


def test_theme_sync_prefers_color_scheme_fallback():
    """При прямом открытии (или cross-origin) тема — из prefers-color-scheme."""
    html = _html()
    assert "prefers-color-scheme" in html
    # Fallback включается только когда синхронизации с родителем нет.
    assert "window.parent" in html


def test_css_has_dark_token_overrides():
    """html.dark в CSS переопределяет базовые токены (иначе синхронизация
    класса не даст визуального эффекта)."""
    dark = _blocks(_css(), "html.dark")
    assert dark, "в loophole.css нет блока html.dark — снимать/применять класс бессмысленно"
    tokens = _tokens(dark[0])
    for name in ("--paper", "--surface", "--ink", "--hair", "--accent"):
        assert name in tokens, f"html.dark не переопределяет {name}"


# ── AC3: единые токены и контраст ─────────────────────────────────────────────

def test_light_tokens_match_main_site():
    """:root модуля повторяет токены AuditLens из index.html — verbatim."""
    main = _tokens(_blocks(INDEX_HTML.read_text(encoding="utf-8"), ":root")[0])
    ours = _tokens(_blocks(_css(), ":root")[0])
    for name in AUDITLENS_TOKENS:
        assert name in ours, f"в палитре модуля нет токена {name}"
        assert _norm(ours[name]) == _norm(main[name]), (
            f"{name}: модуль {ours[name]!r} != основной сайт {main[name]!r}"
        )


def test_dark_tokens_match_main_site():
    """html.dark модуля повторяет dark-токены AuditLens из index.html."""
    main = _tokens(_blocks(INDEX_HTML.read_text(encoding="utf-8"), "html.dark")[0])
    ours = _tokens(_blocks(_css(), "html.dark")[0])
    for name in AUDITLENS_TOKENS:
        assert name in ours, f"в html.dark модуля нет токена {name}"
        assert _norm(ours[name]) == _norm(main[name]), (
            f"{name}: модуль {ours[name]!r} != основной сайт {main[name]!r}"
        )


def test_palette_blocks_have_no_hex():
    """Локальные hex-палитры запрещены: ни в :root, ни в html.dark, ни в
    алиасах наследия — никаких #rrggbb внутри палитровых блоков."""
    css = _css()
    for selector in (":root", "html.dark"):
        for block in _blocks(css, selector):
            assert "#" not in block, f"hex-цвет в блоке {selector}: {block!r}"


def _rule_text_color(css: str, selector: str) -> str:
    """`color: var(--x)` из правила `selector { ... }` — guard смотрит на ту
    цепочку, которую реально использует правило, а не на голые токены."""
    blocks = _blocks(css, selector)
    assert blocks, f"в CSS не найдено правило {selector}"
    m = re.search(r"color:\s*var\((--[\w-]+)\)", blocks[0])
    assert m, f"в правиле {selector} нет color: var(--…)"
    return m.group(1)


def test_text_contrast_floor_4_5():
    """Контраст текста >= 4.5:1 на всех поверхностях в обеих темах.

    Пары — через алиасы наследия модуля (--fg/--muted/--panel/...), чтобы
    проверять именно ту цепочку, которую реально используют правила CSS.
    """
    css = _css()
    cases = {
        "light": _palette(css),
        "dark": _dark_palette(css),
    }
    # Поверхности из критерия приёмки: таблица/модалка/toast (--panel/--bg),
    # чат (sidebar-токены, всегда тёмная поверхность).
    pairs = (
        ("--fg", "--bg"),
        ("--fg", "--panel"),
        ("--muted", "--bg"),
        ("--muted", "--panel"),
        ("--sidebar-fg", "--sidebar-bg"),
        ("--sidebar-fg", "--sidebar-input"),
        ("--sidebar-muted", "--sidebar-bg"),
        ("--on-user-bubble", "--user-bubble"),
    )
    for theme, pal in cases.items():
        for fg, bg in pairs:
            ratio = _contrast(pal, fg, bg)
            assert ratio >= 4.5, (
                f"{theme}: {fg} на {bg} = {ratio:.2f}:1 — ниже порога 4.5:1"
            )
        # Семантический текст на подложке --paper-2 (раскрытый контент записи):
        # quality-ревью story 1.2 нашло там нарушение AC3 в light-теме
        # (--warn = 3.13:1, --accent = 4.27:1). Токены verbatim, поэтому guard
        # проверяет цвет, который фактически назначен правилом.
        for selector in (".lp-content-note", ".lp-content-head a"):
            fg = _rule_text_color(css, selector)
            ratio = _contrast(pal, fg, "--paper-2")
            assert ratio >= 4.5, (
                f"{theme}: {selector} ({fg}) на --paper-2 = {ratio:.2f}:1 — "
                f"ниже порога 4.5:1"
            )

def test_component_rules_use_centralized_color_tokens():
    """Компоненты не вводят локальную hex-палитру вне токенов темы.

    Hex в URL-фрагменте допустим: это часть адреса ресурса, а не CSS-цвет.
    Все визуальные цвета правил компонентов должны ссылаться на токены
    AuditLens через ``var(--...)``. Значение ``transparent`` допускается как
    семантическое отсутствие заливки и не является локальным цветом.
    """
    literals = _direct_component_color_literals(_css())
    assert not literals, f"прямые цветовые литералы вне токенов: {literals}"

def test_component_color_guard_detects_modern_function_bypasses():
    """Прямые hwb(), lab() и lch() в компоненте не обходят guard."""
    css = """
    :root { --token: oklch(50% 0.1 20); }
    html.dark { --token: oklch(60% 0.1 20); }
    .example {
        color: hwb(20 10% 10%);
        background: lab(40% 56.6 39);
        border-color: lch(50% 50 20);
    }
    """
    assert _direct_component_color_literals(css) == [
        "hwb(20 10% 10%)",
        "lab(40% 56.6 39)",
        "lch(50% 50 20)",
    ]
