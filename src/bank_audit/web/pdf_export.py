"""PDF отчёта ИИ-аналитика.

Как устроено:
  • фронт отправляет отчёт, источники и артефакты проверки в /api/ai/export-pdf;
  • сервер собирает HTML: Source Serif 4 для текста, Geist для служебных блоков,
    шрифты — саморазмещённые из /static/vendor (сеть рендеру не нужна);
  • Chromium (Playwright) печатает PDF в два прохода: первый по закладкам
    узнаёт страницу каждого раздела, второй ставит номера в оглавление.

Документ (аудит PDF 29.09): обложка-резюме — название, вопрос, главный вывод,
тезисы, охват источников, оглавление с номерами страниц → разделы с той же
нумерацией → «Ограничения отчёта» → источники группами в две колонки.
Раньше обложкой был сам вопрос, источники занимали треть файла, а одна
битая таблица ужимала весь документ до 7 pt.
"""
from __future__ import annotations
import html as _html
import io
import json
import logging
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..clock import MSK, today_msk

log = logging.getLogger(__name__)

_VENDOR = Path(__file__).parent / "static" / "vendor"
# Шрифты отдаёт перехват запросов в рендере: этот адрес никуда не ходит.
_FONT_HOST = "https://fonts.auditlens.local/"
_PDF_FONTS = ("Geist", "Source Serif 4", "JetBrains Mono")
_PDF_SUBSETS = ("cyrillic", "cyrillic-ext", "latin", "latin-ext")
# Ширина полосы набора A4 при полях 18 мм: 174 мм = 657,6 CSS px.
_PRINT_WIDTH = 658

_MONTHS = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря")


def _esc(s: Any) -> str:
    return _html.escape(str(s or ""))


def _pl(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    if m10 == 1 and m100 != 11:
        return one
    if 2 <= m10 <= 4 and not 12 <= m100 <= 14:
        return few
    return many


def _clip(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"


def _title_size(title: str) -> str:
    """Кегль обложки по длине заголовка: длинный не съедает пол-листа."""
    n = len(title or "")
    return "xlong" if n > 95 else ("long" if n > 55 else "")


def _toc_label(s: str) -> str:
    """Чистый текст заголовка для оглавления (без markdown/цитат/эмодзи)."""
    s = re.sub(r"\[\d+\]", "", s or "")
    s = re.sub(r"[*`#]+", "", s)
    s = re.sub(r"^[\U0001F000-\U0001FAFF☀-➿️\s]+", "", s)
    return s.strip()


def _toc_norm(s: str) -> str:
    """Нормализация заголовка: без эмодзи, пунктуации и регистра."""
    s = _toc_label(s or "")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip().lower()


def _report_day(v: Any) -> date:
    """День отчёта по Москве: дата сохранения отчёта, а не момент выгрузки
    (раньше на обложке стояло время нажатия кнопки)."""
    if isinstance(v, datetime):
        return (v.astimezone(MSK) if v.tzinfo else v).date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return (d.astimezone(MSK) if d.tzinfo else d).date()
        except ValueError:
            pass
    return today_msk().date()


def _ru_date(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month]} {d.year}"


def pdf_filename(title: str, day: date) -> str:
    """«AuditLens — Эквайринг для малого бизнеса — 29.09.2026.pdf»."""
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title or "")
    t = _clip(re.sub(r"\s+", " ", t).strip().rstrip(".…"), 80).rstrip(".…")
    return f"AuditLens — {t or 'Отчёт'} — {day.strftime('%d.%m.%Y')}.pdf"


def _plain(s: str) -> str:
    """Текст без разметки и ссылок на источники — для обложки."""
    s = re.sub(r"\[\[(?:VIZ|CHART):\d+\]\]", "", s or "")
    s = re.sub(r"\s*\[\d{1,3}\]", "", s)
    s = re.sub(r"\[([^\]]+)\]\((?:https?://|#)[^)]+\)", r"\1", s)
    s = re.sub(r"\*\*|__|`", "", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)\*(?!\w)", r"\1", s)
    s = re.sub(r"\s+([.,;:])", r"\1", s)
    s = re.sub(r"(?<!\.)\.\.(?!\.)", ".", s)          # «п.п.» + точка после ссылок
    return re.sub(r"\s+", " ", s).strip()


def _doc_title(title: str | None, question: str, report_md: str) -> tuple[str, str]:
    """(название, тело без строки-названия). Название: сгенерированное →
    «# » в начале отчёта → вопрос без глагола-команды."""
    from ..ai.report_title import clean_title, heuristic_title, is_default_title
    if title and is_default_title(title, question):
        title = None          # до 29.09 названием было начало вопроса
    mt = re.search(r"^#[ \t]+(.+?)[ \t]*$", report_md, re.MULTILINE)
    first_h2 = re.search(r"^##[ \t]", report_md, re.MULTILINE)
    md_title = ""
    # «# » — титул, только если стоит ДО первого раздела (аудит 26.09).
    if mt and (not first_h2 or mt.start() < first_h2.start()):
        md_title = _plain(mt.group(1))
        report_md = (report_md[:mt.start()] + report_md[mt.end():]).lstrip("\n")
    t = clean_title(title) if title else ""
    return (t or clean_title(md_title) or heuristic_title(question)), report_md


def _lead_points(report_md: str) -> tuple[str, list[str]]:
    """Главный вывод и тезисы обложки — первые фразы абзацев раздела «Резюме»
    (жирная фраза, если есть). Обложка ничего не добавляет от себя: числа и
    формулировки — из текста отчёта."""
    m = re.search(r"(?ms)^##\s+[^\n]*резюме[^\n]*\n(.*?)(?=^##\s|\Z)", report_md, re.I) \
        or re.search(r"(?ms)^##\s+[^\n]*\n(.*?)(?=^##\s|\Z)", report_md)
    if not m:
        return "", []
    sec = re.split(r"(?m)^###\s", m.group(1))[0]
    leads: list[str] = []
    for para in re.split(r"\n\s*\n", sec):
        p = para.strip()
        if (not p or p.startswith(("|", "#", ">", "[[")) or re.match(r"^[-*•]\s", p)
                or re.match(r"^\d+[.)]\s", p)):
            continue
        b = re.match(r"^\*\*(.+?)\*\*", p)
        lead = b.group(1) if b else re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«])", p)[0]
        lead = _plain(lead)
        if len(lead) >= 12:
            # обложка — один лист: вывод и тезисы короче, подробности в резюме
            leads.append(_clip(lead, 240 if not leads else 180))
        if len(leads) >= 4:
            break
    return (leads[0], leads[1:]) if leads else ("", [])


_GAPS_H = re.compile(r"(?mi)^##\s+(?:честные пробелы|пробелы и ограничения|ограничения отч[её]та)\s*$")


def _split_gaps(report_md: str) -> tuple[str, list[str]]:
    """Раздел «Честные пробелы» уходит из тела в приложение «Ограничения
    отчёта»: это журнал прогона, а не вывод."""
    m = _GAPS_H.search(report_md)
    if not m:
        return report_md, []
    nxt = re.search(r"(?m)^##\s", report_md[m.end():])
    end = m.end() + nxt.start() if nxt else len(report_md)
    items = [re.sub(r"^\s*[-*•]\s+", "", ln).strip()
             for ln in report_md[m.end():end].splitlines()]
    return (report_md[:m.start()] + report_md[end:]).rstrip() + "\n", [i for i in items if i]


# ── Разметка отчёта → HTML ───────────────────────────────────────────────────

def _cite_link(n: int, sources_by_n: dict[int, dict], label: str | None = None) -> str:
    s = sources_by_n.get(n)
    text = label or str(n)
    return f'<a href="#src-{n}">{text}</a>' if s else text


def _cite_group(nums: list[int], sources_by_n: dict[int, dict]) -> str:
    """Ссылки подряд — одной сноской, соседние номера — диапазоном «49–56»."""
    uniq = sorted(set(nums))
    parts, i = [], 0
    while i < len(uniq):
        j = i
        while j + 1 < len(uniq) and uniq[j + 1] == uniq[j] + 1:
            j += 1
        a, b = uniq[i], uniq[j]
        if b - a >= 2:
            parts.append(_cite_link(a, sources_by_n, f"{a}–{b}"))
        else:
            parts += [_cite_link(k, sources_by_n) for k in range(a, b + 1)]
        i = j + 1
    return '<sup class="cite">' + ", ".join(parts) + "</sup>"


