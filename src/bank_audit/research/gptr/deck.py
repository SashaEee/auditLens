"""Презентация по готовому отчёту: план слайдов — модель, сборка и числа — код.

ЗАЧЕМ. Аудит 11.11: презентацию просили 21 раз за шесть недель, файл получили
трижды. Отчёт файлов не делал вовсе, быстрый агент собирал слайды самодельным
скриптом — когда получалось.

Как устроено:
  1. plan_slides — модель читает ГОТОВЫЙ отчёт и возвращает план: заголовок-
     утверждение, пункты, главные числа, таблица, «что проверить». Нового она
     не ищет и не считает;
  2. check — код сверяет каждое число каждого пункта с текстом отчёта. Пункт
     с числом, которого в отчёте нет, убирается: слайд не может сказать
     больше, чем отчёт;
  3. render — код собирает PPTX в палитре AuditLens: титул, слайды, источники,
     на которые слайды сослались, штамп выгрузки. Текст остаётся редактируемым.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from datetime import datetime

log = logging.getLogger(__name__)

MAX_SLIDES = 10
MAX_BULLETS = 5
_REPORT_CHARS = int(os.getenv("GPTR_DECK_REPORT_CHARS", "48000"))
KINDS = ("bullets", "numbers", "table", "checks")


def model_for() -> str:
    return (os.getenv("GPTR_DECK_MODEL") or os.getenv("LLM_MODEL_ANALYST")
            or os.getenv("LLM_MODEL_REASONING") or os.environ.get("LLM_MODEL_NAME", ""))


_SYSTEM = """Ты готовишь презентацию для руководителя проверки внутреннего аудита Сбербанка
по ГОТОВОМУ отчёту. Нового не ищешь и не считаешь: только то, что есть в отчёте.

Верни строго JSON:
{"title": "...", "subtitle": "...", "slides": [ ... ]}

Виды слайдов:
  {"kind": "bullets", "title": "...", "bullets": ["..."], "note": ""}
  {"kind": "numbers", "title": "...", "items": [{"value": "18,4%", "label": "...", "src": "[6]"}], "note": ""}
  {"kind": "table",   "title": "...", "columns": ["..."], "rows": [["..."]], "note": ""}
  {"kind": "checks",  "title": "Что проверить", "bullets": ["..."]}

Правила:
  • 6–9 слайдов. Первый — главный вывод отчёта (bullets, 3–4 пункта). Если в отчёте есть
    «Что проверить» или «Что проверять» — последним слайд checks с его действиями.
  • Заголовок слайда — утверждение, а не тема: «Сбер дешевле по нижней границе ПСК», а не
    «Сравнение ставок». До 80 знаков.
  • Пункт — одна мысль, до 150 знаков, с числом и ссылкой [n] как в отчёте. До 5 пунктов.
  • Числа и ссылки [n] бери ТОЛЬКО из отчёта и дословно: не округляй, не пересчитывай, не
    складывай. Код сверит каждое число с текстом отчёта и уберёт пункт с числом, которого
    там нет.
  • table — когда в отчёте есть сравнение по организациям или периодам: до 5 столбцов и
    7 строк, в ячейке — коротко, со ссылкой [n].
  • numbers — 2–4 главных числа отчёта: значение как в отчёте, подпись — что это и на какую
    дату, src — ссылка [n].
  • note — одна оговорка отчёта, если она важна для слайда (разные даты замеров, данных
    нет), иначе пустая строка.
  • subtitle — вопрос аудитора одной строкой. Без слайдов «Спасибо» и «Вопросы»; источники
    добавит код.
