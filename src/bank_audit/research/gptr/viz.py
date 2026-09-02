"""Визуализация досье: модель рисует, код гарантирует числа.

Модель получает раздел и его факты и пишет HTML или SVG сама — форму
выбирает она: матрица условий, лестница ранжирования, таймлайн, доска шагов.
Но ни одного числа руками: в шаблоне модели цифр нет вообще, каждое число,
дата и цитата входят только ссылкой на факт `{{f:12}}`, и подставляет их код
из проверенного факта. Разметка проходит белый список тегов, атрибутов и
CSS-свойств — не чёрный список подстрок, а разбор. Что не прошло —
выбрасывается целиком с записью причины: полкартинки хуже, чем её отсутствие.

Конвейер: ответ модели → блок → плейсхолдеры в сентинели → проверка шаблона
(нет цифр, нет скобок якорей, якорь рядом с каждым числом, дата в сравнении)
→ подстановка экранированных значений → санитайзер → проверка выхода
(числа текста ⊆ числа фактов, лимиты svg) → якоря источников и логотипы →
финальная очистка. Якоря нумерует поток, поэтому последний шаг — `finalize`.
"""
from __future__ import annotations

import functools
import hashlib
import html
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Разделы, к которым зовём дизайнера; сколько блоков допустимо; где блок
# стоит относительно текста.
SECTIONS = ("conditions", "market", "voice", "checks", "summary")
MAX_BLOCKS = {"conditions": 2, "checks": 2}
BEFORE_TEXT = ("summary",)
LOGO_SECTIONS = ("market",)
MIN_FACTS = 3

TEMPLATE_BYTES = 60_000
FINAL_BYTES = 80_000
MAX_PLACEHOLDERS = 200
MAX_SVG = 5
MAX_PATHS = 40
PATH_BYTES = 4_000
MAX_TAGS = 2_000
QUOTE_CHARS = 300
CITE_WINDOW = 250          # якорь источника — не дальше этого от числа
TIMEOUT = 120.0            # одна задача дизайнера
FINAL_WAIT = 150.0         # общий бюджет ожидания в конце письма
CONCURRENCY = 2

LOGO_DIR = os.getenv("AL_LOGO_DIR", "/app/assets/logos")
PALETTE = ("--ink", "--ink-2", "--ink-3", "--ink-4", "--paper", "--paper-2",
           "--surface", "--hair", "--hair-2", "--accent", "--accent-soft",
           "--pos", "--warn", "--neg")

_S_VAL, _S_CITE, _S_LOGO = "", "", ""     # сентинели
_SENTINELS = re.compile("[-]")


class VizRejected(Exception):
    """Блок не прошёл проверку. Причина — в тексте, она уходит в лог."""


# ── Фирменные цвета и бейджи ─────────────────────────────────────────────────
# Приблизительные цвета брендов для монограмм; точность не требуется — это
# визуальная подсказка, а не воспроизведение логотипа. Живут только внутри
# бейджа, который собирает код: модели литеральные цвета запрещены.
BRAND = {
    "sberbank": "#21A038", "sber": "#21A038", "vtb": "#0A2896",
    "alfabank": "#EF3124", "alfa": "#EF3124", "tbank": "#FFDD2D",
    "tinkoff": "#FFDD2D", "gazprombank": "#0E2A47", "raiffeisen": "#FEE600",
    "raiffeisenbank": "#FEE600", "otkritie": "#00BEF0", "sovcombank": "#F5222D",
    "rosbank": "#DA291C", "pochtabank": "#0057A8", "mkb": "#C8102E",
    "psb": "#003F87", "promsvyazbank": "#003F87", "rshb": "#006B38",
    "rosselkhozbank": "#006B38", "uralsib": "#DD1C1A", "domclick": "#3AB54A",
    "vbrr": "#1F3A93", "ozon": "#005BFF", "ozonbank": "#005BFF",
    "yandex": "#FC3F1D", "yandexbank": "#FC3F1D", "wildberries": "#CB11AB",
    "wbbank": "#CB11AB", "renaissance": "#1E2F97", "rencredit": "#1E2F97",
    "homebank": "#E4002B", "homecredit": "#E4002B", "otp": "#52AE30",
    "otpbank": "#52AE30", "zenit": "#E30613", "akbars": "#00A651",
    "mtsbank": "#E30611", "mts": "#E30611", "unicredit": "#E2001A",
    "citibank": "#003B70", "bcs": "#1B5EB9", "sinara": "#2C3E7B",
    "lokobank": "#0A3D91", "avangard": "#0072BC", "absolut": "#E4002B",
    "cbr": "#003399", "evotor": "#7B2CBF",
}
_STOP_WORDS = {"банк", "bank", "ао", "пао", "ооо", "кб", "акб", "the"}
_SLUG = re.compile(r"^[a-z0-9_-]{1,40}$")


def _norm_slug(slug: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (slug or "").lower())