def _linkify(s: str) -> str:
    """Голые адреса в тексте (журнал прогона) — ссылкой с коротким видом."""
    def one(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;)")
        tail = m.group(0)[len(url):]
        p = urlparse(url)
        shown = _clip((p.netloc.removeprefix("www.") + p.path).rstrip("/"), 60)
        return f'<a href="{url}">{shown}</a>{tail}'
    chunks = re.split(r"(<a\b.*?</a>)", s, flags=re.S)
    return "".join(c if c.startswith("<a") else re.sub(r"https?://[^\s<\"']+", one, c)
                   for c in chunks)


def _inline(s: str, sources_by_n: dict[int, dict]) -> str:
    s = _esc(s)
    s = re.sub(r"&\#x27;|&\#039;", "'", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(^|[^\w_])__([^_]+?)__(?!\w)", r"\1<strong>\2</strong>", s)
    s = re.sub(r"\*([^*]+?)\*", r"<em>\1</em>", s)
    s = re.sub(r"(^|[^\w_])_([^_]+?)_(?!\w)", r"\1<em>\2</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    # Ссылки на источники: группа подряд — одной сноской, одиночная — как есть.
    s = re.sub(r"\[\d{1,3}\](?:\s*\[\d{1,3}\])*",
               lambda m: _cite_group([int(x) for x in re.findall(r"\d{1,3}", m.group(0))],
                                     sources_by_n), s)
    s = re.sub(
        r"⚠\s*((?:КОНФЛИКТ|РАСХОЖДЕНИЕ|ПРОТИВОРЕЧИЕ)"
        r"(?:[/\\](?:КОНФЛИКТ|РАСХОЖДЕНИЕ|ПРОТИВОРЕЧИЕ))*"
        r"(?:\s+В\s+ИСТОЧНИКАХ|\s+ПО\s+ДАННЫМ)?)\s*[:—\-]?\s*([^\n]{0,200})",
        r'<span class="conflict">⚠ \1</span>\2', s)
    s = re.sub(r"(расхождение[^.,;\n]*?)(\d+(?:[.,]\d+)?\s*(?:п\.п\.|пп|%))",
               r'<span class="conflict">\1\2</span>', s, flags=re.IGNORECASE)
    s = re.sub(r"⚠\s*(Не раскрыто|Тематических отзывов не найдено)",
               r'<span class="undisclosed">⚠ \1</span>', s)
    return s


def _md_to_html(md: str, sources_by_n: dict[int, dict],
                toc_out: list | None = None) -> str:
    """Лёгкий markdown → HTML. Разделы (##) нумеруются так же, как в
    оглавлении; ручная нумерация подразделов («### 3. Болит у других»)
    становится «2.3.»; цитаты «> …» — врезками с подписью."""
    if not md:
        return ""
    md = re.sub(r"\[\[CHART:\d+\]\]", "", md)
    out: list[str] = []
    in_table = False
    table_head: list[str] = []
    table_rows: list[list[str]] = []
    list_buf: list[str] = []
    list_ordered = False
    list_start = 1
    quote_buf: list[str] = []
    hnum = 0      # якоря заголовков
    sec_n = 0     # номер раздела (как в оглавлении)

    def il(x: str) -> str:
        return _inline(x, sources_by_n)

    def _flush_list():
        nonlocal list_buf
        if not list_buf:
            return
        tag = "ol" if list_ordered else "ul"
        start = f' start="{list_start}"' if list_ordered and list_start > 1 else ""
        out.append(f"<{tag}{start}>" + "".join(f"<li>{il(x)}</li>" for x in list_buf) + f"</{tag}>")
        list_buf = []

    def _flush_quote():
        nonlocal quote_buf
        if not quote_buf:
            return
        text = " ".join(q for q in quote_buf if q).strip()
        quote_buf = []
        if not text:
            return
        # «…» — Тверь, 15.08.2026 [42]: цитата и подпись раздельно.
        m = re.match(r"^(.*[»\"”])\s*[—–-]\s*(.+)$", text)
        q, src = (m.group(1), m.group(2)) if m else (text, "")
        out.append('<blockquote class="quote"><p>' + il(q) + "</p>"
                   + (f'<p class="q-src">{il(src)}</p>' if src else "") + "</blockquote>")

    def _flush_table():
        nonlocal in_table, table_head, table_rows
        if not in_table:
            return
        cls = ' class="wide"' if len(table_head) > 4 else ""
        out.append(f"<table{cls}><thead><tr>" +
                   "".join(f"<th>{il(h)}</th>" for h in table_head) +
                   "</tr></thead><tbody>" +
                   "".join("<tr>" + "".join(f"<td>{il(c)}</td>" for c in row) + "</tr>"
                           for row in table_rows) +
                   "</tbody></table>")
        in_table = False
        table_head, table_rows = [], []

    for ln in md.split("\n"):
        if ln.lstrip().startswith(">"):
            _flush_list()
            if in_table:
                _flush_table()
            quote_buf.append(re.sub(r"^\s*>\s?", "", ln).strip())
            continue
        _flush_quote()
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.split("|")][1:-1]
            if all(re.fullmatch(r"-+:?|:?-+:?", (c or "").strip()) for c in cells if c.strip()):
                continue
            _flush_list()
            if not in_table:
                in_table = True
                table_head = cells
            else:
                table_rows.append(cells)
            continue
        elif in_table:
            _flush_table()
        m4 = re.match(r"^####\s+(.+)$", ln)
        m3 = re.match(r"^###\s+(.+)$", ln)
        m2 = re.match(r"^##\s+(.+)$", ln)
        m1 = re.match(r"^#\s+(.+)$", ln)
        if m4:
            _flush_list(); out.append(f"<h4>{il(m4.group(1))}</h4>"); continue
        if m3:
            _flush_list()
            t3 = m3.group(1)
            mn = re.match(r"^(\d{1,2})[.)]\s+(.+)$", t3)
            if mn and sec_n:
                t3 = f"{sec_n}.{mn.group(1)}. {mn.group(2)}"
            out.append(f"<h3>{il(t3)}</h3>"); continue
        if m2 or m1:
            _flush_list(); hnum += 1; sec_n += 1
            hid = f"sec-{hnum}"
            text = (m2 or m1).group(1)
            if toc_out is not None:
                toc_out.append({"level": 2, "text": _toc_label(text), "id": hid,
                                "num": f"{sec_n}.", "section_type": "body"})
            out.append(f'<h2 id="{hid}"><span class="hn">{sec_n}.</span> {il(text)}</h2>')
            continue
        ordered_m = re.match(r"^\s*(\d+)\.\s+(.+)$", ln)
        bullet_m = re.match(r"^\s*[\-\*\+•]\s+(.+)$", ln)
        if ordered_m:
            if not list_ordered:
                _flush_list()
            if not list_buf:
                list_start = int(ordered_m.group(1))
            list_ordered = True
            list_buf.append(ordered_m.group(2))
            continue
        if bullet_m:
            if list_ordered:
                _flush_list()
            list_ordered = False
            list_buf.append(bullet_m.group(1))
            continue
        if ln.strip() == "":
            _flush_list()
            continue
        _flush_list()
        out.append(f"<p>{il(ln)}</p>")
    _flush_quote()
    _flush_list()
    _flush_table()
    return "\n".join(out)


def _render_ranking_section(ranking: dict | None) -> str:
    """🏆 Рейтинг — нумерованные карточки субъектов со score/обоснованием.
    Тот же артефакт что RankingWidget в UI, адаптировано под PDF."""
    if not ranking or not isinstance(ranking, dict):
        return ""
    entries = ranking.get("entries") or []
    if not entries:
        return ""
    crit = _esc(ranking.get("criterion", ""))
    entries = sorted(entries, key=lambda e: (e.get("rank") or 99))
    cards = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rank = _esc(e.get("rank", ""))
        label = _esc(e.get("subject_label") or e.get("subject", ""))
        sc = e.get("score")
        score = f"{sc:g}" if isinstance(sc, (int, float)) else _esc(sc or "")
        rationale = _esc(e.get("rationale", ""))
        gap = ('<span class="rank-gap">недостаточно данных</span>'
               if e.get("data_gap") else "")
        cards.append(
            f'<li class="rank-card">'
            f'<div class="rank-num">{rank}</div>'
            f'<div class="rank-body">'
            f'<div class="rank-head"><span class="rank-name">{label}</span>'
            f'<span class="rank-score">{score}<span class="rank-max">/10</span></span>{gap}</div>'
            f'<div class="rank-rationale">{rationale}</div>'
            f'</div></li>')
    return f'''
    <section class="block-page ranking-page" id="sec-ranking">
      <h2>Рейтинг сервисов</h2>
      {f'<div class="lede">{crit}</div>' if crit else ''}
      <ol class="rank-list">{"".join(cards)}</ol>
    </section>'''


def _render_insights_section(insights: list[dict] | None) -> str:
    """💡 Ключевые инсайты — headline + explanation (+ impact)."""
    if not insights:
        return ""
    items = []
    for ins in insights:
        if not isinstance(ins, dict):
            continue
        hl = _esc(ins.get("headline", ""))
        if not hl:
            continue
        expl = _esc(ins.get("explanation", ""))
        impact = _esc(ins.get("impact", ""))
        items.append(
            f'<li class="insight-item">'
            f'<div class="insight-hl">{hl}</div>'
            f'{f"<div class=&#39;insight-expl&#39;>{expl}</div>" if expl else ""}'
            f'{f"<div class=&#39;insight-impact&#39;>Влияние: {impact}</div>" if impact else ""}'
            f'</li>')
    if not items:
        return ""
    return f'''
    <section class="block-page insights-page" id="sec-insights">
      <h2>Ключевые инсайты</h2>
      <ul class="insight-list">{"".join(items)}</ul>
    </section>'''


def _render_gaps_section(gaps: dict | None) -> str:
    """⚠ Пробелы покрытия — что не удалось собрать (честность для аудита)."""
    if not gaps or not isinstance(gaps, dict):
        return ""
    missing = gaps.get("missing") or []
    items = []
    for m in missing:
        if not isinstance(m, dict):
            continue
        what = _esc(m.get("attribute", ""))
        if not what:
            continue
        banks = ", ".join(_esc(b) for b in (m.get("missing_banks") or []))
        items.append(f'<li class="gap-item"><span class="gap-what">{what}</span>'
                     f'{f" — {banks}" if banks else ""}</li>')
    if not items:
        return ""
    return f'''
    <section class="block-page gaps-page" id="sec-gaps">
      <h2>Пробелы покрытия</h2>
      <div class="lede">Данные, которые не удалось собрать в открытых источниках — для честной оценки полноты.</div>
      <ul class="gap-list">{"".join(items)}</ul>
    </section>'''


def _render_claimcheck_section(cc: dict | None) -> str:
    """Компактный trust-баннер: N верифицировано · X отфильтровано."""
    if not cc or not isinstance(cc, dict):
        return ""
    verified = cc.get("verified") or 0
    dropped = cc.get("dropped") or 0
    if not verified and not dropped:
        return ""
    pills = [f'<span class="cc-pill ok">{verified} фактов верифицировано</span>']
    if dropped:
        pills.append(f'<span class="cc-pill warn">{dropped} отфильтровано '
                     f'(защита от галлюцинаций)</span>')
    return f'<section class="cc-section"><div class="cc-box">{"".join(pills)}</div></section>'


def _chart_valid(c) -> bool:
    return isinstance(c, dict) and bool(c.get("labels")) and bool(c.get("datasets"))


def _chart_height_mm(c: dict) -> int:
    """Высота канваса: horizontalBar растёт с числом строк (labels×datasets) —
    иначе многострочные сравнения плющатся в блин."""
    t = c.get("chartType")
    if t == "doughnut":
        return 70
    if t == "horizontalBar":
        rows = max(len(c.get("labels") or []), 1) * max(len(c.get("datasets") or []), 1)
        legend = 10 if len(c.get("datasets") or []) > 1 else 0
        return min(150, max(60, 24 + rows * 13 + legend))
    return 80


def _chart_figure_html(i: int, c: dict) -> str:
    """Карточка графика: canvas + подпись (title · unit) + insight + источники."""
    cid = f"pdfchart_{i}"
    title = _esc(c.get("title", ""))
    unit = _esc(c.get("unit") or "")
    if title and unit:
        title = f"{title} · {unit}"
    insight = _esc(c.get("insight") or "")
    cites = c.get("sourceCitations") or []
    cite_html = ""
    if cites:
        cite_html = ('<div class="chart-cites">Источники: ' +
                      " ".join(f'<span class="cite-mark">[{int(n)}]</span>'
                                for n in cites if isinstance(n, (int, float))) +
                      '</div>')
    return (
        f'<figure class="chart-figure">'
        f'  <div class="chart-canvas-wrap"><canvas id="{cid}"></canvas></div>'
        f'  {f"<figcaption class=\"chart-caption\">{title}</figcaption>" if title else ""}'
        f'  {f"<div class=\"chart-insight\">{insight}</div>" if insight else ""}'
        f'  {cite_html}'
        f'</figure>'
    )


def _render_charts_assets(charts: list[dict], tail_ids: list[int]) -> tuple[str, str]:
    """(tail_html, js). JS рендерит ВСЕ канвасы (inline в теле + хвост).
    Chart.js заинлайнен из /static/vendor (никаких CDN — часть сетей их режет).
    Стиль = веб-ChartCanvas: Сбер/highlight акцентом, ink-градации, монограммы
    банков на категорийной оси, пунктирная референс-линия."""
    calls = []
    for i, c in enumerate(charts):
        if not _chart_valid(c):
            continue
        spec_json = json.dumps({
            "labels": c.get("labels") or [],
            "datasets": c.get("datasets") or [],
            "ctype": c.get("chartType") or "bar",
            "horizontal": c.get("chartType") == "horizontalBar",
            "hl": c.get("highlight") or "",
            "refline": c.get("referenceLine") or None,
        }, ensure_ascii=False)
        calls.append(f'  renderChart("pdfchart_{i}", {spec_json});')
    if not calls:
        return "", ""

    tail_html = ""
    tail_items = [_chart_figure_html(i, charts[i]) for i in tail_ids
                  if _chart_valid(charts[i])]
    if tail_items:
        tail_html = (
            '<section class="charts-page" id="sec-charts">'
            '<h2>Дополнительные визуализации</h2>'
            '<div class="lede">Сравнения, не привязанные к разделам отчёта</div>'
            + "".join(tail_items) + '</section>'
        )

    # Chart.js — локальный (тот же vendored файл, что у веб-фронта)
    try:
        chartjs_src = (Path(__file__).parent / "static" / "vendor"
                       / "chart.umd.js").read_text(encoding="utf-8")
    except Exception:
        chartjs_src = ""
    js = (
        '<script>' + chartjs_src + '</script>'
        '<script>'
        # Оттенки серого сливались в один тёмный круг — доли не различались;
        # об этом прямо написали в обратной связи. Цвета подобраны так, чтобы
        # отличаться и по тону, и по светлоте: тогда они читаются и на
        # чёрно-белой печати.
        'const PAL=["#3b6fb6","#c8412b","#2f7a45","#b8862b","#6b52a3","#4a8f9c","#8a5a3c","#707075"];'
        'const ACC="oklch(58% 0.18 25)",INK="#16181d",INK3="#707075",'
        '      HAIR="#ebebed",PAPER="#ffffff";'
        'function renderChart(cid, spec){'
        '  const el=document.getElementById(cid); if(!el||!window.Chart) return;'
        '  const horiz=spec.horizontal===true;'
        '  const isLine=spec.ctype==="line", isDough=spec.ctype==="doughnut";'
        '  const hl=String(spec.hl||"").toLowerCase().slice(0,5);'
        '  const isHl=lb=>hl&&String(lb||"").toLowerCase().includes(hl);'
        '  const pos=(lb,i)=>isHl(lb)?ACC:PAL[i%PAL.length];'
        '  const labels=spec.labels||[];'
        '  const rows=(labels.length||1)*Math.max((spec.datasets||[]).length,1);'
        '  const Hpx=isDough?340:(horiz?Math.min(900,90+rows*44+((spec.datasets||[]).length>1?30:0)):380);'
        '  el.width=760; el.height=Hpx;'
        '  const single=(spec.datasets||[]).length===1;'
        '  const ds=(spec.datasets||[]).map((d,i)=>{'
        '    const base=isHl(d.label)?ACC:PAL[i%PAL.length];'
        '    return {...d,'
        '      backgroundColor:isDough?labels.map(pos):(isLine?"transparent":(single?labels.map(pos):base)),'
        '      borderColor:isDough?PAPER:base, borderWidth:isLine?2:(isDough?2:0),'
        '      borderRadius:(!isLine&&!isDough)?3:0,'
        '      pointRadius:isLine?3:0, pointBackgroundColor:base, tension:isLine?0.25:0,'
        '      maxBarThickness:22};});'
        '  const fmt=v=>v==null?"":Number(v).toLocaleString("ru-RU",{maximumFractionDigits:1});'
        '  const valLabels={id:"valLabels",afterDatasetsDraw(chart){'
        '    if(isLine||isDough)return; const{ctx}=chart;'
        '    chart.data.datasets.forEach((set,di)=>{'
        '      chart.getDatasetMeta(di).data.forEach((bar,i)=>{'
        '        const v=set.data[i]; if(v==null)return;'
        '        ctx.save(); ctx.font="500 11px JetBrains Mono, monospace"; ctx.fillStyle=INK;'
        '        ctx.textAlign=horiz?"left":"center"; ctx.textBaseline=horiz?"middle":"bottom";'
        '        if(horiz)ctx.fillText(fmt(v),bar.x+5,bar.y); else ctx.fillText(fmt(v),bar.x,bar.y-5);'
        '        ctx.restore();});});'
        '  }};'
        '  const inits=lb=>{const p=String(lb||"").split(/[\\s\\-]+/).filter(Boolean);'
        '    return ((p.length>1?p[0][0]+p[1][0]:String(lb||"").slice(0,2))||"·").toUpperCase();};'
        '  const monoAxis={id:"monoAxis",afterDraw(chart){'
        '    if(!horiz||isDough)return; const s=chart.scales.y; if(!s)return; const c2=chart.ctx;'
        '    labels.forEach((lb,i)=>{const y=s.getPixelForTick(i); const cx=s.left+12;'
        '      c2.save(); c2.beginPath(); c2.arc(cx,y,9,0,Math.PI*2);'
        '      c2.fillStyle=pos(lb,i); c2.fill();'
        '      c2.font="600 8px JetBrains Mono, monospace"; c2.fillStyle=PAPER;'
        '      c2.textAlign="center"; c2.textBaseline="middle"; c2.fillText(inits(lb),cx,y+0.5);'
        '      c2.font="500 10.5px Geist, sans-serif"; c2.fillStyle=isHl(lb)?ACC:INK3;'
        '      c2.textAlign="left";'
        '      let nm=String(lb||""); if(nm.length>13)nm=nm.slice(0,12)+"…";'
        '      c2.fillText(nm,cx+14,y+0.5); c2.restore();});'
        '  }};'
        '  const refLine={id:"refLine",afterDatasetsDraw(chart){'
        '    const rl=spec.refline; if(!rl||isDough||rl.value==null)return;'
        '    const area=chart.chartArea; const sc=horiz?chart.scales.x:chart.scales.y;'
        '    if(!sc)return; const px=sc.getPixelForValue(+rl.value); const c2=chart.ctx;'
        '    c2.save(); c2.strokeStyle=INK3; c2.setLineDash([4,4]); c2.lineWidth=1; c2.beginPath();'
        '    if(horiz){c2.moveTo(px,area.top);c2.lineTo(px,area.bottom);}'
        '    else{c2.moveTo(area.left,px);c2.lineTo(area.right,px);}'
        '    c2.stroke(); c2.setLineDash([]);'
        '    c2.font="500 9.5px JetBrains Mono, monospace"; c2.fillStyle=INK3;'
        '    const t=((rl.label||"")+" "+fmt(+rl.value)).trim();'
        '    if(horiz)c2.fillText(t,Math.min(px+5,area.right-60),area.top+10);'
        '    else c2.fillText(t,area.left+5,Math.max(px-5,area.top+10));'
        '    c2.restore();'
        '  }};'
        '  const _ch=new Chart(el.getContext("2d"),{'
        '    type: horiz?"bar":(isDough?"doughnut":isLine?"line":"bar"),'
        '    data:{labels:labels, datasets:ds},'
        '    plugins:[valLabels,monoAxis,refLine],'
        '    options:{indexAxis:horiz?"y":"x", responsive:false,'
        '      devicePixelRatio:3,'
        '      animation:false, layout:{padding:{top:isDough?4:18,right:horiz?44:8}},'
        '      plugins:{legend:{display:ds.length>1||isDough,'
        '          position:isDough?"right":"bottom",'
        '          labels:{font:{size:11,family:"Geist,sans-serif"},color:"#44464d",'
        '                  boxWidth:10,boxHeight:10,padding:14,usePointStyle:true,pointStyle:"rect"}},'
        '        tooltip:{enabled:false}},'
        '      scales:isDough?{}:{x:{ticks:{font:{size:10.5,family:"Geist,sans-serif"},'
        '          color:horiz?INK3:(c)=>isHl(labels[c.index])?ACC:INK3},'
        '        grid:{display:!horiz,color:HAIR,lineWidth:1,drawTicks:false},'
        '        border:{display:false}},'
        '      y:{beginAtZero:true,'
        '        afterFit:horiz?(sc)=>{sc.width=Math.max(sc.width,118);}:undefined,'
        '        ticks:horiz?{display:false}:{font:{size:10.5,family:"Geist,sans-serif"},color:INK3},'
        '        grid:{display:horiz,color:HAIR,lineWidth:1,drawTicks:false},'
        '        border:{display:false}}}}'
        '  });'
        '  try{const img=document.createElement("img");'
        '  img.src=el.toDataURL("image/png");'
        '  img.style.width="100%";img.style.height="auto";img.style.display="block";'
        '  el.parentNode.replaceChild(img,el);_ch.destroy();}catch(e){}'
        '}'
        '\nfunction _runCharts(){\n'
        '  if(typeof window.Chart === "undefined"){\n'
        '    setTimeout(_runCharts, 50); return;\n'
        '  }\n'
        + "\n".join(calls) + '\n'
        '  window.__chartsRendered = true;\n'
        '}\n'
        'if(document.readyState === "complete" || document.readyState === "interactive"){\n'
        '  _runCharts();\n'
        '} else {\n'
        '  document.addEventListener("DOMContentLoaded", _runCharts);\n'
        '}\n'
        '</script>'
    )
    return tail_html, js




# ── Источники: группами, в две колонки ───────────────────────────────────────
# 29.09 список источников занимал 7–15 страниц из 18–36: карточка на источник
# с полным адресом и цитатой, пять на лист. Теперь запись — одна-две строки:
# номер, название-ссылка, сайт, дата прочтения; цитата-доказательство одной
# строкой — только у сайтов банков и регуляторов (решение владельца 29.09).

_SRC_GROUPS = (
    ("official", "Сайты банков и регуляторов"),
    ("auditlens", "Данные AuditLens"),
    ("reviews", "Агрегаторы и отзывы клиентов"),
    ("other", "СМИ и другие источники"),
)


def _src_group(s: dict) -> str:
    kind = str(s.get("source_kind") or "").lower()
    url = str(s.get("url") or "")
    if kind == "auditlens" or url.startswith("#"):
        return "auditlens"
    if kind in ("bank_official", "regulator", "regulatory"):
        return "official"
    if kind in ("aggregator", "review", "reviews"):
        return "reviews"
    return "other"


def _src_domain(s: dict) -> str:
    d = s.get("domain") or urlparse(str(s.get("url") or "")).netloc
    return str(d or "").removeprefix("www.")


def _src_date(s: dict) -> str:
    v = str(s.get("fetched_at") or "")[:10]
    try:
        return date.fromisoformat(v).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _src_quote(s: dict) -> str:
    ex = s.get("excerpts") or [f.get("verbatim") for f in (s.get("facts") or [])
                               if isinstance(f, dict)] or ([s["excerpt"]] if s.get("excerpt") else [])
    best = max((str(e) for e in ex if e), key=len, default="") if isinstance(ex, list) else ""
    return _clip(_plain(best), 170)


def _render_sources_section(sources: list[dict]) -> tuple[str, dict[str, int]]:
    """(html, число источников по группам)."""
    groups: dict[str, list[dict]] = {k: [] for k, _ in _SRC_GROUPS}
    for s in sorted((s for s in sources or [] if isinstance(s, dict)),
                    key=lambda s: (s.get("n") is None, s.get("n") or 0)):
        groups[_src_group(s)].append(s)
    counts = {k: len(v) for k, v in groups.items()}
    if not sum(counts.values()):
        return "", counts
    blocks = []
    for key, label in _SRC_GROUPS:
        rows = []
        for s in groups[key]:
            n = s.get("n", "")
            url = str(s.get("url") or "")
            title = re.sub(r"\s+", " ", str(s.get("title") or "")).strip()
            if key == "auditlens":
                title = re.sub(r"^AuditLens\s*·\s*", "", title)
            title = _clip(title or _src_domain(s) or "Источник", 120)
            t_html = (f'<a href="{_esc(url)}">{_esc(title)}</a>' if url.startswith("http")
                      else _esc(title))
            meta = ["срез вкладки AuditLens" if key == "auditlens" else _src_domain(s), _src_date(s)]
            quote = _src_quote(s) if key == "official" else ""
            rows.append(
                f'<li class="sr" id="src-{_esc(n)}"><span class="sn">{_esc(n)}</span>'
                f'<span class="sb"><span class="st">{t_html}</span> '
                f'<span class="sm">{_esc(" · ".join(x for x in meta if x))}</span>'
                + (f'<span class="sq">«{_esc(quote)}»</span>' if quote else "")
                + "</span></li>")
        if rows:
            blocks.append(f'<div class="sg"><h3>{label}<span>{len(rows)}</span></h3>'
                          f'<ol class="scol">{"".join(rows)}</ol></div>')
    return "".join(blocks), counts


def _coverage_line(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if not total:
        return ""
    parts = []
    if counts.get("official"):
        n = counts["official"]
        parts.append(f"{n} {_pl(n, 'сайт банка или регулятора', 'сайта банков и регуляторов', 'сайтов банков и регуляторов')}")
    if counts.get("auditlens"):
        n = counts["auditlens"]
        parts.append(f"{n} {_pl(n, 'срез', 'среза', 'срезов')} AuditLens")
    if counts.get("reviews"):
        n = counts["reviews"]
        parts.append(f"{n} {_pl(n, 'страница', 'страницы', 'страниц')} агрегаторов и отзывов")
    if counts.get("other"):
        n = counts["other"]
        parts.append(f"{n} {_pl(n, 'другой', 'других', 'других')}")
    return f"{total} {_pl(total, 'источник', 'источника', 'источников')}: " + ", ".join(parts)


# ── Ограничения отчёта ───────────────────────────────────────────────────────

def _sentence_with(md: str, number: str) -> str:
    """Фраза отчёта, в которой стоит число: у пункта «сверить число» должен
    быть контекст (29.09 в PDF был пункт «число 300 000» — и больше ничего)."""
    digits = re.sub(r"\D", "", number)
    if len(digits) < 2:
        return ""
    pat = r"(?<![\d,.])" + r"[\s  ]?".join(re.escape(ch) for ch in digits) + r"(?![\d])"
    for line in (md or "").splitlines():
        if line.startswith(("|", "#", "[[")):
            continue
        m = re.search(pat, line)
        if not m:
            continue
        for sent in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«*])", line):
            if re.search(pat, sent):
                return _clip(re.sub(r"^[>\-*•\s]+", "", sent).strip(), 280)
    return ""


