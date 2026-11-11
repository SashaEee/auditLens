"""Презентация по отчёту — один HTML-файл, который открывается без интернета.

ЗАЧЕМ. Владелец 11.11: презентация должна выглядеть как работа дизайнера
AuditLens, а не как шаблон с подставленным текстом; своя для каждого отчёта.
Поэтому макет и стили пишет сильная модель — целиком, под историю конкретного
отчёта. Код делает только то, что макет не портит:

  • упаковывает: шрифты AuditLens внутри файла, палитра, листание с клавиатуры,
    полный экран, печать в PDF — ни одного обращения в сеть;
  • чистит: никаких скриптов, внешних адресов и картинок из сети — файл
    открывают локально, и он обязан быть безвредным;
  • сверяет числа с текстом отчёта — тихо. Ничего не вырезает и ничего не
    приписывает: неточное место исправляется точечной заменой текста, остаток
    уходит только в журнал;
  • добавляет слайд источников и номера страниц.
"""
from __future__ import annotations

import base64
import html as _html
import json
import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

MAX_SLIDES = 14
_REPORT_CHARS = int(os.getenv("GPTR_DECK_REPORT_CHARS", "60000"))
_MAX_TOKENS = int(os.getenv("GPTR_DECK_MAX_TOKENS", "16000"))
_FONTS_DIR = Path(__file__).resolve().parents[2] / "web" / "static" / "vendor" / "fonts"
# семейство, файл, диапазон насыщенности, диапазон знаков
_LATIN = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+20AC,"
          "U+2122,U+2191,U+2193,U+2212,U+2215")
_CYR = "U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116"
# семейство, файл, начертание, диапазон насыщенности, диапазон знаков
_FONTS = [
    ("Geist", "geist-3.woff2", "normal", "300 700", _CYR),
    ("Geist", "geist-4.woff2", "normal", "300 700", _LATIN),
    ("Source Serif 4", "sourceserif4-20.woff2", "normal", "400 600", _CYR),
    ("Source Serif 4", "sourceserif4-19.woff2", "normal", "400 600", _LATIN),
    ("JetBrains Mono", "jetbrainsmono-13.woff2", "normal", "400 600", _CYR),
    ("JetBrains Mono", "jetbrainsmono-11.woff2", "normal", "400 600", _LATIN),
    # У Instrument Serif нет кириллицы: им набраны числа и латиница, русские
    # буквы берутся из Source Serif 4 (прямые) и системной Georgia (курсив).
    ("Instrument Serif", "instrumentserif-5.woff2", "normal", "400", _LATIN),
    ("Instrument Serif", "instrumentserif-8.woff2", "italic", "400", _LATIN),
]


# ── Общие помощники ──────────────────────────────────────────────────────────
def clean_report(md: str) -> str:
    """Отчёт для модели и для сверки: без маркеров блоков и служебных меток."""
    text = re.sub(r"\[\[(VIZ|CHART|FILE):[^\]]*\]\]", "", md or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_CITE = re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]")
# Пробел внутри числа — только разделитель тысяч («5 000 000»): иначе год и
# соседнее число из другой ячейки склеивались в «20266».
_NUM = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?(?!\d)")
_YEAR = re.compile(r"(?:19|20)\d\d")


def numbers(text: str) -> set[str]:
    """Числа текста в едином виде: без пробелов, запятая → точка. Не числа для
    сверки: ссылки [n]; малые целые до 12 («три банка», номера пунктов); годы —
    дизайнер ставит их на шкалу времени, и в отчёте их может не быть дословно
    (владелец 11.11: такие «находки» только мешают)."""
    out: set[str] = set()
    for m in _NUM.findall(_CITE.sub(" ¦ ", text or "")):
        n = re.sub(r"[\s\u00a0\u202f]", "", m).replace(",", ".").rstrip(".")
        if not n:
            continue
        if "." not in n and (int(n) <= 12 or _YEAR.fullmatch(n)):
            continue
        out.add(n)
    return out


def cites(text: str) -> list[int]:
    return [int(x) for grp in _CITE.findall(text or "") for x in re.findall(r"\d+", grp)]