def brand_color(slug: str) -> str:
    key = _norm_slug(slug)
    if key in BRAND:
        return BRAND[key]
    for k, v in BRAND.items():
        if len(key) >= 4 and (key.startswith(k) or k.startswith(key)):
            return v
    h = int(hashlib.sha1(key.encode()).hexdigest()[:6], 16)   # детерминированно
    return _hsl_hex(h % 360, 0.55, 0.42)


def _hsl_hex(h: float, s: float, l: float) -> str:
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    r, g, b = {0: (c, x, 0), 1: (x, c, 0), 2: (0, c, x), 3: (0, x, c),
               4: (x, 0, c), 5: (c, 0, x)}[int(h // 60) % 6]
    return "#%02X%02X%02X" % tuple(int((v + m) * 255) for v in (r, g, b))


def _text_on(color: str) -> str:
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return "#111318" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#FFFFFF"


def initials(label: str) -> str:
    clean = re.sub(r"[«»\"'().,]", " ", label or "")
    words = [w for w in re.split(r"[\s\-–—]+", clean) if w]
    sig = [w for w in words if w.lower() not in _STOP_WORDS] or words
    if not sig:
        return "?"
    if len(sig) == 1 and sig[0].isupper() and 2 <= len(sig[0]) <= 5:
        return sig[0][:2]                      # аббревиатура: ВБРР → ВБ
    return "".join(w[0] for w in sig[:2]).upper()


def logo_svg(slug: str, label: str) -> str:
    """Официальный SVG из каталога на сервере, иначе монограмма. Фрагмент уже
    безопасен: официальный прошёл санитайзер, монограмму собрал код."""
    official = _official_logo(_norm_slug(slug))
    if official:
        return official
    color = brand_color(slug)
    txt = html.escape(initials(label))
    fs = 20 if len(txt) == 1 else 15
    return (f'<svg viewBox="0 0 40 40" width="1.6em" height="1.6em" role="img" '
            f'aria-hidden="true" style="vertical-align:middle">'
            f'<rect width="40" height="40" rx="10" fill="{color}"></rect>'
            f'<text x="20" y="26" text-anchor="middle" font-size="{fs}" '
            f'font-weight="700" fill="{_text_on(color)}">{txt}</text></svg>')


@functools.lru_cache(maxsize=256)
def _official_logo(key: str) -> str:
    """Файл каталога, если он есть и безопасен. Слаг приходит от модели, а
    модель читает чужие страницы — поэтому путь проверяется, а не строится."""
    if not key or not _SLUG.match(key):
        return ""
    root = Path(LOGO_DIR)
    try:
        p = (root / f"{key}.svg").resolve()
        if not p.is_relative_to(root.resolve()) or p.is_symlink() or not p.is_file():
            return ""
        if p.stat().st_size > 64_000:
            return ""
        raw = p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    if "viewBox" not in raw:
        return ""
    try:
        cleaned = _nh3(raw, final=True)
    except VizRejected as e:
        log.warning("логотип %s отклонён: %s", key, e)
        return ""
    if not cleaned.lstrip().startswith("<svg"):
        return ""
    cleaned = re.sub(r"<svg\b([^>]*)>", lambda m: "<svg" + re.sub(
        r'\s(width|height|style)="[^"]*"', "", m.group(1))
        + ' width="1.6em" height="1.6em" style="vertical-align:middle">',
        cleaned, count=1)
    return cleaned


# ── Плейсхолдеры и шаблон ────────────────────────────────────────────────────
_PH = re.compile(
    r"\{\{\s*(?:f:(?P<fid>\d{1,6})(?:\.(?P<field>value|unit|date|subject|quote|cite|attr|side))?"
    r"|(?P<kind>logo|name|meta):(?P<key>[A-Za-z0-9_\-]{1,40}))\s*\}\}")
_META_KEYS = ("facts_used", "facts_total", "subjects", "date_min", "date_max")
_TAG = re.compile(r"<[^>]+>")
_SUPERSCRIPT = re.compile(r"[²³¹⁰-⁹¼-¾⅐-⅞]")


def _esc(v) -> str:
    """Значение факта — с чужого сайта. Экранируем всё, что может стать
    разметкой, плейсхолдером или якорем."""
    return (html.escape(str(v if v is not None else ""), quote=True)
            .replace("{", "&#123;").replace("}", "&#125;")
            .replace("[", "&#91;").replace("]", "&#93;"))


def _side(stance: str) -> str:
    return {"declared": "заявлено", "regulatory": "норма регулятора"}.get(stance, "наблюдается")


@dataclass
class Prepared:
    html: str                         # значения подставлены, cite и logo — сентинели
    fact_ids: list[int]
    logos: dict[str, str] = field(default_factory=dict)   # slug → фрагмент


def meta_for(facts: list) -> dict[str, str]:
    dates = sorted(d for d in (str(getattr(f, "date", "") or "")[:10] for f in facts) if d)
    return {"facts_total": str(len(facts)),
            "subjects": str(len({getattr(f, "subject", "") for f in facts})),
            "date_min": dates[0] if dates else "",
            "date_max": dates[-1] if dates else ""}


def prepare(template: str, *, facts: list, labels: dict[str, str], section: str,
            subjects: list[str]) -> Prepared:
    """Шаблон модели → разметка с подставленными значениями.

    Проверки шаблона идут ДО подстановки, по тексту с сентинелями вместо
    плейсхолдеров: так цифры, скобки якорей и прочее заведомо принадлежат
    модели, а не фактам."""
    if len(template.encode("utf-8")) > TEMPLATE_BYTES:
        raise VizRejected(f"шаблон больше {TEMPLATE_BYTES // 1000} КБ")
    if _SENTINELS.search(template):
        raise VizRejected("служебные символы в шаблоне")
    by_id = {f.id: f for f in facts}
    slots: list[tuple] = []

    def to_sentinel(m: re.Match) -> str:
        if m.group("fid"):
            fid = int(m.group("fid"))
            if fid not in by_id:
                raise VizRejected(f"факт f:{fid} не из этого раздела")
            slots.append(("f", fid, m.group("field") or "value"))
        else:
            kind, key = m.group("kind"), m.group("key")
            if kind == "logo":
                if section not in LOGO_SECTIONS:
                    raise VizRejected("логотипы допустимы только в сравнении с рынком")
                if key not in subjects or not _SLUG.match(key):
                    raise VizRejected(f"логотип неизвестного объекта {key}")
            elif kind == "name":
                if key not in subjects and key not in labels:
                    raise VizRejected(f"название неизвестного объекта {key}")
            elif kind == "meta":
                if key not in _META_KEYS:
                    raise VizRejected(f"неизвестный счётчик meta:{key}")
            slots.append((kind, key, ""))
        return f"{_S_VAL}{len(slots) - 1}{_S_VAL}"

    sent = _PH.sub(to_sentinel, template)
    if len(slots) > MAX_PLACEHOLDERS:
        raise VizRejected("слишком много плейсхолдеров")
    left = re.search(r"\{\{[^}]{0,80}\}?\}?", sent)
    if left:
        raise VizRejected(f"неизвестный плейсхолдер {left.group(0)[:60]}")
    # Текст шаблона без сентинелей — то, что написала модель сама. Цифры
    # запрещены в видимом тексте: в геометрии svg (viewBox, d, x) они
    # неизбежны и числом для читателя не являются.
    own = re.sub(rf"{_S_VAL}\d+{_S_VAL}", "", sent)
    own_text = html.unescape(_TAG.sub(" ", own))
    digits = [i for i, ch in enumerate(own_text) if unicodedata.category(ch) == "Nd"]
    if digits:
        # Причина с контекстом: по ней видно, что именно модель пишет руками —
        # ранг, срок из текста или число из факта мимо плейсхолдера.
        seen, snippets = -100, []
        for i in digits:
            if i - seen > 30 and len(snippets) < 3:
                snippets.append("…" + re.sub(r"\s+", " ", own_text[max(0, i - 18):i + 14]).strip() + "…")
            seen = i
        raise VizRejected("цифра, написанная руками, — числа только из фактов: " + " ".join(snippets))
    if _SUPERSCRIPT.search(own_text):
        raise VizRejected("надстрочная цифра в шаблоне")
    if "[" in own_text or "]" in own_text:
        raise VizRejected("квадратные скобки — якоря ставит код")
    if re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", own):
        raise VizRejected("литеральный цвет — только переменные палитры")

    # Якорь рядом с каждым числом; дата — в любом сравнении двух объектов.
    positions = [(m.start(), int(m.group(1))) for m in re.finditer(rf"{_S_VAL}(\d+){_S_VAL}", sent)]
    cite_pos = {i: p for p, i in positions if slots[i][0] == "f" and slots[i][2] == "cite"}
    for p, i in positions:
        kind, fid, fld = slots[i]
        if kind == "f" and fld in ("value", "quote"):
            near = any(slots[j][1] == fid and abs(cp - p) <= CITE_WINDOW for j, cp in cite_pos.items())
            if not near:
                raise VizRejected(f"у числа факта f:{fid} нет якоря источника рядом")
    used_ids = list(dict.fromkeys(s[1] for s in slots if s[0] == "f"))
    if len({by_id[i].subject for i in used_ids}) >= 2 and not any(
            s[0] == "f" and s[2] == "date" for s in slots):
        raise VizRejected("сравнение объектов без единой даты")

    used = [by_id[i] for i in used_ids]
    meta = meta_for(facts)
    meta["facts_used"] = str(len(used_ids))
    logos: dict[str, str] = {}

    def fill(m: re.Match) -> str:
        kind, key, fld = slots[int(m.group(1))]
        if kind == "f":
            f = by_id[key]
            if fld == "value":
                unit = f" {f.unit}" if getattr(f, "unit", "") else ""
                return _esc(f"{f.value}{unit}".strip())
            if fld == "unit":
                return _esc(getattr(f, "unit", "") or "")
            if fld == "date":
                return _esc(str(getattr(f, "date", "") or "")[:10])
            if fld == "subject":
                return _esc(labels.get(f.subject, f.subject) or "общее")
            if fld == "attr":
                return _esc(getattr(f, "attribute", "") or "")
            if fld == "side":
                return _esc(_side(getattr(f, "stance", "")))
            if fld == "quote":
                q = (getattr(f, "verbatim", "") or "").strip()
                return _esc(q[:QUOTE_CHARS] + ("…" if len(q) > QUOTE_CHARS else ""))
            return f"{_S_CITE}{key}{_S_CITE}"
        if kind == "name":
            return _esc(labels.get(key, key))
        if kind == "meta":
            return _esc(meta.get(key, ""))
        logos[key] = logo_svg(key, labels.get(key, key))
        return f"{_S_LOGO}{key}{_S_LOGO}"

    out = re.sub(rf"{_S_VAL}(\d+){_S_VAL}", fill, sent)
    if re.search(r"<[^>]*[][^>]*>", out):
        raise VizRejected("якорь или логотип внутри тега, а не в тексте")
    return Prepared(out, used_ids, logos)


# ── Числа на выходе: страховка ───────────────────────────────────────────────
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _tokens(text: str) -> set[str]:
    out = set()
    # «500 000» — одно число; «1,7 500» — два. Группа тысяч склеивается
    # только у целого числа из одной-трёх цифр.
    grouped = re.sub(r"(?<![\d.,])(\d{1,3})((?:[\s\u00a0\u202f]\d{3})+)(?![\d.,]\d)",
                     lambda m: m.group(1) + re.sub(r"[\s\u00a0\u202f]", "", m.group(2)), text or "")
    for m in _NUM.finditer(grouped):
        tok = m.group(0).replace(",", ".")
        out.add(tok)
        if "." in tok:
            out.add(tok.rstrip("0").rstrip("."))
    return out


def visible_text(markup: str) -> str:
    return html.unescape(_TAG.sub(" ", markup))


def check_output_numbers(markup: str, facts: list, meta: dict[str, str]) -> None:
    """После подстановки все числа текста обязаны быть числами фактов блока
    или служебных счётчиков. Ловит подстановку в атрибутный контекст и всё,
    что просочилось бы мимо проверки шаблона."""
    allowed: set[str] = set()
    for f in facts:
        for fld in ("value", "unit", "date", "verbatim", "attribute"):
            allowed |= _tokens(str(getattr(f, fld, "") or ""))
    for v in meta.values():
        allowed |= _tokens(v)
    seen = _tokens(visible_text(_SENTINELS.sub(" ", re.sub(r"[][^]*[]", " ", markup))))
    foreign = sorted(t for t in seen if t not in allowed and t.rstrip("0").rstrip(".") not in allowed)
    if foreign:
        raise VizRejected("числа не из фактов: " + ", ".join(foreign[:6]))


# ── Санитайзер ───────────────────────────────────────────────────────────────
_HTML_TAGS = {"div", "span", "p", "b", "strong", "i", "em", "small", "sup",
              "sub", "br", "hr", "ul", "ol", "li", "table", "thead", "tbody",
              "tr", "th", "td", "caption", "h4"}
_SVG_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line",
             "polyline", "polygon", "text", "tspan"}
_GLOBAL_ATTRS = {"class", "style", "role", "aria-hidden", "colspan", "rowspan"}
_SVG_ATTRS = {"viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2",
              "cx", "cy", "r", "rx", "ry", "d", "points", "fill", "stroke",
              "stroke-width", "stroke-linecap", "stroke-linejoin",
              "stroke-dasharray", "opacity", "fill-opacity", "stroke-opacity",
              "transform", "font-size", "font-weight", "text-anchor",
              "dominant-baseline", "letter-spacing", "preserveAspectRatio",
              "dx", "dy"}
_ATTRS = {t: set(_GLOBAL_ATTRS) for t in _HTML_TAGS}
_ATTRS.update({t: set(_GLOBAL_ATTRS) | _SVG_ATTRS for t in _SVG_TAGS})
_ATTRS_FINAL = {t: set(v) for t, v in _ATTRS.items()}
_ATTRS_FINAL["sup"] = _ATTRS_FINAL["sup"] | {"data-cite"}
_DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "img",
                      "foreignObject", "foreignobject", "use", "animate",
                      "animateTransform", "animatetransform", "set", "a",
                      "link", "meta", "video", "audio", "math", "form",
                      "input", "button", "textarea", "select", "noscript",
                      "template", "title", "desc", "defs", "linearGradient",
                      "lineargradient", "radialGradient", "radialgradient",
                      "clipPath", "clippath", "mask", "pattern", "filter",
                      "image", "symbol", "marker", "switch"}