def _render_limits_section(items_md: list[str], verification: dict | None,
                           gaps: dict | None, report_md: str,
                           sources_by_n: dict[int, dict]) -> str:
    """Одна врезка вместо «Честных пробелов», «Пробелов покрытия» и листа
    «Требуют ручной проверки»: что учесть и какие числа сверить — с фразой из
    отчёта и ссылкой на источник."""
    v = verification or {}
    notes = [_linkify(_inline(x, sources_by_n)) for x in items_md or []]
    for g in (v.get("unanswered") or [])[:6]:
        notes.append("Нет ответа в источниках: " + _esc(g))
    if v.get("critic_failed"):
        notes.append("Смысловая проверка на этом прогоне не выполнялась: числа сверены "
                     "автоматически, цитаты и полнота ответа — нет.")
    seen = {_toc_norm(_plain(x)) for x in items_md or []}
    for m in (gaps or {}).get("missing") or []:
        if not isinstance(m, dict) or not m.get("attribute"):
            continue
        # «Пробелы» прогона повторяют строки раздела «Честные пробелы» — дубль не печатаем.
        if _toc_norm(_plain(str(m["attribute"]))) in seen:
            continue
        banks = ", ".join(_esc(b) for b in (m.get("missing_banks") or []))
        notes.append("Не найдено в источниках: " + _inline(str(m["attribute"]), sources_by_n)
                     + (f" — {banks}" if banks else ""))
    checks = []
    for u in v.get("unverified") or []:
        claim = u.get("claim") if isinstance(u, dict) else f"число {u}"
        num = re.search(r"\d[\d\s .,]*\d|\d", str(claim or ""))
        sent = _sentence_with(report_md, num.group(0)) if num else ""
        shown = (num.group(0).strip() if num else str(claim or "")).strip()
        body = (f"<b>{_esc(shown)}</b> — «{_inline(sent, sources_by_n)}»" if sent
                else f"<b>{_esc(claim)}</b>" + (f" — {_esc(u.get('issue'))}" if isinstance(u, dict)
                                                 and u.get("issue") else ""))
        checks.append(f"<li>{body}</li>")
    if not notes and not checks:
        return ""
    parts = ['<section class="limits" id="sec-limits"><h2>Ограничения отчёта</h2>',
             '<div class="limits-box">']
    if notes:
        parts.append('<ul class="limits-list">' + "".join(f"<li>{x}</li>" for x in notes) + "</ul>")
    if checks:
        parts.append('<div class="limits-sub">Сверить вручную: числа, которых нет в '
                     'источнике рядом с цитатой</div><ol class="limits-checks">'
                     + "".join(checks) + "</ol>")
    parts.append("</div></section>")
    return "".join(parts)