def _ru_day(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else ""


def file_name(title: str) -> str:
    """Имя файла презентации по названию отчёта."""
    stem = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", title or "")).strip(" .")[:90]
    stem = stem or "Отчёт AuditLens"
    return f"{stem}.html"


def model_for() -> str:
    """Сильная модель: макет — работа дизайнера, а не пересказ."""
    return (os.getenv("GPTR_DECK_MODEL") or os.getenv("LLM_MODEL_ANALYST")
            or os.getenv("LLM_MODEL_REASONING") or os.environ.get("LLM_MODEL_NAME", ""))


# ── Задание дизайнеру ────────────────────────────────────────────────────────
_SYSTEM = """Ты — арт-директор AuditLens. Тебе дают готовый аналитический отчёт службы внутреннего аудита
Сбербанка. Сделай по нему презентацию для руководителя проверки — 8–11 слайдов, которые выглядят
как работа дизайнера делового издания, а не как шаблон с подставленным текстом. У каждого отчёта
своя история: найди её и построй композицию на ней. Сравнение — разворот-сопоставление и
диаграмма; жалобы — голоса клиентов и динамика; нормы — шкала дат; рынок — крупные числа.

# Что ты отдаёшь
Только HTML, без пояснений и без ограждения ```:
  <style> …свои стили этой презентации (если нужны)… </style>
  <section class="slide cover"> … </section>
  <section class="slide" data-label="Главный вывод"> <div class="body"> … </div> </section>
  …
Каждый слайд — один <section class="slide">, вложенных <section> нет. data-label — имя раздела
в 1–3 слова: код поставит его в шапку рядом с номером («03 / 10 — Главный вывод»).

# Холст и то, что делает код
• 1920×1080 px. Код сам масштабирует слайд под любой экран — раскладывай в пикселях.
• Шапку слайда (знак AuditLens, номер и раздел, название отчёта), оформление обложки (знак,
  водяной знак, дата внизу), слайд «Источники» и сноски ставит код. Не рисуй их.
• Внутри <div class="body"> у тебя 1728×860 px (поля 84/96/72 уже заданы). Всё должно
  помещаться без прокрутки.

# Язык AuditLens — набор готовых приёмов (классы уже есть, пользуйся ими)
Бумага и чернила: фон тёплый белый, текст почти чёрный, тонкие линии вместо рамок, один красный
акцент. Переменные: var(--paper) var(--paper-2) var(--surface) var(--ink) var(--ink-2) var(--ink-3)
var(--ink-4) var(--hair) var(--hair-2) var(--accent) var(--accent-soft) var(--pos) var(--warn)
var(--sber) var(--select); шрифты var(--serif) var(--display) var(--sans) var(--mono).

  .title      заголовок слайда — Source Serif, 68 px. Это УТВЕРЖДЕНИЕ, а не тема.
  .sub        подзаголовок 36 px, серый; .sub.it — курсивом с засечками
  .statement  фраза-вывод 46 px (числа — Instrument Serif); .statement.strong — жирным курсивом;
              .pin прижимает блок к низу слайда. <em> внутри — красный курсив: ОДНО главное слово
              или число на слайд.
  .eyebrow    надзаголовок: моноширинный, прописные; .small — подпись 25 px
  .rows > .row            строки, разделённые тонкими линиями. Внутри:
       <span class="n">01</span> номер;  <span class="dot"></span> красная точка;
       <span class="tag pos">СЕЙЧАС</span> метка (pos | accent | warn | muted);
       <div><div class="t">строка</div><div class="d">пояснение</div></div>  (.t.serif — с засечками)
  .strip > .cell          полоса показателей: в ячейке <div class="num">18,4%</div><div class="cap">подпись</div>
  .hero       число-герой 116 px (.hero.xl — 200 px); <span class="unit">₽ / мес</span> — единица;
              .vr — вертикальная линия между двумя героями
  table.tbl   таблица: th — моноширинные прописные, td.v — числовая ячейка; tr.hl — выделенная строка
  .pill       капсула для шагов процесса; между капсулами — стрелка «→» цветом var(--ink-4)
  .quote      цитата клиента курсивом 40 px; под ней .small с городом и датой
  .body.split две колонки (текст слева 600–760 px, справа диаграмма или таблица); .body.center
  цвет текста: .accent .pos .warn .sber .ink2 .muted .faint

Пример обложки:
  <section class="slide cover">
    <p class="eyebrow">Потребительские кредиты · сравнение условий</p>
    <h1><em>Сбер</em> дешевле на входе, но условия раскрыты хуже конкурентов</h1>
    <p class="sub">Вопрос аудитора одной строкой.</p>
  </section>
Пример слайда:
  <section class="slide" data-label="Цена кредита">
    <div class="body">
      <h1 class="title">По нижней границе полной стоимости Сбер — первый из трёх</h1>
      <div style="display:flex;gap:100px;margin-top:52px">
        <div><div class="hero accent">18,4%</div><div class="cap">Сбербанк, ПСК «от» [1]</div></div>
        <div class="vr"></div>
        <div><div class="hero">19,9%</div><div class="cap">ВТБ, ПСК «от» [3]</div></div>
      </div>
      <p class="statement strong pin">Сравнение «от» с «от»: это вход в продукт, а не цена для заёмщика.</p>
    </div>
  </section>

# Как сделать, чтобы это не выглядело шаблоном
• Не повторяй один макет два слайда подряд. Чередуй: число-герой → строки → таблица → диаграмма →
  цитата → схема. Плотный слайд сменяется «воздушным»: одна фраза или одно число на весь слайд.
• Минимум на двух слайдах — собственная диаграмма или схема, нарисованная под этот отчёт в SVG
  или на CSS: полосы, точки на общей шкале, «было — стало», шкала времени, путь клиента.
  Правила диаграмм: длины и положения считай ТОЧНО пропорционально значениям (это аудит);
  у каждого значения подпись числом; оси и сетка — var(--hair-2) толщиной 1 px; подписи —
  var(--mono) 22–25 px; данные Сбера — var(--sber) или var(--accent), остальные — var(--ink-3)
  и var(--ink-4); легенды нет — подписывай прямо у столбца или линии. В SVG обязателен viewBox,
  цвета — через var(--…) или currentColor.
• Свои приёмы допустимы и желательны, если они в том же языке: крупная типографика, сетка,
  тонкие линии, воздух. Нельзя: градиентные заливки, тени (кроме .card), эмодзи, иконки-
  пиктограммы, клипарт, цветные плашки на пол-слайда, больше одного акцентного цвета на слайде.
• Размеры текста: меньше 25 px не бывает. Основной текст 28–36 px. На слайде — одна мысль и
  не больше 55 слов.

# Содержание
• Только то, что есть в отчёте. Ничего не придумывай и не считай: не складывай, не вычитай, не
  округляй, не переводи единицы. Число пишется ровно как в отчёте. Хочешь показать разницу —
  поставь два числа рядом.
• Ссылки на источники — как в отчёте: [n] сразу после числа или утверждения (в подписи .cap,
  в строке, в ячейке). Код превратит их в сноски и соберёт слайд источников.
• Порядок: обложка → главный вывод → 4–7 слайдов доказательств → оговорки и пробелы данных,
  если они меняют вывод → «Что проверить» (если такой раздел есть в отчёте: его действия —
  строками с номерами, глагол в начале).
• Цитаты клиентов — дословно, с городом и датой, как в отчёте. Имён нет.
• Жалоба — это слова клиента, а не установленный факт. В заголовках и выводах пиши «клиент
  сообщает», «по словам клиента», «в двух жалобах описано»; вину банка или сотрудника не
  утверждай. Обложка — про главный вывод отчёта, а не про отдельный случай.
• Язык — простой русский. Без «данный», «является», «осуществляется», «в рамках».

# Запрещено технически (такой элемент будет удалён)
<script>, <img>, <iframe>, <link>, <a>, внешние адреса, url(...) кроме ссылок на свои градиенты
в SVG, @import, @font-face, position: fixed. Свои стили — в одном <style> в начале; селекторы
начинай с .slide, свои классы называй с префиксом x- (например .x-scale), чтобы не задеть набор."""


def _sources_digest(sources: list[dict] | None, limit: int = 60) -> str:
    rows = []
    for s in (sources or [])[:limit]:
        n = s.get("n")
        if n is None:
            continue
        name = str(s.get("title") or s.get("bank_name") or "")[:90]
        rows.append(f"[{n}] {name} — {s.get('domain') or ''}".rstrip(" —"))
    return "\n".join(rows)


def user_message(*, question: str, title: str, report: str, sources: list[dict] | None,
                 when: datetime | None) -> str:
    day = (when or datetime.now()).strftime("%d.%m.%Y")
    return (f"# Название отчёта\n{title or '—'}\n\n# Вопрос аудитора\n{question or '—'}\n\n"
            f"# Дата отчёта\n{day}\n\n# Источники отчёта (номер — название — сайт)\n"
            f"{_sources_digest(sources) or '—'}\n\n# Отчёт\n"
            f"{clean_report(report)[:_REPORT_CHARS]}")


# ── Очистка ──────────────────────────────────────────────────────────────────
_HTML_TAGS = {"section", "div", "span", "p", "h1", "h2", "h3", "h4", "b", "strong", "i", "em",
              "small", "sup", "sub", "br", "hr", "ul", "ol", "li", "table", "thead", "tbody",
              "tfoot", "tr", "th", "td", "caption", "colgroup", "col", "blockquote", "figure",
              "figcaption", "header", "footer", "article", "aside", "mark", "time", "dl", "dt",
              "dd", "cite", "q"}
_SVG_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
             "text", "tspan", "defs", "linearGradient", "lineargradient", "radialGradient",
             "radialgradient", "stop", "clipPath", "clippath"}