Только JSON, без пояснений."""


def clean_report(md: str) -> str:
    """Отчёт для модели и для сверки: без маркеров блоков и служебных меток."""
    text = re.sub(r"\[\[(VIZ|CHART|FILE):[^\]]*\]\]", "", md or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse(raw: str) -> dict:
    """План слайдов из ответа модели. Мусор и пустые слайды отбрасываются."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def s(x, n: int) -> str:
        return re.sub(r"\s+", " ", str(x or "")).strip()[:n]

    slides: list[dict] = []
    for it in data.get("slides") or []:
        if not isinstance(it, dict) or it.get("kind") not in KINDS:
            continue
        kind = it["kind"]
        out: dict = {"kind": kind, "title": s(it.get("title"), 110), "note": s(it.get("note"), 220)}
        if kind in ("bullets", "checks"):
            out["bullets"] = [b for b in (s(x, 260) for x in it.get("bullets") or []) if b][:MAX_BULLETS + 1]
            if not out["bullets"]:
                continue
        elif kind == "numbers":
            items = []
            for x in it.get("items") or []:
                if isinstance(x, dict) and s(x.get("value"), 24):
                    items.append({"value": s(x.get("value"), 24), "label": s(x.get("label"), 120),
                                  "src": s(x.get("src"), 20)})
            out["items"] = items[:4]
            if not out["items"]:
                continue
        else:
            cols = [s(c, 40) for c in it.get("columns") or []][:5]
            rows = [[s(c, 120) for c in r][:len(cols)] for r in it.get("rows") or []
                    if isinstance(r, list)][:7]
            rows = [r + [""] * (len(cols) - len(r)) for r in rows if any(r)]
            if len(cols) < 2 or not rows:
                continue
            out["columns"], out["rows"] = cols, rows
        if not out["title"]:
            continue
        slides.append(out)
        if len(slides) >= MAX_SLIDES:
            break
    return {"title": s(data.get("title"), 120), "subtitle": s(data.get("subtitle"), 220),
            "slides": slides}


# ── Сверка чисел ─────────────────────────────────────────────────────────────
_CITE = re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]")
# Пробел внутри числа — только разделитель тысяч («5 000 000»): иначе год и
# соседнее число из другой ячейки склеивались в «20266».
_NUM = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?(?!\d)")


def numbers(text: str) -> set[str]:
    """Числа текста в едином виде: без пробелов, запятая → точка. Ссылки [n] не
    числа; малые целые (до 12) не сверяем — «три банка» в отчёте бывает словом."""
    out: set[str] = set()
    for m in _NUM.findall(_CITE.sub(" ¦ ", text or "")):
        n = re.sub(r"[\s  ]", "", m).replace(",", ".").rstrip(".")
        if not n:
            continue
        if "." not in n and len(n) <= 2 and int(n) <= 12:
            continue
        out.add(n)
    return out


def cites(text: str) -> list[int]:
    return [int(x) for grp in _CITE.findall(text or "") for x in re.findall(r"\d+", grp)]


def check(plan: dict, report: str, known_sources: set[int] | None = None) -> dict:
    """Убирает из плана всё, чего нет в отчёте. Возвращает план и счётчик снятого."""
    base = numbers(report)
    dropped = 0

    def ok(text: str) -> bool:
        return numbers(text) <= base

    def fix_cites(text: str) -> str:
        if known_sources is None:
            return text
        return _CITE.sub(lambda m: m.group(0) if all(
            int(x) in known_sources for x in re.findall(r"\d+", m.group(0))) else "", text).strip()

    slides: list[dict] = []
    for sl in plan.get("slides") or []:
        sl = dict(sl)
        if not ok(sl.get("title") or ""):
            dropped += 1
            continue
        if sl.get("note") and not ok(sl["note"]):
            sl["note"] = ""
        if sl["kind"] in ("bullets", "checks"):
            keep = [fix_cites(b) for b in sl["bullets"] if ok(b)]
            dropped += len(sl["bullets"]) - len(keep)
            sl["bullets"] = keep[:MAX_BULLETS]
            if not sl["bullets"]:
                continue
        elif sl["kind"] == "numbers":
            keep = [dict(it, src=fix_cites(it.get("src") or "")) for it in sl["items"]
                    if ok(it["value"]) and ok(it.get("label") or "")]
            dropped += len(sl["items"]) - len(keep)
            sl["items"] = keep
            if len(sl["items"]) < 2:
                # одно число — не слайд чисел: отдаём его пунктом
                if sl["items"]:
                    it = sl["items"][0]
                    sl = {"kind": "bullets", "title": sl["title"], "note": sl.get("note", ""),
                          "bullets": [f"{it['label']}: {it['value']} {it.get('src') or ''}".strip()]}
                else:
                    continue
        else:
            rows = []
            for r in sl["rows"]:
                cells = [fix_cites(c) if ok(c) else "—" for c in r]
                dropped += sum(1 for a, b in zip(r, cells) if a and b == "—" and a != "—")
                rows.append(cells)
            sl["rows"] = [r for r in rows if any(c and c != "—" for c in r[1:])]
            if not sl["rows"]:
                continue
        slides.append(sl)
    return {**plan, "slides": slides, "dropped": dropped}