# ── Обложка и оглавление ─────────────────────────────────────────────────────

def _render_toc(entries: list[dict], pages: dict[str, int] | None) -> str:
    """Оглавление на обложке. Номера страниц — со второго прохода рендера
    (по закладкам первого); в первом проходе место под номер уже занято."""
    seen: set[str] = set()
    body, appendix = [], []
    for e in entries:
        key = _toc_norm(e.get("text", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        (appendix if e.get("section_type") == "appendix" else body).append(e)
    if not body and not appendix:
        return ""

    def row(e: dict, cls: str) -> str:
        num = e.get("num") or ""
        key = _toc_norm(f"{num} {e['text']}")
        page = (pages or {}).get(key)
        page_html = (f'<span class="toc-page">{page}</span>' if page
                     else '<span class="toc-page pending">00</span>')
        return (f'<li class="{cls}"><a href="#{e["id"]}">'
                f'<span class="toc-num">{_esc(num)}</span>'
                f'<span class="toc-text">{_esc(_toc_label(e["text"]))}</span>'
                f'<span class="toc-dots"></span>{page_html}</a></li>')

    items = "".join(row(e, "toc-row") for e in body)
    if appendix:
        items += "".join(row(e, "toc-row toc-app") for e in appendix)
    return f'<nav class="toc"><div class="toc-head">Содержание</div><ol class="toc-list">{items}</ol></nav>'


def _heading_pages(pdf: bytes) -> dict[str, int]:
    """Страница каждого заголовка — по закладкам PDF (их строит Chromium из
    h1–h6): {нормализованный текст: номер страницы}."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf))
    out: dict[str, int] = {}

    def walk(items):
        for it in items:
            if isinstance(it, list):
                walk(it)
                continue
            try:
                page = reader.get_destination_page_number(it) + 1
            except Exception:  # noqa: BLE001
                continue
            out.setdefault(_toc_norm(str(getattr(it, "title", "") or "")), page)
    walk(reader.outline)
    return out


# ── Шрифты ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _font_faces() -> str:
    """@font-face из саморазмещённых шрифтов сайта (vendor/fonts.css): только
    нужные семейства, кириллица и латиница. Раньше шрифты грузились с Google
    Fonts при каждой выгрузке: на проде другой набор начертаний и 38 страниц
    вместо 36, в закрытом контуре — системные шрифты."""
    try:
        css = (_VENDOR / "fonts.css").read_text(encoding="utf-8")
    except OSError:
        return ""
    out = []
    for m in re.finditer(r"/\*\s*([\w-]+)\s*\*/\s*(@font-face\s*\{[^}]*\})", css):
        subset, block = m.group(1), m.group(2)
        fam = re.search(r"font-family:\s*'([^']+)'", block)
        if fam and fam.group(1) in _PDF_FONTS and subset in _PDF_SUBSETS:
            out.append(block.replace("/static/vendor/fonts/", _FONT_HOST))
    return "\n".join(out)


def _font_file(url: str) -> Path | None:
    name = url[len(_FONT_HOST):].split("?")[0]
    if not re.fullmatch(r"[\w.-]+\.woff2", name):
        return None
    p = _VENDOR / "fonts" / name
    return p if p.is_file() else None


# ── Документ ─────────────────────────────────────────────────────────────────

def build_pdf_html(*, question: str, report_md: str,
                   sources: list[dict] | None = None,
                   meta: dict | None = None,
                   verification: dict | None = None,
                   charts: list[dict] | None = None,
                   viz: list[dict] | None = None,
                   ranking: dict | None = None,
                   insights: list[dict] | None = None,
                   gaps: dict | None = None,
                   claim_check: dict | None = None,
                   title: str | None = None,
                   report_id: int | None = None,
                   report_date: Any = None,
                   author: str | None = None,
                   toc_pages: dict[str, int] | None = None) -> str:
    """HTML документа для печати Chromium'ом."""
    sources = [s for s in (sources or []) if isinstance(s, dict)]
    sources_by_n = {s["n"]: s for s in sources if s.get("n") is not None}
    meta = meta or {}
    doc_title, report_md = _doc_title(title, question, report_md or "")
    day = _report_day(report_date)
    answer, theses = _lead_points(report_md)
    report_md, gap_items = _split_gaps(report_md)

    toc_entries: list[dict] = []
    body_md = re.sub(r"\[\[CHART:(\d+)\]\]", r"CHARTSLOT7f3a\1end", report_md)
    body_md = body_md.replace("VIZSLOT7f3a", "VIZSLOT 7f3a")
    body_md = re.sub(r"(?m)^\s*\[\[VIZ:(\d{1,2})\]\]\s*$", r"VIZSLOT7f3a\1end", body_md)
    body_md = re.sub(r"\[\[VIZ:\d+\]\]", "", body_md)
    body_html = _md_to_html(body_md, sources_by_n, toc_out=toc_entries)

    charts = charts or []
    figs = {i: _chart_figure_html(i, c) for i, c in enumerate(charts) if _chart_valid(c)}
    placed: set[int] = set()

    def _chsub(mm):
        i = int(mm.group(1))
        if i in figs and i not in placed:
            placed.add(i)
            return figs[i]
        return ""
    body_html = re.sub(r"(?:<p>\s*)?CHARTSLOT7f3a(\d+)end(?:\s*</p>)?", _chsub, body_html)
    viz_by_n = {int(v["n"]): (v.get("html") or "") for v in (viz or [])
                if isinstance(v, dict) and v.get("n") is not None}

    def _vzsub(mm):
        h = viz_by_n.get(int(mm.group(1)), "")
        return f'<div class="viz-block">{h}</div>' if h.strip() else ""
    body_html = re.sub(r"(?:<p>\s*)?VIZSLOT7f3a(\d+)end(?:\s*</p>)?", _vzsub, body_html)
    charts_html, charts_js = _render_charts_assets(charts, [i for i in figs if i not in placed])

    body_keys = {_toc_norm(e["text"]) for e in toc_entries}
    ranking_html = _render_ranking_section(ranking) if ranking and not any("рейтинг" in k for k in body_keys) else ""
    insights_html = _render_insights_section(insights) if insights and not any("инсайт" in k for k in body_keys) else ""
    limits_html = _render_limits_section(gap_items, verification, gaps, report_md, sources_by_n)
    claimcheck_html = _render_claimcheck_section(claim_check)
    sources_html, counts = _render_sources_section(sources)
    for cond, label, sid in ((ranking_html, "Рейтинг сервисов", "sec-ranking"),
                             (insights_html, "Ключевые инсайты", "sec-insights"),
                             (charts_html, "Визуализация ключевых метрик", "sec-charts"),
                             (limits_html, "Ограничения отчёта", "sec-limits"),
                             (sources_html, "Источники", "sec-sources")):
        if cond:
            toc_entries.append({"level": 2, "text": label, "id": sid, "section_type": "appendix"})
    toc_html = _render_toc(toc_entries, toc_pages)

    q_plain = _plain(question)
    show_q = q_plain and q_plain.lower().rstrip("?.! ") != doc_title.lower().rstrip("?.!… ")
    meta_bits = [_ru_date(day)]
    if report_id:
        meta_bits.append(f"отчёт № {int(report_id)}")
    if author:
        meta_bits.append(_esc(author))
    if meta.get("verified"):
        meta_bits.append(f"{_esc(meta['verified'])} фактов сверено с источниками")
    answer_html = ""
    if answer:
        answer_html = ('<div class="cover-answer"><div class="ca-label">Главный вывод</div>'
                       f'<p class="ca-main">{_esc(answer)}</p>'
                       + ('<ul class="ca-theses">' + "".join(f"<li>{_esc(t)}</li>" for t in theses)
                          + "</ul>" if theses else "") + "</div>")
    cov = _coverage_line(counts)
    cover_html = (
        '<section class="cover">'
        '<div class="brand"><span class="brand-mark">AuditLens</span>'
        '<span class="brand-kind">Аналитический отчёт</span></div>'
        f'<h1 class="cover-title {_title_size(doc_title)}">{_esc(doc_title)}</h1>'
        + (f'<p class="cover-q"><span>Вопрос аудитора</span>{_esc(_clip(q_plain, 300))}</p>' if show_q else "")
        + f'<p class="cover-meta">{" · ".join(meta_bits)}</p>'
        + answer_html
        + (f'<p class="cover-cov">{_esc(cov)}</p>' if cov else "")
        + toc_html + "</section>")

    footer_title = _clip(doc_title, 70).replace("\\", "\\\\").replace('"', '\\"')
    css = (_CSS.replace("%FOOTER%", f"AuditLens · {footer_title} · {day.strftime('%d.%m.%Y')}")
           .replace("%FONTS%", _font_faces()))
    sources_block = (f'<section class="sources-page" id="sec-sources"><h2>Источники</h2>'
                     f'<p class="src-lede">Номер — тот же, что у ссылки в тексте; название '
                     f'открывает источник, дата — когда он прочитан.</p>{sources_html}</section>'
                     if sources_html else "")
    return (f'<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">'
            f'<title>{_esc(doc_title)} · AuditLens</title><style>{css}</style></head><body>'
            f'{cover_html}<section class="body">{body_html}</section>'
            f'{ranking_html}{insights_html}{charts_html}{claimcheck_html}{limits_html}'
            f'{sources_block}{charts_js}</body></html>')


_CSS = r"""
%FONTS%
@page {
  size: A4;
  margin: 20mm 18mm 20mm 18mm;
  @bottom-left { content: "%FOOTER%"; font-family: 'Geist', system-ui, sans-serif; font-size: 7.5pt; color: #8a8f99; }
  @bottom-right { content: counter(page) " / " counter(pages); font-family: 'Geist', system-ui, sans-serif; font-size: 7.5pt; color: #8a8f99; }
}
@page :first { @bottom-left { content: none; } @bottom-right { content: none; } }
* { box-sizing: border-box; }
:root {
  --paper: #fbfaf7; --paper-2: #f4f2ec; --surface: #ffffff;
  --ink: #16181d; --ink-2: #4a4f5a; --ink-3: #6b717d; --ink-4: #b3b8c2;
  --hair: #e6e3da; --hair-2: #d9d5ca;
  --accent: #c8412b; --accent-soft: rgba(200,65,43,.08);
  --pos: #2f7a3d; --warn: #b8862b; --neg: #c8412b;
  --sber: oklch(46% 0.13 152); --sber-soft: oklch(46% 0.13 152 / 0.10);
}
html, body { margin: 0; padding: 0; }
body {
  font-family: 'Source Serif 4', Georgia, serif; font-size: 11pt; line-height: 1.55; color: var(--ink);
  -webkit-print-color-adjust: exact; print-color-adjust: exact; text-rendering: optimizeLegibility;
  orphans: 3; widows: 3;
}
a { color: inherit; }

/* Обложка-резюме: название, вопрос, главный вывод, охват, оглавление */
.cover { break-after: page; padding-top: 4mm; }
.brand { display: flex; justify-content: space-between; align-items: baseline; font-family: 'Geist', system-ui, sans-serif;
  border-bottom: 1.5px solid var(--ink); padding-bottom: 3mm; margin-bottom: 12mm; }
.brand-mark { font-weight: 700; font-size: 11pt; letter-spacing: -0.01em; }
.brand-kind { font-size: 8pt; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.08em; }
.cover-title { font-size: 25pt; font-weight: 600; line-height: 1.15; letter-spacing: -0.015em; margin: 0 0 5mm; text-wrap: balance; }
.cover-title.long { font-size: 22pt; }
.cover-title.xlong { font-size: 19pt; }
.cover-q { font-family: 'Geist', system-ui, sans-serif; font-size: 9.5pt; line-height: 1.45; color: var(--ink-2); margin: 0 0 3mm; }
.cover-q span, .ca-label, .toc-head { display: block; font-family: 'Geist', system-ui, sans-serif; font-size: 7.5pt; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-3); margin-bottom: 1mm; }
.cover-meta { font-family: 'Geist', system-ui, sans-serif; font-size: 9pt; color: var(--ink-3); margin: 0 0 8mm; }
.cover-answer { border-left: 3px solid var(--ink); padding: 0.5mm 0 0.5mm 5mm; margin: 0 0 6mm; }
.ca-main { font-size: 12.5pt; font-weight: 600; line-height: 1.4; margin: 0 0 3mm; }
.ca-theses { margin: 0; padding-left: 4.5mm; font-size: 9.5pt; line-height: 1.42; color: var(--ink-2); }
.ca-theses li { margin-bottom: 1.6mm; }
.cover-cov { font-family: 'Geist', system-ui, sans-serif; font-size: 8.5pt; color: var(--ink-3); margin: 0 0 7mm; }
.toc { border-top: 1px solid var(--hair-2); padding-top: 4mm; }
.toc-list { list-style: none; padding: 0; margin: 1.5mm 0 0; font-family: 'Geist', system-ui, sans-serif; font-size: 9.5pt; }
.toc-row a { display: flex; align-items: baseline; gap: 2mm; padding: 0.9mm 0; color: var(--ink); text-decoration: none; }
.toc-num { flex: 0 0 7mm; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.toc-text { flex: 0 1 auto; }
.toc-dots { flex: 1 1 auto; border-bottom: 1px dotted var(--ink-4); transform: translateY(-1mm); min-width: 8mm; }
.toc-page { flex: 0 0 8mm; text-align: right; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.toc-page.pending { visibility: hidden; }
.toc-app a { color: var(--ink-2); }
.toc-app:first-of-type, .toc-row + .toc-app { }
.toc-row:not(.toc-app) + .toc-app { margin-top: 2.5mm; padding-top: 2mm; border-top: 1px solid var(--hair); }

/* Тело */
.body h1, .body h2 { font-size: 16pt; font-weight: 600; letter-spacing: -0.01em; line-height: 1.25;
  margin: 9mm 0 3mm; padding-top: 4mm; border-top: 1px solid var(--hair-2); break-after: avoid; }
.body > h2:first-child { margin-top: 0; border-top: 0; padding-top: 0; }
.body h2 .hn { color: var(--ink-3); font-weight: 500; margin-right: 1mm; }
.body h3 { font-family: 'Geist', system-ui, sans-serif; font-size: 11pt; font-weight: 600; margin: 6mm 0 2mm; break-after: avoid; }
.body h4 { font-family: 'Geist', system-ui, sans-serif; font-size: 10pt; font-weight: 600; margin: 4mm 0 1.5mm; color: var(--ink-2); break-after: avoid; }
.body p { margin: 2mm 0 3mm; }
.body ul, .body ol { margin: 2mm 0 4mm; padding-left: 6mm; }
.body li { margin-bottom: 1.5mm; }
.body strong { font-weight: 600; }
.body code { font-family: 'JetBrains Mono', monospace; font-size: 0.9em; background: var(--paper-2); padding: 0 3px; border-radius: 3px; }
.body blockquote.quote { margin: 3mm 0 4mm; padding: 1mm 0 1mm 5mm; border-left: 2px solid var(--hair-2); break-inside: avoid; }
.body blockquote.quote p { margin: 0; font-style: italic; font-size: 10.5pt; line-height: 1.5; }
.body blockquote.quote .q-src { margin-top: 1mm; font-family: 'Geist', system-ui, sans-serif; font-style: normal; font-size: 8.5pt; color: var(--ink-3); }
.body sup.cite, .limits sup.cite { font-family: 'Geist', system-ui, sans-serif; font-size: 6.8pt; color: var(--ink-3);
  vertical-align: super; line-height: 0; margin-left: 1px; font-variant-numeric: tabular-nums; }
.body sup.cite a, .limits sup.cite a { color: inherit; text-decoration: none; }
.body table { width: 100%; border-collapse: collapse; font-family: 'Geist', system-ui, sans-serif; font-size: 9pt; margin: 4mm 0 6mm; }
.body thead { display: table-header-group; }
.body tr { break-inside: avoid; }
.body thead th { text-align: left; font-weight: 600; font-size: 8pt; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink-2);
  padding: 2mm 2.5mm; border-bottom: 1.5px solid var(--ink); border-top: 1px solid var(--hair-2); }
.body tbody td { padding: 2mm 2.5mm; border-bottom: 1px solid var(--hair); vertical-align: top; }
.body tbody tr:nth-child(even) td { background: #fafaf8; }
.body table.wide { font-size: 7.5pt; table-layout: fixed; }
.body table.wide thead th { font-size: 7pt; padding: 1.5mm; }
.body table.wide tbody td { padding: 1.5mm; overflow-wrap: anywhere; }
.body .conflict { background: #fff5e6; color: #8a4400; padding: 0.5px 5px 1px; border-radius: 3px; border: 1px solid #f0d29a;
  font-weight: 500; font-size: 0.95em; line-height: 1.3; }
.body .undisclosed { color: var(--ink-3); font-style: italic; font-size: 0.95em; }

/* Визуализации: свой банк — зелёным, как во всей системе; длинные таблицы
   переносятся с повтором шапки, заголовок не отрывается от блока. */
.viz-block { margin: 4mm 0 6mm; font-family: 'Geist', system-ui, sans-serif; font-size: 9pt; line-height: 1.4; }
.viz-block .viz { width: 100%; --accent: var(--sber); --accent-soft: var(--sber-soft); }
.viz-block h4 { font-size: 10pt; font-weight: 600; margin: 0 0 2mm; break-after: avoid; }
/* подписи-надстрочники внутри блока («Первые шаги проверки») — вместе со списком */
.viz-block [style*="uppercase"] { break-after: avoid; }
.viz-block table { width: 100%; border-collapse: collapse; }
.viz-block thead { display: table-header-group; }
.viz-block tr, .viz-block li { break-inside: avoid; }
.viz-block td, .viz-block th { overflow-wrap: anywhere; }
.viz-block svg { max-width: 100%; height: auto; max-height: 600px; }
.viz-block .viz-cite { font-size: 6.5pt; vertical-align: super; line-height: 0; color: var(--ink-3); margin-left: 1px; }
.viz-block small { color: var(--ink-3); }

/* Ограничения отчёта */
.limits { margin-top: 10mm; }
.limits h2, .sources-page h2, .block-page h2, .charts-page h2 { font-size: 16pt; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 3mm; }
.limits-box { background: var(--paper-2); border: 1px solid var(--hair); border-radius: 6px; padding: 4.5mm 6mm;
  font-family: 'Geist', system-ui, sans-serif; font-size: 8.8pt; line-height: 1.45; color: var(--ink-2); }
.limits-list, .limits-checks { margin: 0; padding-left: 4.5mm; }
.limits-list li, .limits-checks li { margin-bottom: 1.4mm; break-inside: avoid; }
.limits-list a, .limits-checks a { color: var(--ink-2); }
.limits-sub { margin: 3mm 0 1.5mm; font-weight: 600; color: var(--ink); }
.limits-checks b { color: var(--ink); font-weight: 600; }

/* Источники: группы, две колонки, запись в одну-две строки */
.sources-page { break-before: page; }
.src-lede { font-family: 'Geist', system-ui, sans-serif; font-size: 8.5pt; color: var(--ink-3); margin: 0 0 3mm; }
.sg h3 { font-family: 'Geist', system-ui, sans-serif; font-size: 8pt; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--ink-2); margin: 5mm 0 1.5mm; padding-bottom: 1mm; border-bottom: 1px solid var(--ink); break-after: avoid; }
.sg h3 span { margin-left: 2mm; color: var(--ink-3); font-weight: 500; }
.scol { columns: 2; column-gap: 7mm; list-style: none; padding: 0; margin: 0; }
.sr { display: flex; gap: 2mm; break-inside: avoid; padding: 1mm 0; border-bottom: 1px solid var(--hair);
  font-family: 'Geist', system-ui, sans-serif; font-size: 7.8pt; line-height: 1.35; }
.sn { flex: 0 0 6mm; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.sb { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
.st a { color: var(--ink); text-decoration: none; }
.sm { color: var(--ink-3); white-space: nowrap; }
/* ── Богатые секции (рейтинг / инсайты / gaps / claim-check) ── */
.block-page { page-break-inside: avoid; margin-top: 9mm; }
.rank-list, .insight-list { list-style: none; padding: 0; margin: 0; }
.rank-card { display: flex; gap: 10px; padding: 9px 0; border-bottom: 1px solid #ededed; page-break-inside: avoid; }
.rank-num { flex: 0 0 auto; width: 21px; height: 21px; border-radius: 50%; background: #b3261e; color: #fff; font-family: 'Geist', sans-serif; font-weight: 700; font-size: 10.5pt; line-height: 21px; text-align: center; }
.rank-body { flex: 1; }
.rank-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; }
.rank-name { font-family: 'Geist', sans-serif; font-weight: 600; font-size: 11.5pt; color: #16181d; }
.rank-score { font-family: 'JetBrains Mono', monospace; font-weight: 600; color: #b3261e; font-size: 11pt; }
.rank-max { color: #b0b0b4; font-size: 8.5pt; }
.rank-gap { font-family: 'Geist', sans-serif; font-size: 8pt; color: #9a6a00; background: #fdf3e0; padding: 1px 7px; border-radius: 8px; }
.rank-rationale { font-size: 10pt; color: #3a3d44; line-height: 1.5; }
.insight-item { padding: 7px 0 7px 12px; border-left: 3px solid #1f4e79; margin-bottom: 9px; page-break-inside: avoid; }
.insight-hl { font-family: 'Geist', sans-serif; font-weight: 600; font-size: 11pt; color: #16181d; margin-bottom: 2px; }
.insight-expl { font-size: 10pt; color: #3a3d44; line-height: 1.5; }
.insight-impact { font-size: 9pt; color: #6b7280; margin-top: 3px; font-style: italic; }
.gap-list { margin: 0; padding-left: 18px; }
.gap-item { font-size: 10pt; color: #3a3d44; margin-bottom: 4px; }
.gap-what { font-weight: 600; color: #16181d; }
.cc-section { margin: 7mm 0 0; }
.cc-box { display: flex; gap: 10px; flex-wrap: wrap; }
.cc-pill { font-family: 'Geist', sans-serif; font-size: 9pt; padding: 4px 11px; border-radius: 12px; }
.cc-pill.ok { background: #e7f4ec; color: #1a7f4b; }
.cc-pill.warn { background: #fdf3e0; color: #9a6a00; }

/* Charts page — визуализация ключевых метрик. Каждый график на отдельной
   секции, с тонкой рамкой как для таблиц, без shadow/gradients. */
.charts-page { margin-top: 10mm; }
.charts-page h2 {
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 18pt;
  font-weight: 500;
  border: none;
  padding: 0;
  margin: 0 0 4mm;
}
.charts-page .lede {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9pt;
  color: #707075;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-bottom: 12mm;
  border-bottom: 1px solid #d6d6d8;
  padding-bottom: 4mm;
}
.chart-figure {
  margin: 0 0 14mm;
  page-break-inside: avoid;
}
.chart-insight {
  font-style: italic;
  color: #44464d;
  font-size: 10.5pt;
  margin: 2mm 0 0;
  line-height: 1.5;
}
.chart-canvas-wrap {
  width: 100%;
  height: auto;
  position: relative;
  border: 1px solid #ebebed;
  background: #ffffff;
  padding: 4mm 4mm 2mm;
  border-radius: 4px;
}
.chart-canvas-wrap canvas {
  width: 100% !important;
  height: 100% !important;
}
.chart-caption {
  margin-top: 3mm;
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 11pt;
  font-weight: 500;
  color: #16181d;
  letter-spacing: -0.005em;
}
.chart-cites {
  margin-top: 1.5mm;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8.5pt;
  color: #707075;
}
.chart-cites .cite-mark {
  color: #c43838;
  font-weight: 500;
  margin-right: 3px;
}


.sq { display: block; margin-top: 0.4mm; font-family: 'Source Serif 4', Georgia, serif; font-style: italic; font-size: 7.8pt; color: var(--ink-2); }
"""


# ── Печать ───────────────────────────────────────────────────────────────────

# Блок шире полосы набора уменьшается сам — не весь документ. 29.09 таблица
# визуализации шириной 3 727 px заставила Chromium ужать все 18 страниц до
# двух третей (текст 7 pt вместо 11).
_WIDTH_GUARD_JS = """() => {
  const W = document.documentElement.clientWidth; let fixed = 0;
  const sel = '.viz-block, .body > table, .body pre, .chart-figure, .limits-box, .block-page, .cover';
  for (const el of document.querySelectorAll(sel)) {
    const w = el.scrollWidth;
    if (w > W + 2) { el.style.zoom = String(Math.max(0.45, W / w)); fixed++; }
  }
  return {fixed, total: document.documentElement.scrollWidth, width: W};
}"""


def _route(route):
    """Сеть рендеру не нужна: в документе разметка модели. Шрифты — из
    /static/vendor по служебному адресу, всё остальное обрывается."""
    url = route.request.url
    if url.startswith(_FONT_HOST):
        p = _font_file(url)
        if p:
            route.fulfill(status=200, body=p.read_bytes(), content_type="font/woff2",
                          headers={"Access-Control-Allow-Origin": "*"})
        else:
            route.abort()
        return
    if url.startswith(("data:", "about:")):
        route.continue_()
    else:
        route.abort()


def _print(browser, html_str: str) -> bytes:
    ctx = browser.new_context(device_scale_factor=2,
                              viewport={"width": _PRINT_WIDTH, "height": 1100})
    try:
        page = ctx.new_page()
        page.route("**/*", _route)
        page.emulate_media(media="print")
        page.set_content(html_str, wait_until="networkidle", timeout=30000)
        # Разметка модели не должна раздуть документ до тысяч страниц.
        if page.evaluate("document.documentElement.scrollHeight") > 200_000:
            raise ValueError("документ аномально высок")
        try:
            page.evaluate("document.fonts.ready.then(() => true)")
        except Exception:  # noqa: BLE001
            pass
        try:
            if page.evaluate("document.querySelector('[id^=pdfchart_]') !== null"):
                page.wait_for_function("window.__chartsRendered === true", timeout=12000)
                page.wait_for_timeout(250)
        except Exception as e:  # noqa: BLE001
            log.warning("PDF: графики не дорисовались (%s)", e)
        try:
            g = page.evaluate(_WIDTH_GUARD_JS)
            if g.get("fixed"):
                log.info("PDF: уменьшено широких блоков: %s (ширина %s из %s)",
                         g["fixed"], g.get("total"), g.get("width"))
        except Exception:  # noqa: BLE001
            pass
        page.set_default_timeout(60000)
        return page.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                        margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
                        outline=True, tagged=True)
    finally:
        ctx.close()


def _browser(p):
    return p.chromium.launch(headless=True, args=["--no-sandbox",
                                                  "--disable-blink-features=AutomationControlled"])


def render_pdf(html_str: str) -> bytes:
    """HTML → PDF одним проходом (без номеров страниц в оглавлении)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            return _print(browser, html_str)
        finally:
            browser.close()


def export_report_to_pdf(*, question: str, report_md: str,
                         sources: list[dict] | None = None,
                         meta: dict | None = None,
                         verification: dict | None = None,
                         charts: list[dict] | None = None,
                         viz: list[dict] | None = None,
                         ranking: dict | None = None,
                         insights: list[dict] | None = None,
                         gaps: dict | None = None,
                         claim_check: dict | None = None,
                         title: str | None = None,
                         report_id: int | None = None,
                         report_date: Any = None,
                         author: str | None = None) -> bytes:
    """PDF отчёта в два прохода: первый — узнать по закладкам страницы
    разделов, второй — с номерами в оглавлении. Не вышло узнать — первый."""
    from playwright.sync_api import sync_playwright
    kw = dict(question=question, report_md=report_md, sources=sources, meta=meta,
              verification=verification, charts=charts, viz=viz, ranking=ranking,
              insights=insights, gaps=gaps, claim_check=claim_check, title=title,
              report_id=report_id, report_date=report_date, author=author)
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            first = _print(browser, build_pdf_html(**kw))
            try:
                pages = _heading_pages(first)
            except Exception as e:  # noqa: BLE001
                log.warning("PDF: страницы разделов не прочитаны (%s)", e)
                pages = {}
            if not pages:
                return first
            return _print(browser, build_pdf_html(**kw, toc_pages=pages))
        finally:
            browser.close()