_CSS_PROPS = {
    "color", "background-color", "font-size", "font-weight", "font-style",
    "width", "height", "min-width", "max-width", "min-height", "max-height",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-radius", "border-color", "border-width", "border-style",
    "display", "flex", "flex-direction", "flex-wrap", "flex-grow",
    "flex-shrink", "flex-basis", "grid-template-columns", "grid-template-rows",
    "grid-column", "grid-row", "gap", "row-gap", "column-gap", "align-items",
    "align-self", "align-content", "justify-content", "justify-items",
    "justify-self", "text-align", "line-height", "white-space",
    "letter-spacing", "text-transform", "text-decoration", "vertical-align",
    "word-break", "overflow-wrap", "list-style", "table-layout",
    "border-collapse", "border-spacing", "fill", "stroke", "box-sizing",
}
_DISPLAY_OK = {"flex", "inline-flex", "grid", "block", "inline-block", "inline",
               "table", "table-row", "table-cell", "list-item"}
_NO_NEGATIVE = re.compile(r"(^|[\s,])-\d")
_VAR = re.compile(r"var\((--[a-z0-9-]+)\)")
_ROLES = {"img", "group", "presentation", "table", "row", "cell", "list", "listitem"}


def _num(value: str, lo: float, hi: float, units=("", "px", "%")) -> str | None:
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)(px|%|em|rem)?\s*", value or "")
    if not m or (m.group(2) or "") not in units:
        return None
    v = float(m.group(1))
    if m.group(2) == "%":
        return value.strip() if 0 <= v <= 100 else None
    return value.strip() if lo <= v <= hi else None