_GLOBAL_ATTRS = {"class", "style", "id", "role", "aria-hidden", "aria-label", "colspan",
                 "rowspan", "start", "datetime"}
_SVG_ATTRS = {"viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r",
              "rx", "ry", "d", "points", "fill", "stroke", "stroke-width", "stroke-linecap",
              "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "opacity",
              "fill-opacity", "stroke-opacity", "transform", "font-size", "font-weight",
              "font-family", "text-anchor", "dominant-baseline", "letter-spacing",
              "preserveAspectRatio", "dx", "dy", "fill-rule", "clip-path", "offset",
              "stop-color", "stop-opacity", "gradientUnits", "gradientTransform",
              "vector-effect", "textLength", "lengthAdjust", "viewbox", "preserveaspectratio",
              "gradientunits", "gradienttransform", "textlength", "lengthadjust"}
_DROP_WITH_CONTENT = {"script", "iframe", "object", "embed", "img", "image", "foreignObject",
                      "use", "animate", "animateTransform", "set", "link", "meta", "video",
                      "audio", "form", "input", "button", "textarea", "select", "noscript",
                      "template", "base", "title"}
_RESERVED_IDS = {"deck", "bar", "cnt", "ui", "hint"}
_URL_REF = re.compile(r"^url\(#[A-Za-z][\w-]*\)$")
_BAD_VALUE = re.compile(r"javascript\s*:|expression\s*\(|url\s*\(|@import|behavior\s*:|"
                        r"-moz-binding|\\", re.I)


def clean_css(css: str) -> str:
    """Стили модели: без обращений наружу и без того, что ломает оболочку."""
    css = re.sub(r"/\*.*?\*/", "", css or "", flags=re.S).replace("\\", "").replace("<", "")
    css = re.sub(r"@import[^;]*;?", "", css, flags=re.I)
    css = re.sub(r"@(font-face|namespace|charset|page)\b[^{;]*(\{[^}]*\}|;)", "", css, flags=re.I)
    css = re.sub(r"url\(\s*(?![\"']?#)[^)]*\)", "none", css, flags=re.I)
    css = re.sub(r"expression\s*\(|javascript\s*:|behavior\s*:|-moz-binding", "x-", css, flags=re.I)
    css = re.sub(r"position\s*:\s*fixed", "position:absolute", css, flags=re.I)
    return re.sub(r"\bnone\)+", "none", css).strip()


def _attr_filter(tag: str, attr: str, value: str) -> str | None:
    v = (value or "").strip()
    if attr == "style":
        out = clean_css(v)
        return out or None
    if attr == "id":
        return None if v.lower() in _RESERVED_IDS or not re.fullmatch(r"[A-Za-z][\w-]{0,40}", v) else v
    if attr in ("fill", "stroke", "clip-path"):
        if "url(" in v.lower():
            return v if _URL_REF.match(v) else None
    if _BAD_VALUE.search(v):
        return None
    return value