async def plan_slides(client, model: str, *, question: str, title: str, report: str) -> dict:
    """Просит у модели план слайдов по отчёту. Пустой план — если она не ответила."""
    body = clean_report(report)[:_REPORT_CHARS]
    user = (f"# Название отчёта\n{title or '—'}\n\n# Вопрос аудитора\n{question or '—'}\n\n"
            f"# Отчёт\n{body}")
    kw = {"model": model, "max_tokens": 3500,
          "messages": [{"role": "system", "content": _SYSTEM},
                       {"role": "user", "content": user}]}
    from .facts import call_model
    resp = await call_model(client, model, kw)
    return parse(resp.choices[0].message.content or "")


def fallback_plan(title: str, question: str, report: str) -> dict:
    """План без модели: по заголовкам отчёта и первым абзацам разделов."""
    slides: list[dict] = []
    for m in re.finditer(r"(?m)^##\s+(.+?)\s*\n(.*?)(?=^##\s|\Z)", clean_report(report), re.S):
        head, text = m.group(1).strip(), m.group(2)
        if head.lower().startswith("честные пробелы"):
            continue
        items = [re.sub(r"^\s*(?:\d+[.)]|[-•*])\s*", "", ln).strip()
                 for ln in text.splitlines() if re.match(r"^\s*(?:\d+[.)]|[-•*])\s+\S", ln)]
        if not items:
            paras = [p.strip() for p in re.split(r"\n\s*\n", text)
                     if p.strip() and not p.lstrip().startswith(("#", "|", ">"))]
            items = [re.split(r"(?<=[.!?])\s+", p)[0] for p in paras[:MAX_BULLETS]]
        items = [re.sub(r"\*\*|__", "", x)[:240] for x in items if len(x) > 12][:MAX_BULLETS]
        if items:
            kind = "checks" if head.lower().startswith("что провер") else "bullets"
            slides.append({"kind": kind, "title": head[:110], "bullets": items, "note": ""})
        if len(slides) >= MAX_SLIDES:
            break
    return {"title": title, "subtitle": question, "slides": slides}


# ── Сборка PPTX ──────────────────────────────────────────────────────────────
_W, _H = 13.333, 7.5          # дюймы, 16:9
_TITLE_FONT, _BODY_FONT = "Georgia", "Arial"


def _rgb(hex_: str):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hex_)


def _box(slide, x, y, w, h, text, *, size=16, bold=False, color="0D1014", font=_BODY_FONT,
         align="left", line=1.12):
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, side, 0)
    p = tf.paragraphs[0]
    p.alignment = {"left": PP_ALIGN.LEFT, "right": PP_ALIGN.RIGHT, "center": PP_ALIGN.CENTER}[align]
    p.line_spacing = line
    r = p.add_run()
    r.text = text
    r.font.size, r.font.bold, r.font.name = Pt(size), bold, font
    r.font.color.rgb = _rgb(color)
    return tb


def _rect(slide, x, y, w, h, fill: str, line: str | None = None):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches
    sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = _rgb(fill)
    if line:
        sh.line.color.rgb = _rgb(line)
    else:
        sh.line.fill.background()
    sh.shadow.inherit = False
    return sh