def clean_style(value: str) -> str | None:
    """Разбор, а не чёрный список: свойство из белого списка, значение из
    букв, цифр и единиц, из функций — только var(--…) палитры."""
    if not value or re.search(r"[\\<>&\"'\x00-\x1f]", value):
        return None
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    out = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop, val = prop.strip().lower(), val.strip().lower()
        if prop not in _CSS_PROPS or not val:
            continue
        stripped = _VAR.sub(lambda m: "" if m.group(1) in PALETTE else "\x00", val)
        if "\x00" in stripped or "(" in stripped or ")" in stripped or "#" in stripped:
            continue
        if not re.fullmatch(r"[a-z0-9%.,\- ]*", stripped) or _NO_NEGATIVE.search(stripped):
            continue
        if prop == "display" and val not in _DISPLAY_OK:
            continue
        if prop == "font-size" and not (_num(val, 12, 48, ("px",)) or _num(val, 0.85, 3, ("em", "rem"))):
            continue
        if prop in ("width", "min-width", "max-width", "flex-basis") and val != "auto" \
                and not _num(val, 0, 1200):
            continue
        if prop in ("height", "min-height", "max-height") and val != "auto" and not _num(val, 0, 600):
            continue
        out.append(f"{prop}:{val}")
    return ";".join(out) if out else None