def sanitize(markup: str) -> str:
    """Белый список элементов и атрибутов. Скрипты, ссылки и внешние адреса исчезают."""
    import nh3
    attrs = {t: set(_GLOBAL_ATTRS) for t in _HTML_TAGS}
    attrs.update({t: set(_GLOBAL_ATTRS) | _SVG_ATTRS for t in _SVG_TAGS})
    return nh3.clean(markup, tags=_HTML_TAGS | _SVG_TAGS, attributes=attrs,
                     clean_content_tags=_DROP_WITH_CONTENT, attribute_filter=_attr_filter,
                     generic_attribute_prefixes={"data-"}, strip_comments=True, link_rel=None,
                     url_schemes=set())


_FENCE = re.compile(r"```(?:html)?[ \t]*\n(.*?)```", re.S | re.I)
_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S | re.I)
_SECTION = re.compile(r"<section\b[^>]*>.*?</section>", re.S | re.I)
_OPEN = re.compile(r"^<section\b([^>]*)>", re.I)


def split_answer(answer: str) -> tuple[str, list[str]]:
    """Ответ модели → (стили, слайды). Ограждение ``` снимается, если есть."""
    text = answer or ""
    m = _FENCE.search(text)
    if m and "<section" in m.group(1):
        text = m.group(1)
    css = "\n".join(_STYLE.findall(text))
    body = _STYLE.sub("", text)
    slides = [s for s in _SECTION.findall(body)
              if re.search(r'class="[^"]*\bslide\b', _OPEN.match(s).group(1) if _OPEN.match(s) else "")]
    return css, slides[:MAX_SLIDES]


_TAG = re.compile(r"<[^>]+>")


def visible_text(markup: str, sep: str = " ") -> str:
    """Текст без тегов. Для сверки чисел теги заменяются разделителем, который
    не может оказаться внутри числа: «2026» и «6» из соседних ячеек — два числа."""
    return _html.unescape(_TAG.sub(sep, markup or ""))


def unknown_numbers(slides: list[str], report: str) -> list[str]:
    """Числа слайдов, которых нет в отчёте. Только сообщает — ничего не трогает."""
    base = numbers(report)
    out: list[str] = []
    for s in slides:
        for n in sorted(numbers(visible_text(s, " ¦ ")) - base):
            if n not in out:
                out.append(n)
    return out


_CITE_TXT = re.compile(r"\s*\[(\d+(?:\s*[,;]\s*\d+)*)\]")


def link_citations(markup: str, known: set[int]) -> str:
    """[n] в тексте слайда → аккуратная сноска. Внутри тегов ничего не трогаем."""
    def cite(m: re.Match) -> str:
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        if known and not all(n in known for n in nums):
            return m.group(0)
        return '<sup class="cite">' + ",".join(str(n) for n in nums) + "</sup>"
    parts = re.split(r"(<[^>]+>)", markup)
    in_svg = 0
    for i, part in enumerate(parts):
        if part.startswith("<"):
            if re.match(r"<svg\b", part, re.I):
                in_svg += 1
            elif re.match(r"</svg", part, re.I):
                in_svg = max(0, in_svg - 1)
            continue
        if not in_svg:
            parts[i] = _CITE_TXT.sub(cite, part)
    # [1][3] подряд — одна сноска «1,3»
    return "".join(parts).replace('</sup><sup class="cite">', ",")


# ── Оболочка ─────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _font_css() -> str:
    rules = []
    for family, name, style, weight, urange in _FONTS:
        try:
            data = base64.b64encode((_FONTS_DIR / name).read_bytes()).decode("ascii")
        except OSError as e:
            log.warning("презентация: шрифт %s не найден — %s", name, e)
            continue
        rules.append(f'@font-face{{font-family:"{family}";font-style:{style};font-weight:{weight};'
                     f'font-display:swap;src:url(data:font/woff2;base64,{data}) format("woff2");'
                     f"unicode-range:{urange}}}")
    return "".join(rules)