def _lines(text: str, per_line: int) -> int:
    """Грубая оценка числа строк: чтобы соседние блоки не наезжали друг на друга."""
    return max(1, sum(max(1, -(-len(part) // per_line)) for part in (text or "").split("\n")))


def _per_line(width_in: float, size: int, serif_bold: bool = False) -> int:
    """Сколько знаков помещается в строку. Средняя ширина знака кириллицы — 0,62 кегля
    у полужирной Georgia и 0,54 у Arial; берём с запасом: лишняя строка в оценке
    даёт пустое место, а недостающая — наложение текста."""
    return max(12, int(width_in * 72 / ((0.62 if serif_bold else 0.54) * size)))


def _ru_day(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else ""


def file_name(title: str) -> str:
    """Имя файла презентации по названию отчёта."""
    stem = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", title or "").strip(" .")[:90] or "Отчёт AuditLens"
    return f"{stem}.pptx"


def _title_size(text: str) -> int:
    return 30 if len(text) <= 48 else 26 if len(text) <= 80 else 22


def render(plan: dict, *, sources: list[dict] | None = None, report_title: str = "",
           when: datetime | None = None) -> bytes:
    """Собирает PPTX по проверенному плану слайдов."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    from ...web import export_brand as B
    C = B.C
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(_W), Inches(_H)
    blank = prs.slide_layouts[6]
    title = plan.get("title") or report_title or "Отчёт AuditLens"
    stamp = B.stamp_line(when)
    slides = plan.get("slides") or []
    used = sorted({n for sl in slides for n in cites(json.dumps(sl, ensure_ascii=False))})
    by_n = {int(s_.get("n")): s_ for s_ in (sources or []) if str(s_.get("n") or "").isdigit()}
    src_rows = [(n, by_n[n]) for n in used if n in by_n]
    src_pages = [src_rows[i:i + 12] for i in range(0, len(src_rows), 12)]
    total = 1 + len(slides) + len(src_pages)

    def base(n: int):
        sl = prs.slides.add_slide(blank)
        _rect(sl, 0, 0, _W, _H, C["paper"])
        _rect(sl, 0.7, _H - 0.62, _W - 1.4, 0.012, C["hair2"])
        _box(sl, 0.7, _H - 0.5, 8.5, 0.3, f"AuditLens · {title[:90]}", size=10, color=C["ink3"])
        _box(sl, _W - 2.7, _H - 0.5, 2.0, 0.3, f"{n} / {total}", size=10, color=C["ink3"],
             align="right")
        return sl

    def head(sl, text: str) -> float:
        """Заголовок слайда с красной меткой слева. Возвращает y под заголовком."""
        size = _title_size(text)
        rows = _lines(text, _per_line(_W - 1.9, size, True))
        h = rows * size * 1.2 / 72
        _rect(sl, 0.7, 0.72, 0.07, h, C["accent"])
        _box(sl, 0.95, 0.66, _W - 1.9, h + 0.1, text, size=size, bold=True, font=_TITLE_FONT)
        return 0.72 + h + 0.38

    # ── титул ──
    sl = prs.slides.add_slide(blank)
    _rect(sl, 0, 0, _W, _H, C["paper"])
    # знак рисуется на белом — под него белая полоса во всю ширину
    _rect(sl, 0, 0, _W, 1.5, C["surface"])
    _rect(sl, 0, 1.5, _W, 0.012, C["hair2"])
    try:
        sl.shapes.add_picture(io.BytesIO(B.logo_png(96)), Inches(0.7), Inches(0.54), height=Inches(0.42))
    except Exception as e:  # noqa: BLE001 — без знака презентация всё равно годна
        log.info("презентация: знак не вставлен — %s", type(e).__name__)
        _box(sl, 0.7, 0.55, 4, 0.4, "AuditLens", size=16, bold=True)
    _box(sl, 0.7, 2.35, 9, 0.3, "ИИ-ПОМОЩНИК · ОТЧЁТ", size=11, bold=True, color=C["accent"])
    t_size = 40 if len(title) <= 40 else 34 if len(title) <= 70 else 28
    t_rows = _lines(title, _per_line(_W - 1.6, t_size, True))
    _box(sl, 0.7, 2.75, _W - 1.6, t_rows * t_size * 1.18 / 72 + 0.1, title, size=t_size,
         bold=True, font=_TITLE_FONT, line=1.08)
    y = 2.75 + t_rows * t_size * 1.18 / 72 + 0.35
    if plan.get("subtitle"):
        _box(sl, 0.7, y, _W - 2.6, 1.2, plan["subtitle"], size=16, color=C["ink2"])
    _rect(sl, 0.7, _H - 1.0, _W - 1.4, 0.012, C["hair2"])
    _box(sl, 0.7, _H - 0.85, _W - 1.4, 0.3, stamp, size=11, color=C["ink3"])

    # ── содержательные слайды ──
    for i, spec in enumerate(slides, start=2):
        sl = base(i)
        y = head(sl, spec["title"])
        note = spec.get("note") or ""
        floor = _H - (1.25 if note else 0.85)
        kind = spec["kind"]
        if kind in ("bullets", "checks"):
            items = spec["bullets"]
            size = 18 if sum(len(b) for b in items) < 520 else 16
            per = _per_line(_W - 2.45, size)
            need = sum(_lines(b, per) * size * 1.25 / 72 + 0.22 for b in items)
            gap = min(0.3, max(0.0, (floor - y - need) / max(1, len(items))))
            for k, b in enumerate(items, start=1):
                h = _lines(b, per) * size * 1.25 / 72
                if kind == "checks":
                    _box(sl, 0.95, y - 0.02, 0.5, 0.4, f"{k}.", size=size, bold=True,
                         color=C["accent"], font=_TITLE_FONT)
                else:
                    _rect(sl, 1.0, y + size * 0.42 / 72, 0.1, 0.1, C["accent"])
                _box(sl, 1.45, y, _W - 2.45, h + 0.08, b, size=size, line=1.2)
                y += h + 0.22 + gap
        elif kind == "numbers":
            items = spec["items"]
            n = len(items)
            gapx = 0.35
            w = (_W - 1.9 - gapx * (n - 1)) / n
            for k, it in enumerate(items):
                x = 0.95 + k * (w + gapx)
                _rect(sl, x, y + 0.1, w, 2.9, C["surface"], line=C["hair2"])
                v_size = 44 if len(it["value"]) <= 8 else 34 if len(it["value"]) <= 13 else 26
                _box(sl, x + 0.3, y + 0.4, w - 0.6, 0.95, it["value"], size=v_size, bold=True,
                     font=_TITLE_FONT, color=C["accent_ink"] if k == 0 else C["ink"])
                _box(sl, x + 0.3, y + 1.45, w - 0.6, 1.1, it.get("label") or "", size=14,
                     color=C["ink2"])
                if it.get("src"):
                    _box(sl, x + 0.3, y + 2.55, w - 0.6, 0.3, it["src"], size=11, color=C["ink3"])
        else:
            cols, rows = spec["columns"], spec["rows"]
            width = _W - 1.9
            first = width * (0.3 if len(cols) > 2 else 0.4)
            other = (width - first) / (len(cols) - 1)
            f_size = 12 if len(rows) > 5 or len(cols) > 4 else 14 if len(rows) <= 4 else 13
            height = min(floor - y, 0.5 + 0.62 * len(rows))
            shape = sl.shapes.add_table(len(rows) + 1, len(cols), Inches(0.95), Inches(y),
                                        Inches(width), Inches(height))
            tbl = shape.table
            for c in range(len(cols)):
                tbl.columns[c].width = Inches(first if c == 0 else other)
            for r in range(len(rows) + 1):
                for c in range(len(cols)):
                    cell = tbl.cell(r, c)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = _rgb(C["ink"] if r == 0 else
                                                    C["surface"] if r % 2 else C["paper2"])
                    tf = cell.text_frame
                    tf.word_wrap = True
                    tf.paragraphs[0].text = ""
                    run = tf.paragraphs[0].add_run()
                    run.text = cols[c] if r == 0 else rows[r - 1][c]
                    run.font.size = Pt(f_size)
                    run.font.name = _BODY_FONT
                    run.font.bold = r == 0 or c == 0
                    run.font.color.rgb = _rgb("FFFFFF" if r == 0 else C["ink"])
                    cell.margin_left = cell.margin_right = Inches(0.1)
                    cell.margin_top = cell.margin_bottom = Inches(0.06)
        if note:
            _box(sl, 0.95, _H - 1.2, _W - 1.9, 0.5, note, size=12, color=C["ink3"])

    # ── источники ──
    for j, page in enumerate(src_pages):
        sl = base(1 + len(slides) + j + 1)
        y = head(sl, "Источники" if len(src_pages) == 1 else f"Источники · {j + 1}")
        for n, s_ in page:
            name = str(s_.get("title") or s_.get("bank_name") or s_.get("domain") or "")[:110]
            dom = str(s_.get("domain") or "")
            day = _ru_day(str(s_.get("published") or ""))
            tail = " — ".join(x for x in (dom, day) if x)
            _box(sl, 0.95, y, 0.6, 0.3, f"[{n}]", size=12, bold=True, color=C["accent_ink"])
            _box(sl, 1.55, y, _W - 2.6, 0.32, name + (f"  ·  {tail}" if tail else ""), size=12,
                 color=C["ink2"])
            y += 0.4
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


async def build(client, *, question: str, title: str, report: str,
                sources: list[dict] | None = None, when: datetime | None = None,
                model: str | None = None) -> tuple[bytes, dict]:
    """Отчёт → PPTX. Возвращает файл и сводку: слайдов, снято пунктов, запасной ли план."""
    text = clean_report(report)
    known = {int(s_["n"]) for s_ in (sources or []) if str(s_.get("n") or "").isdigit()}
    used_fallback = False
    try:
        plan = await plan_slides(client, model or model_for(), question=question, title=title,
                                 report=text)
    except Exception as e:  # noqa: BLE001 — без модели соберём по заголовкам
        log.warning("презентация: план слайдов не получен — %s", str(e)[:140])
        plan = {"slides": []}
    plan = check(plan, text, known or None)
    if len(plan.get("slides") or []) < 3:
        used_fallback = True
        plan = check(fallback_plan(title, question, text), text, known or None)
    plan["title"] = plan.get("title") or title
    plan["subtitle"] = plan.get("subtitle") or question
    data = render(plan, sources=sources, report_title=title, when=when)
    info = {"slides": len(plan.get("slides") or []), "dropped": plan.get("dropped", 0),
            "fallback": used_fallback}
    log.info("презентация: %s", info)
    return data, info