def _transform_ok(value: str) -> bool:
    rest = value.strip().lower()
    for m in re.finditer(r"(translate|rotate|scale)\(([\d.,\s-]+)\)", rest):
        nums = [float(x) for x in re.split(r"[\s,]+", m.group(2).strip()) if x]
        if m.group(1) == "translate" and not all(abs(v) <= 2000 for v in nums):
            return False
        if m.group(1) == "scale" and not all(0.1 <= v <= 10 for v in nums):
            return False
        if m.group(1) == "rotate" and not nums:
            return False
    return re.fullmatch(r"(\s*(translate|rotate|scale)\([\d.,\s-]+\)\s*)+", rest) is not None


def _make_filter(final: bool):
    def attr_filter(tag: str, attr: str, value: str) -> str | None:
        v = _SENTINELS.sub("", value or "")
        if attr == "style":
            return clean_style(v)
        if attr == "class":
            keep = [c for c in v.split() if c == "viz" or (final and c in ("cite", "viz-cite"))]
            return " ".join(keep) or None
        if attr == "role":
            return v if v in _ROLES else None
        if attr == "aria-hidden":
            return v if v in ("true", "false") else None
        if attr in ("colspan", "rowspan"):
            return v if re.fullmatch(r"\d{1,2}", v) and 1 <= int(v) <= 12 else None
        if attr == "data-cite":
            return v if final and re.fullmatch(r"\d{1,3}", v) else None
        if attr in ("fill", "stroke"):
            low = v.strip().lower()
            if low in ("none", "currentcolor", "transparent"):
                return low
            m = _VAR.fullmatch(low)
            if m and m.group(1) in PALETTE:
                return low
            if final and re.fullmatch(r"#[0-9a-f]{3,8}", low):
                return v.strip()               # только фрагменты, собранные кодом
            return None
        if attr == "d":
            if len(v) > PATH_BYTES or not re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9.,\s\-]+", v) \
                    or len(re.findall(r"[A-Za-z]", v)) > 300:
                return None
            return v
        if attr == "points":
            nums = re.findall(r"-?\d+(?:\.\d+)?", v)
            if not re.fullmatch(r"[\d.,\s\-]+", v) or len(nums) > 400 or any(abs(float(x)) > 5000 for x in nums):
                return None
            return v
        if attr == "transform":
            return v if _transform_ok(v) else None
        if attr == "viewBox":
            nums = re.findall(r"-?\d+(?:\.\d+)?", v)
            return v if len(nums) == 4 and all(abs(float(x)) <= 5000 for x in nums) else None
        if attr in ("x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "dx", "dy"):
            return _num(v, -5000, 5000, ("", "px", "%", "em"))
        if attr in ("width", "height") and tag == "svg":
            return _num(v, 0, 1200, ("", "px", "%", "em"))
        if attr in ("width", "height"):
            return _num(v, 0, 5000, ("", "px", "%", "em"))
        if attr in ("r", "rx", "ry", "stroke-width"):
            return _num(v, 0, 500, ("", "px", "%"))
        if attr in ("opacity", "fill-opacity", "stroke-opacity"):
            return _num(v, 0.15, 1, ("",))
        if attr == "font-size":
            return _num(v, 10, 48, ("", "px"))
        if attr == "font-weight":
            return v if v in ("400", "500", "600", "700", "bold", "normal") else None
        if attr == "text-anchor":
            return v if v in ("start", "middle", "end") else None
        if attr == "dominant-baseline":
            return v if v in ("auto", "middle", "central", "hanging", "alphabetic") else None
        if attr == "stroke-linecap":
            return v if v in ("butt", "round", "square") else None
        if attr == "stroke-linejoin":
            return v if v in ("miter", "round", "bevel") else None
        if attr == "stroke-dasharray":
            nums = re.findall(r"\d+(?:\.\d+)?", v)
            return v if nums and re.fullmatch(r"[\d.,\s]+", v) and all(1 <= float(x) <= 100 for x in nums) else None
        if attr == "letter-spacing":
            return _num(v, 0, 5, ("", "px"))
        if attr == "preserveAspectRatio":
            return v if re.fullmatch(r"(none|x(Min|Mid|Max)Y(Min|Mid|Max)( (meet|slice))?)", v) else None
        return None
    return attr_filter


def _nh3(markup: str, *, final: bool) -> str:
    try:
        import nh3
    except ImportError as e:                     # pragma: no cover
        raise VizRejected(f"санитайзер недоступен: {e}")
    return nh3.clean(markup, tags=_HTML_TAGS | _SVG_TAGS,
                     attributes=_ATTRS_FINAL if final else _ATTRS,
                     clean_content_tags=_DROP_WITH_CONTENT,
                     attribute_filter=_make_filter(final), strip_comments=True,
                     link_rel=None, url_schemes=set())


def sanitize(markup: str) -> str:
    """Белый список плюс лимиты формы. Корневые svg получают ширину 100%."""
    if len(markup.encode("utf-8")) > TEMPLATE_BYTES:
        raise VizRejected(f"блок больше {TEMPLATE_BYTES // 1000} КБ")
    cleaned = _nh3(markup, final=False).strip()
    if len(re.findall(r"<[a-zA-Z]", cleaned)) > MAX_TAGS:
        raise VizRejected("слишком много элементов")
    if cleaned.count("<svg") > MAX_SVG:
        raise VizRejected("слишком много svg")
    if cleaned.count("<path") > MAX_PATHS:
        raise VizRejected("слишком много путей svg")
    if re.search(r"<svg\b(?![^>]*\bviewBox=)", cleaned):
        raise VizRejected("svg без viewBox")
    if re.search(r"<(script|iframe|foreignobject|use)\b|\son[a-z]+\s*=", cleaned, re.I):
        raise VizRejected("запрещённый элемент пережил очистку")
    cleaned = re.sub(r"<svg\b([^>]*)>", lambda m: "<svg" + re.sub(
        r'\s(width|height)="[^"]*"', "", m.group(1)) + ' width="100%">', cleaned)
    return cleaned


# ── Ответ модели → блоки ─────────────────────────────────────────────────────
_FENCE = re.compile(r"```(?:html|svg|xml)?[ \t]*\n(.*?)```", re.S | re.I)


def parse_blocks(answer: str, limit: int = 1) -> list[str]:
    text = (answer or "").strip()
    if not text or re.fullmatch(r"\W*ПУСТО\W*", text, re.I):
        return []
    blocks = [b.strip() for b in _FENCE.findall(text) if b.strip()]
    if not blocks and text.startswith("<"):
        blocks = [text]
    return [b for b in blocks if b.lower().startswith("<div")][:limit]


@dataclass
class Built:
    html: str                          # cite и logo — ещё сентинели
    fact_ids: list[int]
    logos: dict[str, str]
    rejected: list[str]


def build(answer: str, *, facts: list, labels: dict[str, str], section: str,
          subjects: list[str]) -> Built:
    """Ответ дизайнера → проверенная разметка (без номеров источников: их
    знает только поток). Отклонённые блоки — в `rejected` с причиной."""
    out: list[str] = []
    ids: list[int] = []
    logos: dict[str, str] = {}
    rejected: list[str] = []
    for i, raw in enumerate(parse_blocks(answer, MAX_BLOCKS.get(section, 1))):
        try:
            prep = prepare(raw, facts=facts, labels=labels, section=section, subjects=subjects)
            cleaned = sanitize(prep.html)
            used = [f for f in facts if f.id in prep.fact_ids]
            meta = meta_for(facts)
            meta["facts_used"] = str(len(prep.fact_ids))
            check_output_numbers(cleaned, used, meta)
            if not re.search(r"[A-Za-zА-Яа-я]", visible_text(cleaned)):
                raise VizRejected("блок без текста")
            if not prep.fact_ids:
                raise VizRejected("блок без единого факта")
            out.append(cleaned)
            ids.extend(prep.fact_ids)
            logos.update(prep.logos)
        except VizRejected as e:
            rejected.append(f"блок {i + 1}: {e}")
            log.info("визуализация %s: блок %d отклонён — %s", section, i + 1, e)
    return Built("\n".join(out), list(dict.fromkeys(ids)), logos, rejected)


def finalize(html_with_sentinels: str, logos: dict[str, str], cite) -> str:
    """Последний шаг — в потоке: номера источников и логотипы на место
    сентинелей, затем повторная очистка. `cite(fact_id)` → номер или None."""
    def _cite(m: re.Match) -> str:
        n = cite(int(m.group(1)))
        if n is None:
            raise VizRejected(f"источник факта f:{m.group(1)} неизвестен")
        return f'<sup class="cite viz-cite" data-cite="{n}">{n}</sup>'

    out = re.sub(rf"{_S_CITE}(\d+){_S_CITE}", _cite, html_with_sentinels)
    out = re.sub(rf"{_S_LOGO}([a-z0-9_\-]+){_S_LOGO}", lambda m: logos.get(m.group(1), ""), out)
    if _SENTINELS.search(out):
        raise VizRejected("остался служебный символ")
    out = _nh3(out, final=True).strip()
    if len(out.encode("utf-8")) > FINAL_BYTES:
        raise VizRejected(f"блок после подстановки больше {FINAL_BYTES // 1000} КБ")
    return out


def resanitize(markup: str) -> str:
    """Для разметки, пришедшей извне (клиент → PDF): та же финальная очистка."""
    try:
        return _nh3(markup or "", final=True).strip()
    except VizRejected:
        return ""


# ── Маркер в тексте ──────────────────────────────────────────────────────────
MARKER = "[[VIZ:{n}]]"
_MARKER_PREFIX = "[[VIZ:"


def marker(n: int) -> str:
    return "\n\n" + MARKER.format(n=n) + "\n\n"


async def without_markers(pieces):
    """Модель видела маркеры в чужих отчётах и может написать [[VIZ:0]]
    сама — тогда её текст подменил бы блок. Всё, что похоже на маркер в
    тексте модели, обезвреживается; хвост, который может оказаться началом
    маркера, придерживается до следующего куска."""
    buf = ""
    async for piece in pieces:
        buf += piece
        buf = buf.replace(_MARKER_PREFIX, "[[VIZ​:")
        hold = 0
        for k in range(min(len(_MARKER_PREFIX) - 1, len(buf)), 0, -1):
            if buf.endswith(_MARKER_PREFIX[:k]):
                hold = k
                break
        if hold:
            yield buf[:-hold]
            buf = buf[-hold:]
        else:
            yield buf
            buf = ""
    if buf:
        yield buf


def strip_markers(text: str) -> str:
    return re.sub(r"\n*\[\[VIZ:\d+\]\]\n*", "\n\n", text or "")


# ── Промпт дизайнера ─────────────────────────────────────────────────────────
_FORMS = {
    "conditions": (
        "Матрица «объект × характеристика»: точка отсчёта первой строкой, "
        "значение с меткой стороны, датой и якорем в каждой ячейке; пустая "
        "ячейка — штриховка фоном var(--hair) и подпись «нет данных». Второй "
        "блок допустим только как таймлайн, если у одной характеристики есть "
        "два и больше значений с разными датами."),
    "market": (
        "Лестница ранжирования допустима, только если у всех объектов одна "
        "характеристика, одна единица, одна сторона и даты в одном окне; "
        "тогда подпись «упорядочено по … на дату …». Иначе — матрица "
        "«лучше / хуже / паритет / несопоставимо: причина» относительно точки "
        "отсчёта. Здесь и только здесь уместны логотипы {{logo:slug}}."),
    "voice": (
        "Три–пять дословных цитат {{f:N.quote}} с объектом, датой и якорем, "
        "сгруппированных по темам. Никаких шкал, долей и индексов "
        "недовольства; «у них — проверить у нас» — отдельной плашкой."),
    "checks": (
        "Доска шагов: столбцы «Гипотеза (с якорем) → Процедура → Источник в "
        "банке → Ожидаемый документ», шаги через <ol>. Второй блок — только "
        "светофор приоритетов, если приоритеты названы в тексте раздела."),
    "summary": (
        "Одна главная карточка строгого состава: вопрос проверки одной "
        "строкой; три главных расхождения «заявлено против наблюдается» с "
        "якорями; позиция точки отсчёта относительно рынка, если сопоставимо; "
        "строка покрытия; даты самого свежего и самого старого факта; три "
        "первых шага проверки. Если чего-то из этого нет в фактах — ПУСТО."),
}


def designer_prompt(*, section: str, title: str, question: str, anchor: str,
                    labels: dict[str, str], facts_text: str, section_text: str,
                    subjects: list[str]) -> str:
    subj = ", ".join(f"{s} = {labels.get(s, s)}" for s in subjects) or "—"
    anchor_line = (f"Точка отсчёта: {labels.get(anchor, anchor)} (slug {anchor}); "
                   "выдели её рамкой var(--accent) и подписью «точка отсчёта»."
                   if anchor else "Точки отсчёта нет — объекты равноправны.")
    max_blocks = MAX_BLOCKS.get(section, 1)
    return "\n".join([
        "Ты — дизайнер аудиторского досье. Раздел уже написан; покажи его "
        "главное так, чтобы руководитель проверки понял за пять секунд. Форму "
        "выбираешь сам в рамках подсказки ниже; таблица предпочтительнее svg "
        "везде, где нет оси времени. Если визуализация не добавит ясности к "
        "тексту — ответь одним словом ПУСТО. Ответь ПУСТО обязательно, если: "
        "меньше трёх фактов; сравнение, а объектов с одной характеристикой "
        "меньше двух; все факты одной стороны и сравнивать нечего.",
        "",
        f"ВОПРОС АУДИТОРА: {question}",
        f"РАЗДЕЛ: {title}.",
        f"ФОРМА: {_FORMS.get(section, '')}",
        anchor_line,
        f"ОБЪЕКТЫ (slug = название): {subj}",
        "",
        "ЖЕЛЕЗНЫЕ ПРАВИЛА — блок с нарушением выбрасывается целиком.",
        "1. В твоей разметке нет ни одной цифры. Каждое число, дата, единица и "
        "цитата — только плейсхолдером, код подставит их из проверенных "
        "фактов: {{f:12}} значение с единицей; {{f:12.value}}, {{f:12.unit}}, "
        "{{f:12.date}}, {{f:12.subject}} название объекта, {{f:12.attr}} "
        "характеристика, {{f:12.side}} метка «заявлено/наблюдается/норма», "
        "{{f:12.quote}} дословная цитата. Нумерация шагов и тем — только "
        "списком <ol>, без цифр в тексте. Оси и шкалы числами не подписывай.",
        "2. Рядом с каждым числом, в том же элементе, — якорь источника "
        "{{f:12.cite}}. Не списком под блоком, а у числа.",
        "3. В сравнении двух и более объектов у значений стоит дата "
        "{{f:12.date}} — либо у каждого, либо в заголовке столбца.",
        "4. Нет данных — покажи честно: ячейка с подписью «нет данных», "
        "строку или столбец не убирай. Несопоставимое (разные единицы, "
        "стороны, даты) — серым с подписью «несопоставимо: причина», в ранг "
        "не включай. В сравнениях у каждого значения — метка стороны "
        "{{f:12.side}}, легенда сторон и статусов, если они есть.",
        "5. Название объекта — {{name:slug}}. Строка покрытия в конце "
        "каждого блока обязательна: «Показано фактов: {{meta:facts_used}} из "
        "{{meta:facts_total}} в разделе; объектов {{meta:subjects}}».",
        "6. Цвета — только переменные палитры: var(--ink) основной текст, "
        "var(--ink-2) второстепенный, var(--ink-3) подписи, var(--surface) фон "
        "карточки, var(--paper-2) фон подложки, var(--hair) линии и штриховка, "
        "var(--accent) рамка точки отсчёта, var(--pos) лучше, var(--warn) "
        "внимание, var(--neg) хуже — последние три только для статусов, "
        "названных в тексте раздела. Литеральные цвета (#hex, rgb) запрещены; "
        "интерфейс бывает тёмным. В тексте блока имена переменных не "
        "упоминай: легенда пишется словами («рамкой выделена точка отсчёта»). "
        "Между названием объекта и якорем разделителей не ставь.",
        "7. Разметка самодостаточна: ровно один корневой <div class=\"viz\"> "
        "на блок, только inline-стили (без функций, кроме var(--…); сетки "
        "через «1fr 1fr 1fr»), без <script>, <style>, <img>, ссылок, id, "
        "градиентов, классов, кроме viz. Ширина 100%, высота до ~480px, шрифт "
        "12–16px, переносы на узком экране (flex-wrap). Не больше шести "
        "объектов и шести характеристик; остальное — «ещё … в тексте».",
        "8. Запрещено: писать числа, проценты, даты и единицы руками; выводить "
        "производные (разницу, среднее, долю, ранг «на глаз»); кодировать "
        "величину длиной, площадью или углом; сортировать по величине вне "
        "сравнения с рынком; смешивать заявленное и наблюдаемое без метки; "
        "убирать объект из-за «нет данных»; словесные оценки объёма "
        "(«большинство», «массово», «резко») без факта-счётчика; эмодзи; "
        "заголовки-выводы («банк хуже рынка») — вывод только цитатой из текста "
        "раздела; стрелки тренда и прогноза.",
        f"9. Заголовок блока — <h4>: что показано (характеристика, период), а "
        f"не вывод. Блоков не больше {max_blocks}, каждый в отдельном "
        "ограждении ```html … ```. Никаких пояснений вне ограждений.",
        "",
        "ФАКТЫ РАЗДЕЛА (id | объект | характеристика | значение | сторона | дата):",
        facts_text.strip() or "— фактов нет —",
        "",
        "ТЕКСТ РАЗДЕЛА (для понимания главного; числа из него брать нельзя — "
        "только плейсхолдерами фактов):",
        (section_text or "").strip()[:12000],
    ])