_BASE_CSS = """
:root{--paper:#FBFAF7;--paper-2:#F6F4F1;--surface:#FFFFFF;--ink:#0E1116;--ink-2:#3F4348;--ink-3:#73777D;
--ink-4:#A9ADB3;--hair:#E3E1DE;--hair-2:#D3D1CD;--accent:#D0393B;--accent-soft:#FBEFEE;--pos:#2F7A4B;
--pos-soft:#E8F1EA;--warn:#B07A14;--warn-soft:#FAF2E1;--sber:#006B30;--sber-soft:#E6F0EA;--select:#374960;
--mark:#1F4DFF;
--serif:"Source Serif 4",Georgia,"Times New Roman",serif;
--display:"Instrument Serif","Source Serif 4",Georgia,"Times New Roman",serif;
--italic:"Instrument Serif",Georgia,"Times New Roman",serif;
--sans:"Geist",-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@supports (color:oklch(50% .1 20)){:root{--paper:oklch(98.5% .004 80);--paper-2:oklch(96.8% .005 80);
--surface:oklch(100% 0 0);--ink:oklch(17% .01 260);--ink-2:oklch(38% .01 260);--ink-3:oklch(58% .012 260);
--ink-4:oklch(76% .012 260);--hair:oklch(91% .005 80);--hair-2:oklch(86% .005 80);--accent:oklch(58% .18 25);
--accent-soft:oklch(58% .18 25 / .08);--pos:oklch(48% .10 150);--pos-soft:oklch(48% .10 150 / .10);
--warn:oklch(64% .13 75);--warn-soft:oklch(64% .13 75 / .12);--sber:oklch(46% .13 152);
--sber-soft:oklch(46% .13 152 / .10);--select:oklch(40% .045 255)}}
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;height:100%;background:#0B0D10;overflow:hidden}
.slide{position:absolute;left:50%;top:50%;width:1920px;height:1080px;margin:-540px 0 0 -960px;
transform:scale(var(--k,1));transform-origin:center center;background:var(--paper);color:var(--ink);
font-family:var(--sans);font-size:28px;line-height:1.5;overflow:hidden;flex-direction:column;
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
:where(.slide) :where(h1,h2,h3,h4,p,figure,blockquote){margin:0}
:where(.slide) :where(ul,ol){margin:0;padding:0;list-style:none}
:where(.slide) svg{display:block;overflow:visible}
.slide .fit{flex:1;min-height:0;display:flex;flex-direction:column}
/* ── приёмы AuditLens ── */
.body{flex:1;min-height:0;display:flex;flex-direction:column;padding:84px 96px 72px}
.body.split{flex-direction:row;gap:64px;align-items:center}
.body.center{justify-content:center}
.title{font-family:var(--serif);font-weight:600;font-size:68px;letter-spacing:-.015em;line-height:1.08;text-wrap:balance}
.sub{font-size:36px;line-height:1.5;color:var(--ink-2);margin-top:36px;max-width:1500px;text-wrap:pretty}
.sub.it{font-family:var(--serif);font-style:italic}
.statement{font-family:var(--display);font-size:46px;line-height:1.25;max-width:1600px;text-wrap:balance}
.statement.strong{font-family:var(--serif);font-style:italic;font-weight:600}
.slide em{font-family:var(--italic);font-style:italic;color:var(--accent)}
.pin{margin-top:auto}
.eyebrow{font-family:var(--mono);font-size:27px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
.small{font-family:var(--mono);font-size:25px;color:var(--ink-3);line-height:1.4}
.rows{display:flex;flex-direction:column}
.row{display:flex;gap:32px;padding:24px 0;border-top:1px solid var(--hair);align-items:baseline}
.rows>.row:last-child{border-bottom:1px solid var(--hair)}
.row .n{font-family:var(--mono);font-size:27px;color:var(--ink-4);flex:none;width:64px}
.row .t{font-weight:500;font-size:30px;line-height:1.35}
.row .t.serif{font-family:var(--serif);font-weight:600}
.row .d{font-size:28px;color:var(--ink-2);line-height:1.5;margin-top:5px}
.row .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);flex:none;transform:translateY(-5px)}
.row .tag{font-family:var(--mono);font-size:25px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;flex:none;width:190px}
.strip{display:flex;border:1px solid var(--hair);border-radius:16px;overflow:hidden}
.strip .cell{flex:1;padding:28px 30px;min-width:0}
.strip .cell+.cell{border-left:1px solid var(--hair)}
.num{font-family:var(--display);font-size:52px;line-height:1;font-variant-numeric:lining-nums}
.cap{font-family:var(--mono);font-size:25px;color:var(--ink-3);margin-top:12px;line-height:1.35}
.hero{font-family:var(--display);font-size:116px;line-height:1;letter-spacing:-.02em;font-variant-numeric:lining-nums}
.hero.xl{font-size:200px}
.unit{font-family:var(--mono);font-size:34px;letter-spacing:0;color:var(--ink-3)}
.vr{width:1px;background:var(--hair);align-self:stretch;flex:none}
.tbl{width:100%;border-collapse:collapse}
.tbl th{text-align:left;font-family:var(--mono);font-size:25px;letter-spacing:.05em;text-transform:uppercase;
color:var(--ink-3);font-weight:500;padding:0 24px 22px 0;border-bottom:1.5px solid var(--ink)}
.tbl td{padding:24px 24px 24px 0;border-bottom:1px solid var(--hair);font-size:28px;line-height:1.4;vertical-align:top}
.tbl td.v{font-family:var(--mono);font-size:30px;color:var(--ink-2);font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl tr.hl td{background:var(--accent-soft)}
.pill{display:inline-block;padding:12px 22px;border:1px solid var(--hair-2);border-radius:999px;background:var(--surface);
font-family:var(--mono);font-size:25px;white-space:nowrap}
.card{border:1px solid var(--hair-2);border-radius:14px;background:var(--surface);box-shadow:0 8px 28px rgba(0,0,0,.08)}
.quote{font-family:var(--serif);font-style:italic;font-size:40px;line-height:1.38;text-wrap:pretty}
.accent{color:var(--accent)}.pos{color:var(--pos)}.warn{color:var(--warn)}.sber{color:var(--sber)}
.ink2{color:var(--ink-2)}.muted{color:var(--ink-3)}.faint{color:var(--ink-4)}
sup.cite{font-family:var(--mono);font-style:normal;font-size:max(15px,.42em);font-weight:500;letter-spacing:0;color:var(--ink-3);
vertical-align:super;line-height:0;margin-left:.16em;white-space:nowrap}
"""
# Правила оболочки идут ПОСЛЕ стилей модели и с !important: листание, масштаб,
# шапка и подписи не должны зависеть от того, что написал дизайнер.
_SHELL_CSS = """
.slide{display:none!important;position:absolute!important;left:50%!important;top:50%!important;
width:1920px!important;height:1080px!important;margin:-540px 0 0 -960px!important;padding:0!important;
transform:scale(var(--k,1))!important;transform-origin:center center!important;overflow:hidden!important;
flex-direction:column!important}
.slide.on{display:flex!important}
.al-bar{flex:none;height:76px;display:flex;align-items:center;gap:20px;padding:0 96px;border-bottom:1px solid var(--hair)}
.al-brand{display:flex;align-items:center;gap:9px;flex:none;font-family:var(--sans);font-weight:600;font-size:27px;color:var(--ink)}
.al-brand svg{width:21px!important;height:21px!important;position:static!important;flex:none}
.al-crumb{flex:1;min-width:0;font-family:var(--mono);font-size:27px;letter-spacing:.05em;text-transform:uppercase;
color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.al-ctx{flex:none;display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:25px;color:var(--ink-3);white-space:nowrap}
.al-ctx i{width:7px;height:7px;border-radius:50%;background:var(--pos)}
.al-water{position:absolute!important;top:-140px!important;right:-120px!important;left:auto!important;
width:620px!important;height:620px!important;opacity:.045!important;pointer-events:none}
.al-cvtop{flex:none;padding:56px 96px 0;display:flex;align-items:center;gap:11px;position:relative;z-index:1;
font-family:var(--sans);font-weight:600;font-size:24px;letter-spacing:-.01em}
.al-cvtop svg{width:30px!important;height:30px!important;position:static!important;flex:none}
.al-cvtop span{font-family:var(--mono);font-weight:400;font-size:24px;color:var(--ink-4);letter-spacing:0;margin-left:2px}
.cover .fit{justify-content:center;padding:0 96px;position:relative;z-index:1}
.cover .eyebrow{margin-bottom:30px}
.cover h1{font-family:var(--display);font-weight:400;font-size:104px;line-height:1.08;letter-spacing:-.02em;max-width:1620px;text-wrap:balance}
.cover .sub{margin-top:34px;max-width:1200px}
.al-cvbot{flex:none;padding:0 96px 64px;display:flex;align-items:baseline;justify-content:space-between;gap:40px;
position:relative;z-index:1;font-family:var(--mono);font-size:25px;color:var(--ink-3)}
.al-src .body{padding-top:72px}
.al-src ol{display:grid;grid-template-columns:1fr 1fr;gap:0 72px;margin-top:40px}
.al-src li{display:grid;grid-template-columns:70px 1fr;gap:0 8px;padding:13px 0;border-top:1px solid var(--hair);
font-size:23px;line-height:1.35;color:var(--ink);break-inside:avoid}
.al-src li b{font-family:var(--mono);font-weight:500;font-size:22px;color:var(--accent)}
.al-src li span{color:var(--ink-3)}
.al-dense ol{margin-top:28px}
.al-dense li{padding:9px 0;font-size:20px;grid-template-columns:60px 1fr}
.al-dense li b{font-size:19px}
.al-note{margin-top:auto;padding-top:22px;border-top:1px solid var(--hair-2);font-family:var(--mono);font-size:22px;
line-height:1.45;color:var(--ink-3)}
.al-ui{position:fixed;left:0;right:0;bottom:0;height:3px;background:rgba(255,255,255,.08);z-index:9}
.al-ui i{display:block;height:100%;width:0;background:#D0393B;transition:width .25s ease}
.al-hint{position:fixed;right:14px;bottom:10px;z-index:9;font:500 11px/1 "JetBrains Mono",ui-monospace,Menlo,monospace;
letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.42);opacity:0;transition:opacity .3s}
body:hover .al-hint{opacity:1}
@media print{
@page{size:1920px 1080px;margin:0}
html,body{height:auto;overflow:visible;background:#fff}
.slide{display:flex!important;position:relative!important;left:auto!important;top:auto!important;margin:0!important;
transform:none!important;page-break-after:always;break-after:page;-webkit-print-color-adjust:exact;print-color-adjust:exact}
.al-ui,.al-hint{display:none!important}}
"""
_JS = """(function(){
var s=[].slice.call(document.querySelectorAll('.slide')),i=0,bar=document.getElementById('bar');
function scale(){document.documentElement.style.setProperty('--k',Math.min(innerWidth/1920,innerHeight/1080));}
function show(n){i=Math.max(0,Math.min(s.length-1,n));
s.forEach(function(el,j){el.classList.toggle('on',j===i);});
bar.style.width=((i+1)/s.length*100)+'%';
try{history.replaceState(null,'','#'+(i+1));}catch(e){}}
function shrink(){s.forEach(function(el){var f=el.querySelector('.fit');if(!f)return;
el.style.setProperty('display','flex','important');
var r=Math.min(f.clientWidth/Math.max(f.clientWidth,f.scrollWidth),f.clientHeight/Math.max(f.clientHeight,f.scrollHeight));
if(r<0.995){var w=f.clientWidth,h=f.clientHeight;f.style.flex='none';f.style.transformOrigin='0 0';
f.style.transform='scale('+r+')';f.style.width=(w/r)+'px';f.style.height=(h/r)+'px';
f.style.marginBottom=(h-h/r)+'px';f.style.marginRight=(w-w/r)+'px';}
el.style.removeProperty('display');});}
addEventListener('keydown',function(e){var k=e.key;
if(k==='ArrowRight'||k==='ArrowDown'||k==='PageDown'||k===' '||k==='Enter'){e.preventDefault();show(i+1);}
else if(k==='ArrowLeft'||k==='ArrowUp'||k==='PageUp'||k==='Backspace'){e.preventDefault();show(i-1);}
else if(k==='Home')show(0);else if(k==='End')show(s.length-1);
else if(/^[fFаА]$/.test(k)){var d=document.documentElement;
if(document.fullscreenElement)document.exitFullscreen();else if(d.requestFullscreen)d.requestFullscreen();}
else if(/^[pPзЗ]$/.test(k))print();});
document.addEventListener('click',function(e){show(i+(e.clientX>innerWidth/2?1:-1));});
var x0=null;addEventListener('touchstart',function(e){x0=e.touches[0].clientX;},{passive:true});
addEventListener('touchend',function(e){if(x0===null)return;var dx=e.changedTouches[0].clientX-x0;
if(Math.abs(dx)>40)show(i+(dx<0?1:-1));x0=null;},{passive:true});
addEventListener('resize',scale);scale();
function start(){shrink();show(Math.max(0,(parseInt(location.hash.slice(1),10)||1)-1));}
if(document.fonts&&document.fonts.ready)document.fonts.ready.then(start);else start();
})();"""
_MARK = ('<svg viewBox="0 0 100 100" aria-hidden="true"><path fill="#1F4DFF" d="M47.5 13 L59.5 13 L89.5 89 L75.5 89 Z"/>'
         '<path fill="currentColor" fill-rule="evenodd" d="M47.5 13 L57.5 13 L83.5 89 L66.5 89 L58.5 67 L36.5 67 L27.5 89 '
         'L10.5 89 Z M47.5 36 L56.5 58 L38.5 58 Z"/></svg>')
_WATER = ('<svg class="al-water" viewBox="0 0 100 100" aria-hidden="true"><path fill="currentColor" d="M47.5 13 L59.5 13 L89.5 89 '
          'L75.5 89 Z"/><path fill="currentColor" fill-rule="evenodd" d="M47.5 13 L57.5 13 L83.5 89 L66.5 89 L58.5 67 L36.5 67 '
          'L27.5 89 L10.5 89 Z M47.5 36 L56.5 58 L38.5 58 Z"/></svg>')


def _esc(v) -> str:
    return _html.escape(str(v or ""), quote=True)


_LABEL = re.compile(r'data-label="([^"]*)"')


def _wrap(slide: str, n: int, total: int, *, context: str, day: str) -> str:
    """Шапка, обложка и обёртка для подгонки — кодом: знак и нумерация точны всегда."""
    m = _OPEN.match(slide)
    attrs = m.group(1) if m else ' class="slide"'
    inner = slide[m.end():].rsplit("</section>", 1)[0] if m else slide
    if re.search(r'class="[^"]*\bcover\b', attrs):
        return (f'<section{attrs}>{_WATER}<div class="al-cvtop">{_MARK}AuditLens<span>ИИ-помощник · отчёт</span></div>'
                f'<div class="fit">{inner}</div>'
                f'<div class="al-cvbot"><span>Подготовлено ИИ-помощником AuditLens по проверенным источникам</span>'
                f"<span>{_esc(day)}</span></div></section>")
    lab = _LABEL.search(attrs)
    label = _html.unescape(lab.group(1)).strip()[:40] if lab else ""
    crumb = f"{n:02d} / {total:02d}" + (f" — {_esc(label)}" if label else "")
    return (f'<section{attrs}><div class="al-bar"><span class="al-brand">{_MARK}AuditLens</span>'
            f'<span class="al-crumb">{crumb}</span><span class="al-ctx"><i></i>{_esc(context)}</span></div>'
            f'<div class="fit">{inner}</div></section>')


def sources_slides(sources: list[dict] | None, used: list[int], note: str = "",
                   per_page: int = 24) -> list[str]:
    by_n = {int(s["n"]): s for s in (sources or []) if str(s.get("n") or "").isdigit()}
    rows = [(n, by_n[n]) for n in used if n in by_n]
    if not rows and not note:
        return []
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    out = []
    for k, page in enumerate(pages):
        items = "".join(
            f"<li><b>{n}</b><div>{_esc(str(s.get('title') or s.get('bank_name') or s.get('domain') or '')[:96])}"
            f"<span> — {_esc(' · '.join(x for x in (str(s.get('domain') or ''), _ru_day(str(s.get('published') or ''))) if x))}</span></div></li>"
            for n, s in page)
        tail = f'<p class="al-note">{_esc(note)}</p>' if note and k == len(pages) - 1 else ""
        head = "Источники" if len(pages) == 1 else f"Источники · {k + 1}"
        dense = " al-dense" if len(page) > 16 else ""
        out.append(f'<section class="slide al-src{dense}" data-label="Источники"><div class="body">'
                   f'<h1 class="title">{head}</h1><ol>{items}</ol>{tail}</div></section>')
    return out


def assemble(css: str, slides: list[str], *, title: str, sources: list[dict] | None = None,
             stamp: str = "", note: str = "", when: datetime | None = None) -> str:
    """Очищенные стили и слайды → один самодостаточный HTML-файл."""
    known = {int(s["n"]) for s in (sources or []) if str(s.get("n") or "").isdigit()}
    used = sorted({n for s in slides for n in cites(visible_text(s))} & known) if known else []
    body = [link_citations(s, known) for s in slides] + sources_slides(sources, used, note)
    total = len(body)
    day = (when or datetime.now()).strftime("%d.%m.%Y")
    context = title if len(title) <= 42 else title[:41].rstrip(" ,:;—-") + "…"
    wrapped = "\n".join(_wrap(s, i, total, context=context, day=day)
                        for i, s in enumerate(body, start=1))
    return (
        '<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)} · AuditLens</title>"
        f'<meta name="generator" content="{_esc(stamp)}">'
        f"<style>{_font_css()}{_BASE_CSS}</style>\n<style>{css}</style>\n<style>{_SHELL_CSS}</style>"
        f'</head><body>\n<main id="deck">\n{wrapped}\n</main>\n'
        '<div class="al-ui"><i id="bar"></i></div>'
        '<div class="al-hint">← → листать · F — на весь экран · P — печать в PDF</div>\n'
        f"<script>{_JS}</script></body></html>\n")


# ── Исправление чисел без вмешательства в макет ──────────────────────────────
_FIX_SYSTEM = """В слайдах презентации есть числа, которых нет в отчёте (их посчитали или округлили).
Для каждого дай точечную замену текста. Верни строго JSON:
{"fixes": [{"find": "фрагмент текста слайда с этим числом, дословно, 8–80 знаков", "replace": "тот же фрагмент с числом из отчёта или без числа"}]}
Правила: меняй только текст, никаких тегов; "find" должен встречаться в слайдах дословно; в
"replace" не должно быть чисел, которых нет в отчёте. Если число верное, но записано иначе
(другие пробелы, знак процента) — приведи к записи отчёта. Только JSON."""


def _context(slides: list[str], number: str, width: int = 70) -> list[str]:
    """Куски текста слайдов вокруг числа — чтобы модель видела, что править."""
    pat = re.compile(r"[\s  ]?".join(re.escape(ch) for ch in number).replace(r"\.", "[.,]"))
    out = []
    for s in slides:
        text = re.sub(r"\s+", " ", visible_text(s))
        for m in pat.finditer(text):
            out.append(text[max(0, m.start() - width):m.end() + width].strip())
            if len(out) >= 2:
                return out
    return out


def apply_fixes(slides: list[str], fixes: list[dict], report: str) -> tuple[list[str], int]:
    """Точечные замены текста. Применяется только то, что встречается ровно один
    раз вне тегов и не вносит новых чужих чисел — макет не меняется."""
    base = numbers(report)
    done = 0
    for fx in fixes or []:
        find, repl = str(fx.get("find") or ""), str(fx.get("replace") or "")
        if len(find) < 4 or "<" in find or "<" in repl or ">" in repl:
            continue
        if not numbers(repl) <= base:
            continue
        esc = _html.escape(find, quote=False)
        for needle in (find, esc):
            hits = [i for i, s in enumerate(slides) if s.count(needle) == 1 and
                    needle in re.sub(r"<[^>]+>", "\x00", s)]
            if len(hits) == 1:
                slides[hits[0]] = slides[hits[0]].replace(
                    needle, repl if needle == find else _html.escape(repl, quote=False))
                done += 1
                break
    return slides, done


async def _fix_numbers(client, model: str, slides: list[str], unknown: list[str],
                       report: str) -> tuple[list[str], int]:
    from .facts import call_model
    listing = "\n".join(f"- {n}: " + " | ".join(f"«…{c}…»" for c in _context(slides, n))
                        for n in unknown[:25])
    user = (f"# Числа слайдов, которых нет в отчёте\n{listing}\n\n# Отчёт\n"
            f"{clean_report(report)[:_REPORT_CHARS]}")
    kw = {"model": model, "max_tokens": 2500,
          "messages": [{"role": "system", "content": _FIX_SYSTEM},
                       {"role": "user", "content": user}]}
    resp = await call_model(client, model, kw)
    m = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.S)
    try:
        fixes = (json.loads(m.group(0)) if m else {}).get("fixes") or []
    except ValueError:
        fixes = []
    return apply_fixes(slides, fixes if isinstance(fixes, list) else [], report)


# ── Сборка ───────────────────────────────────────────────────────────────────
async def build(client, *, question: str, title: str, report: str,
                sources: list[dict] | None = None, when: datetime | None = None,
                model: str | None = None) -> tuple[bytes, dict]:
    """Отчёт → самодостаточный HTML. Возвращает файл и сводку."""
    from ...web import export_brand as B
    from .facts import call_model
    use = model or model_for()
    text = clean_report(report)
    kw = {"model": use, "max_tokens": _MAX_TOKENS,
          "messages": [{"role": "system", "content": _SYSTEM},
                       {"role": "user", "content": user_message(
                           question=question, title=title, report=report, sources=sources,
                           when=when)}]}
    effort = os.getenv("GPTR_DECK_REASONING_EFFORT", "medium")
    if effort not in ("", "default"):
        kw["extra_body"] = {"reasoning_effort": effort}
    resp = await call_model(client, use, kw)
    css, raw = split_answer(resp.choices[0].message.content or "")
    slides = [s for s in (sanitize(x) for x in raw) if len(visible_text(s).strip()) > 3]
    if len(slides) < 4:
        raise RuntimeError(f"модель вернула слайдов: {len(slides)}")
    unknown = unknown_numbers(slides, text)
    fixed = 0
    if unknown:
        try:
            slides, fixed = await _fix_numbers(client, use, slides, unknown, text)
        except Exception as e:  # noqa: BLE001 — исправление необязательно
            log.info("презентация: числа не исправлены — %s", type(e).__name__)
        unknown = unknown_numbers(slides, text)
    # Что исправить не удалось — только в журнал: пометка «сверьте вручную» на
    # слайде и в ответе портила впечатление и чаще всего была ложной (владелец 11.11).
    html_doc = assemble(clean_css(css), slides, title=title or "Отчёт AuditLens", sources=sources,
                        stamp=B.stamp_line(when), when=when)
    info = {"slides": len(slides), "unknown": unknown[:12], "fixed": fixed, "model": use,
            "kb": round(len(html_doc.encode("utf-8")) / 1024)}
    log.info("презентация html: %s", {k: v for k, v in info.items() if k != "unknown"} |
             {"unknown": len(unknown)})
    return html_doc.encode("utf-8"), info
