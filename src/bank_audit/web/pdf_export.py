"""Premium PDF export для аудит-отчётов.

Архитектура:
  • Frontend POSTit отчёт + sources + meta в /api/ai/export-pdf
  • Сервер строит премиум HTML-template (Source Serif 4, Geist, тонкие линии,
    monogram-footer, A4 с грамотными margin'ами)
  • Playwright Chromium конвертит HTML → PDF
  • Возвращаем application/pdf с Content-Disposition attachment

Эстетика: editorial newspaper. Никаких неонов, никаких эмодзи в тексте,
JetBrains Mono для метаданных, Source Serif 4 для тела.
"""
from __future__ import annotations
import html as _html
import io
import json
import logging
import re
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


def _esc(s: Any) -> str:
    return _html.escape(str(s or ""))


def _toc_label(s: str) -> str:
    """Чистый текст заголовка для оглавления (без markdown/цитат)."""
    s = re.sub(r"\[\d+\]", "", s or "")
    s = re.sub(r"[*`#]+", "", s)
    return s.strip()


def _md_to_html(md: str, sources_by_n: dict[int, dict],
                 toc_out: list | None = None) -> str:
    """Лёгкий markdown → HTML с привязкой [N] к источникам.
    toc_out (если задан) наполняется заголовками {level,text,id} для оглавления;
    h1/h2 получают id-якоря для кликабельных переходов из TOC.
    Поддерживает: # заголовки, **bold**, таблицы, списки, blockquote.
    Не использует общий renderMD из jsx — здесь нужен полностью server-side.
    """
    if not md:
        return ""
    # Inline-маркеры графиков [[CHART:N]] — в UI заменяются на сам график; графики
    # в PDF выводятся отдельной секцией, поэтому здесь просто убираем сырой маркер
    # (иначе он остаётся уродливым текстом «[[CHART:2]]» в теле).
    md = re.sub(r"\[\[CHART:\d+\]\]", "", md)
    lines = md.split("\n")
    out: list[str] = []
    in_table = False
    table_head: list[str] = []
    table_rows: list[list[str]] = []
    list_buf: list[str] = []
    list_ordered = False
    hnum = 0  # счётчик заголовков для якорей оглавления

    def _inline(s: str) -> str:
        s = _esc(s)
        s = re.sub(r"&\#039;|'", "'", s)  # restore some chars
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        # __жирный__ / _курсив_ (подчёркивания) — на границах слова
        # (Python \w в py3 Unicode-aware, кириллица не ломается).
        s = re.sub(r"(^|[^\w_])__([^_]+?)__(?!\w)", r"\1<strong>\2</strong>", s)
        s = re.sub(r"\*([^*]+?)\*", r"<em>\1</em>", s)
        s = re.sub(r"(^|[^\w_])_([^_]+?)_(?!\w)", r"\1<em>\2</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # markdown-ссылки [текст](url) → <a> (ДО citation [N])
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                    r'<a href="\2">\1</a>', s)
        # Citation [N] → пометить inline-ссылкой на anchor #src-N
        def _cite(m: re.Match) -> str:
            n = int(m.group(1))
            s = sources_by_n.get(n)
            if s and s.get("url"):
                return (f'<sup class="cite"><a href="#src-{n}">{n}</a></sup>')
            return f"<sup class=\"cite\">{n}</sup>"
        s = re.sub(r"\[(\d{1,3})\]", _cite, s)
        # Conflict-badge — расширенные паттерны:
        #   ⚠ РАСХОЖДЕНИЕ:                — стандартный
        #   ⚠ РАСХОЖДЕНИЕ/ПРОТИВОРЕЧИЕ:   — слеш-формат от LLM
        #   ⚠ КОНФЛИКТ В ИСТОЧНИКАХ:      — длинный
        #   расхождение N п.п.            — inline в тексте
        s = re.sub(
            r"⚠\s*((?:КОНФЛИКТ|РАСХОЖДЕНИЕ|ПРОТИВОРЕЧИЕ)"
            r"(?:[/\\](?:КОНФЛИКТ|РАСХОЖДЕНИЕ|ПРОТИВОРЕЧИЕ))*"
            r"(?:\s+В\s+ИСТОЧНИКАХ|\s+ПО\s+ДАННЫМ)?)\s*[:—\-]?\s*([^\n]{0,200})",
            r'<span class="conflict">⚠ \1</span>\2', s
        )
        s = re.sub(
            r"(расхождение[^.,;\n]*?)(\d+(?:[.,]\d+)?\s*(?:п\.п\.|пп|%))",
            r'<span class="conflict">\1\2</span>', s, flags=re.IGNORECASE
        )
        # «⚠ Не раскрыто» — отдельный muted-marker (не warn, просто visual hint)
        s = re.sub(r"⚠\s*(Не раскрыто|Тематических отзывов не найдено)",
                    r'<span class="undisclosed">⚠ \1</span>', s)
        # Тонкий пробел между sup'ами
        s = s.replace("</sup><sup", "</sup> <sup")
        return s

    def _flush_list():
        nonlocal list_buf
        if not list_buf:
            return
        tag = "ol" if list_ordered else "ul"
        out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>"
                                         for x in list_buf) + f"</{tag}>")
        list_buf = []

    def _flush_table():
        nonlocal in_table, table_head, table_rows
        if not in_table:
            return
        # Широкие таблицы (много банков-колонок) сжимаем, чтобы не обрезались за
        # край A4 (item 61): класс .wide уменьшает шрифт и переносит слова.
        cls = ' class="wide"' if len(table_head) > 4 else ""
        out.append(f"<table{cls}><thead><tr>" +
                   "".join(f"<th>{_inline(h)}</th>" for h in table_head) +
                   "</tr></thead><tbody>" +
                   "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                            for row in table_rows) +
                   "</tbody></table>")
        in_table = False
        table_head, table_rows = [], []

    for ln in lines:
        # Таблицы
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.split("|")][1:-1]
            # separator-row из дефисов — пропускаем
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
        # Заголовки
        m4 = re.match(r"^####\s+(.+)$", ln)
        m3 = re.match(r"^###\s+(.+)$", ln)
        m2 = re.match(r"^##\s+(.+)$", ln)
        m1 = re.match(r"^#\s+(.+)$", ln)
        if m4: _flush_list(); out.append(f"<h4>{_inline(m4.group(1))}</h4>"); continue
        if m3: _flush_list(); out.append(f"<h3>{_inline(m3.group(1))}</h3>"); continue
        if m2:
            _flush_list(); hnum += 1; hid = f"sec-{hnum}"
            if toc_out is not None:
                toc_out.append({"level": 2, "text": _toc_label(m2.group(1)), "id": hid})
            out.append(f'<h2 id="{hid}">{_inline(m2.group(1))}</h2>'); continue
        if m1:
            _flush_list(); hnum += 1; hid = f"sec-{hnum}"
            if toc_out is not None:
                toc_out.append({"level": 1, "text": _toc_label(m1.group(1)), "id": hid})
            out.append(f'<h1 id="{hid}">{_inline(m1.group(1))}</h1>'); continue
        # Списки
        ordered_m = re.match(r"^\s*(\d+)\.\s+(.+)$", ln)
        bullet_m  = re.match(r"^\s*[\-\*\+•]\s+(.+)$", ln)
        if ordered_m:
            if not list_ordered: _flush_list()
            list_ordered = True
            list_buf.append(ordered_m.group(2))
            continue
        if bullet_m:
            if list_ordered: _flush_list()
            list_ordered = False
            list_buf.append(bullet_m.group(1))
            continue
        if ln.strip() == "":
            _flush_list()
            continue
        # Обычный параграф
        _flush_list()
        out.append(f"<p>{_inline(ln)}</p>")
    _flush_list()
    _flush_table()
    return "\n".join(out)


def _render_sources_section(sources: list[dict]) -> str:
    """Premium источники: nested list, trust-marks, дата, тип."""
    if not sources:
        return ""
    KIND_LABELS = {
        "regulator": "Регулятор", "bank_official": "Офиц. сайт банка",
        "press": "Пресса", "analyst": "Аналитика",
        "aggregator": "Агрегатор", "social": "Соцсети", "blog": "Блог",
    }
    rows = []
    for s in sources:
        n     = s.get("n", "?")
        url   = s.get("url", "")
        bank  = s.get("bank_name") or "—"
        kind  = KIND_LABELS.get(s.get("source_kind"), s.get("source_kind") or "—")
        trust = float(s.get("trust_score") or 0)
        # «Премиум» trust marks: ●●● / ●●○ / ●○○
        if   trust >= 0.85: marks = "●●●"
        elif trust >= 0.55: marks = "●●○"
        else:               marks = "●○○"
        head = s.get("headings_path") or ""
        date = s.get("fetched_at") or ""
        if date and "T" in str(date): date = str(date).split("T")[0]
        # Дословная выдержка-доказательство (item 62): чтобы статичный PDF нёс ту
        # же цитату, на которую опирался синтез, а не только URL.
        excerpts = s.get("excerpts") or []
        best = ""
        if isinstance(excerpts, list) and excerpts:
            best = max((str(e) for e in excerpts if e), key=len, default="")
        excerpt_html = (f'<div class="src-excerpt">«{_esc(best[:400])}»</div>'
                        if best else "")
        title = s.get("title") or ""
        title_html = f'<div class="src-title">{_esc(title)}</div>' if title else ""
        rows.append(
            f'<li id="src-{n}" class="src-row">'
            f'<div class="src-num">[{n}]</div>'
            f'<div class="src-meta">'
              f'<div class="src-bank">{_esc(bank)}</div>'
              f'{title_html}'
              f'<div class="src-url"><a href="{_esc(url)}">{_esc(url)}</a></div>'
              f'{f"<div class=\"src-head\">{_esc(head)}</div>" if head else ""}'
              f'{excerpt_html}'
              f'<div class="src-foot">'
                f'<span class="src-kind">{_esc(kind)}</span>'
                f'<span class="src-trust">{marks}</span>'
                f'{f"<span class=\"src-date\">{_esc(date)}</span>" if date else ""}'
              f'</div>'
            f'</div>'
            f'</li>'
        )
    return f'<ol class="src-list">{"".join(rows)}</ol>'


def _render_verification_section(unverified: list[dict]) -> str:
    """Премиум-блок «Утверждения для ручной проверки» — то же что
    VerificationBanner в UI (.dr-verify-warn), но адаптировано под PDF.
    Warn-tinted rounded box со всем содержимым, не голым списком."""
    if not unverified:
        return ""
    items = []
    for i, u in enumerate(unverified, 1):
        claim = _esc(u.get("claim", ""))
        issue = _esc(u.get("issue", ""))
        items.append(
            f'<li class="ver-item">'
            f'  <div class="ver-num">{i:02d}</div>'
            f'  <div class="ver-body">'
            f'    <div class="ver-claim">«{claim}»</div>'
            f'    <div class="ver-issue">{issue}</div>'
            f'  </div>'
            f'</li>'
        )
    return f'''
    <section class="verification-page" id="sec-verify">
      <h2>Требуют ручной проверки</h2>
      <div class="lede">
        Автоматический верификатор не нашёл подтверждения этим утверждениям
        в текстах источников. Это не значит что они неверны — возможно,
        число выражено в источнике другой формулировкой. Рекомендуется
        проверить вручную, открыв URL соответствующего источника.
      </div>
      <div class="ver-box">
        <div class="ver-box-head">
          ⚠ {len(unverified)} {("утверждение" if len(unverified)==1 else "утверждения" if len(unverified)<5 else "утверждений")} требуют ручной проверки
        </div>
        <ol class="ver-list">{"".join(items)}</ol>
      </div>
    </section>'''


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
      <h2>🏆 Рейтинг</h2>
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
      <h2>💡 Ключевые инсайты</h2>
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
      <h2>⚠ Пробелы покрытия</h2>
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
    pills = [f'<span class="cc-pill ok">✓ {verified} фактов верифицировано</span>']
    if dropped:
        pills.append(f'<span class="cc-pill warn">{dropped} отфильтровано '
                     f'(защита от галлюцинаций)</span>')
    return f'<section class="cc-section"><div class="cc-box">{"".join(pills)}</div></section>'


def _render_charts_section(charts: list[dict]) -> tuple[str, str]:
    """Возвращает (html_block, js_block) для отрисовки графиков в PDF.
    Каждый chart = canvas + Chart.js script. Playwright ждёт networkidle
    после загрузки CDN и рендера всех графиков, прежде чем снимать PDF."""
    if not charts:
        return "", ""
    items = []
    js_chunks = []
    for i, c in enumerate(charts):
        if not isinstance(c, dict): continue
        if not c.get("labels") or not c.get("datasets"): continue
        cid = f"pdfchart_{i}"
        title = _esc(c.get("title", ""))
        cites = c.get("sourceCitations") or []
        cite_html = ""
        if cites:
            cite_html = ('<div class="chart-cites">Источники: ' +
                          " ".join(f'<span class="cite-mark">[{int(n)}]</span>'
                                    for n in cites if isinstance(n, (int, float))) +
                          '</div>')
        items.append(
            f'<figure class="chart-figure">'
            f'  <div class="chart-canvas-wrap"><canvas id="{cid}"></canvas></div>'
            f'  {f"<figcaption class=\"chart-caption\">{title}</figcaption>" if title else ""}'
            f'  {cite_html}'
            f'</figure>'
        )
        # Chart.js spec — JSON.dump через json.dumps (escape кавычек, unicode)
        spec_json = json.dumps({
            "type": ("bar" if c.get("chartType") in ("bar","horizontalBar")
                      else c.get("chartType") or "bar"),
            "data": {
                "labels":   c.get("labels") or [],
                "datasets": c.get("datasets") or [],
            },
            "horizontal": c.get("chartType") == "horizontalBar",
            "ctype": c.get("chartType") or "bar",
        }, ensure_ascii=False)
        js_chunks.append(
            f'  renderChart("{cid}", {spec_json});'
        )
    if not items:
        return "", ""
    section = (
        '<section class="charts-page" id="sec-charts">'
        '<h2>Визуализация ключевых метрик</h2>'
        '<div class="lede">Ключевые числовые сравнения из отчёта</div>'
        + "".join(items) +
        '</section>'
    )
    # Промисом ждём пока Chart.js загрузится с CDN (на случай если
    # set_content вернётся раньше чем onload), потом вызываем renderChart
    # для каждого spec'а. Window-флаг __chartsRendered позволяет Playwright'у
    # дождаться завершения через wait_for_function.
    calls = "\n".join(js_chunks)
    js = (
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"></script>'
        '<script>'
        'const PALETTE=["#16181d","#44464d","#707075","#9c9ea3","#c4c6cc"];'
        'function renderChart(cid, spec){'
        '  const el=document.getElementById(cid); if(!el||!window.Chart) return;'
        '  const ds=(spec.data.datasets||[]).map((d,i)=>({...d,'
        '    backgroundColor: spec.ctype==="doughnut"?PALETTE:PALETTE[i%5],'
        '    borderColor: PALETTE[i%5], borderWidth: spec.ctype==="line"?2:0,'
        '    pointRadius: spec.ctype==="line"?3:0, tension:0.25}));'
        '  const horiz=spec.horizontal===true;'
        '  const isLine=spec.ctype==="line", isDough=spec.ctype==="doughnut";'
        '  const fmt=v=>v==null?"":Number(v).toLocaleString("ru-RU",{maximumFractionDigits:1});'
        '  const dataLabelsPlugin={id:"valLabels",afterDatasetsDraw(chart){'
        '    if(isLine||isDough)return; const{ctx}=chart;'
        '    chart.data.datasets.forEach((set,di)=>{'
        '      chart.getDatasetMeta(di).data.forEach((bar,i)=>{'
        '        const v=set.data[i]; if(v==null)return;'
        '        ctx.save(); ctx.font="500 11px JetBrains Mono, monospace";'
        '        ctx.fillStyle="#16181d";'
        '        ctx.textAlign=horiz?"left":"center";'
        '        ctx.textBaseline=horiz?"middle":"bottom";'
        '        if(horiz)ctx.fillText(fmt(v),bar.x+5,bar.y);'
        '        else ctx.fillText(fmt(v),bar.x,bar.y-5);'
        '        ctx.restore();'
        '      });'
        '    });'
        '  }};'
        '  new Chart(el.getContext("2d"),{'
        '    type: horiz?"bar":(isDough?"doughnut":isLine?"line":"bar"),'
        '    data:{labels:spec.data.labels||[], datasets:ds},'
        '    plugins:[dataLabelsPlugin],'
        '    options:{indexAxis:horiz?"y":"x", responsive:true, maintainAspectRatio:false,'
        '      animation:false, layout:{padding:{top:isDough?4:18,right:horiz?44:8}},'
        '      plugins:{legend:{display:ds.length>1||isDough,'
        '          position:isDough?"right":"bottom",'
        '          labels:{font:{size:11,family:"Geist,sans-serif"},color:"#44464d",'
        '                  boxWidth:10,boxHeight:10,padding:14,usePointStyle:true,pointStyle:"rect"}},'
        '        tooltip:{enabled:false}},'
        '      scales:isDough?{}:{x:{ticks:{font:{size:10.5,family:"Geist,sans-serif"},color:"#707075"},'
        '        grid:{display:!horiz,color:"#ebebed",lineWidth:1,drawTicks:false},'
        '        border:{display:false}},'
        '      y:{beginAtZero:true,ticks:{font:{size:10.5,family:"Geist,sans-serif"},color:"#707075"},'
        '        grid:{display:horiz,color:"#ebebed",lineWidth:1,drawTicks:false},'
        '        border:{display:false}}}}'
        '  });'
        '}'
        # Ждём готовности Chart.js (тики setTimeout по 50ms), потом
        # вызываем все renderChart и ставим флаг чтобы Playwright поймал
        '\nfunction _runCharts(){\n'
        '  if(typeof window.Chart === "undefined"){\n'
        '    setTimeout(_runCharts, 50); return;\n'
        '  }\n'
        f'  {calls}\n'
        '  window.__chartsRendered = true;\n'
        '}\n'
        'if(document.readyState === "complete" || document.readyState === "interactive"){\n'
        '  _runCharts();\n'
        '} else {\n'
        '  document.addEventListener("DOMContentLoaded", _runCharts);\n'
        '}\n'
        '</script>'
    )
    return section, js


def _render_toc(entries: list[dict]) -> str:
    """Авто-оглавление по заголовкам отчёта + крупным секциям. Ссылки
    кликабельны в PDF (Chromium сохраняет внутренние якоря)."""
    if not entries:
        return ""
    items = []
    for e in entries:
        cls = "toc-l1" if e.get("level", 1) == 1 else "toc-l2"
        items.append(f'<li class="{cls}"><a href="#{e["id"]}">{_esc(e["text"])}</a></li>')
    return f'''
    <section class="toc-page">
      <h2>Содержание</h2>
      <ul class="toc-list">{"".join(items)}</ul>
    </section>'''


def build_pdf_html(*, question: str, report_md: str,
                   sources: list[dict] | None = None,
                   meta: dict | None = None,
                   verification: dict | None = None,
                   charts: list[dict] | None = None,
                   ranking: dict | None = None,
                   insights: list[dict] | None = None,
                   gaps: dict | None = None,
                   claim_check: dict | None = None) -> str:
    """Собирает HTML-документ для последующего рендера в PDF Chromium'ом."""
    sources = sources or []
    sources_by_n = {s["n"]: s for s in sources if s.get("n") is not None}
    meta = meta or {}
    toc_entries: list[dict] = []
    report_md = report_md or ""
    # Заголовок документа берём из первого «# » отчёта (аналитик генерит нормальный
    # деловой заголовок). Сырой вопрос пользователя как титул НЕ используем. Строку
    # вырезаем из тела, чтобы h1 не задваивался (обложка + тело).
    doc_title = "Аудит-отчёт"
    _mt = re.search(r'^#[ \t]+(.+?)[ \t]*$', report_md, re.MULTILINE)
    if _mt:
        doc_title = _mt.group(1).strip()
        report_md = (report_md[:_mt.start()] + report_md[_mt.end():]).lstrip("\n")
    body_html = _md_to_html(report_md, sources_by_n, toc_out=toc_entries)
    sources_html = _render_sources_section(sources)
    unverified = (verification or {}).get("unverified") or []
    verification_html = _render_verification_section(unverified)
    charts_html, charts_js = _render_charts_section(charts or [])
    # Богатые виджеты UI, которых раньше не было в PDF (рейтинг/инсайты/gaps/claim-check)
    ranking_html = _render_ranking_section(ranking)
    insights_html = _render_insights_section(insights)
    gaps_html = _render_gaps_section(gaps)
    claimcheck_html = _render_claimcheck_section(claim_check)
    # Оглавление: заголовки тела + крупные секции (в порядке документа).
    for cond, label, sid in (
        (ranking_html, "🏆 Рейтинг", "sec-ranking"),
        (insights_html, "💡 Ключевые инсайты", "sec-insights"),
        (charts_html, "Визуализация ключевых метрик", "sec-charts"),
        (verification_html, "Требуют ручной проверки", "sec-verify"),
        (gaps_html, "⚠ Пробелы покрытия", "sec-gaps"),
        (sources_html, "Источники", "sec-sources"),
    ):
        if cond:
            toc_entries.append({"level": 1, "text": label, "id": sid})
    toc_html = _render_toc(toc_entries)
    now_iso = datetime.now().strftime("%Y-%m-%d · %H:%M")
    n_cites = len(set(re.findall(r"\[(\d{1,3})\]", report_md or "")))
    audit_id = meta.get("audit_id") or now_iso.replace(" ", "")[:14]

    # CSS: premium editorial. Source Serif 4 для тела, Geist для UI-блоков,
    # JetBrains Mono для метаданных. Никаких градиентов / неонов.
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Аудит-отчёт · AuditLens</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600;8..60,700&family=Geist:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
@page {{
  size: A4;
  margin: 22mm 18mm 22mm 18mm;
  @bottom-left  {{ content: "AuditLens · {audit_id}"; font-family: 'JetBrains Mono', monospace; font-size: 8pt; color: #888; }}
  @bottom-right {{ content: "стр. " counter(page) " из " counter(pages); font-family: 'JetBrains Mono', monospace; font-size: 8pt; color: #888; }}
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: #16181d;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
  text-rendering: optimizeLegibility;
}}
.cover {{
  page-break-after: always;
  padding-top: 32mm;
}}
.cover .mark {{
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 9pt;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #16181d;
  border-top: 1px solid #16181d;
  border-bottom: 1px solid #16181d;
  padding: 6mm 0;
  margin-bottom: 18mm;
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.cover .mark .id {{ font-family: 'JetBrains Mono', monospace; font-size: 8pt; font-weight: 500; color: #707075; letter-spacing: 0.06em; }}
.cover .eyebrow {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 9pt;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #707075;
  margin-bottom: 6mm;
}}
.cover h1 {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 30pt;
  font-weight: 500;
  line-height: 1.2;
  letter-spacing: -0.01em;
  margin: 0 0 12mm;
  max-width: 130mm;
}}
.cover .meta {{
  margin-top: 26mm;
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 9.5pt;
  color: #44464d;
  display: grid;
  grid-template-columns: 28mm 1fr;
  row-gap: 4mm;
}}
.cover .meta dt {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 8pt;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #909094;
}}
.cover .meta dd {{ margin: 0; color: #16181d; font-weight: 500; }}
/* Body */
.body {{ }}
.body h1 {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 18pt; font-weight: 500;
  margin: 14mm 0 4mm;
  letter-spacing: -0.005em;
}}
.body h2 {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 14pt; font-weight: 500;
  margin: 10mm 0 2.5mm;
  padding-top: 4mm;
  border-top: 1px solid #d6d6d8;
  letter-spacing: -0.005em;
  page-break-after: avoid;
}}
.body h3 {{
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 11pt; font-weight: 600;
  margin: 7mm 0 2mm;
  letter-spacing: -0.005em;
  color: #16181d;
  page-break-after: avoid;
}}
.body h4 {{
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 10pt; font-weight: 600;
  margin: 5mm 0 1.5mm;
  color: #44464d;
}}
.body p {{ margin: 2mm 0 3mm; }}
.body ul, .body ol {{ margin: 2mm 0 4mm 0; padding-left: 6mm; }}
.body li {{ margin-bottom: 1.5mm; }}
.body strong {{ font-weight: 600; }}
.body em {{ font-style: italic; }}
.body code {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.92em;
  background: #f3f3f4;
  padding: 0 3px;
  border-radius: 3px;
}}
/* Tables — newspaper-grade */
.body table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 9.5pt;
  margin: 4mm 0 6mm;
  page-break-inside: avoid;
  font-family: 'Geist', system-ui, sans-serif;
}}
.body thead th {{
  text-align: left;
  font-weight: 600;
  font-size: 8.5pt;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: #707075;
  padding: 2.5mm 3mm;
  border-bottom: 1.5px solid #16181d;
  border-top: 1px solid #d6d6d8;
}}
.body tbody td {{
  padding: 2.5mm 3mm;
  border-bottom: 1px solid #ebebed;
  vertical-align: top;
}}
.body tbody tr:last-child td {{ border-bottom: 1.5px solid #16181d; }}
/* Широкие сравнительные таблицы (5+ колонок): сжать, переносить, не обрезать */
.body table.wide {{ font-size: 7.5pt; table-layout: fixed; word-break: break-word; }}
.body table.wide thead th {{ font-size: 7pt; padding: 1.5mm 1.5mm; }}
.body table.wide tbody td {{ padding: 1.5mm 1.5mm; word-break: break-word; overflow-wrap: anywhere; }}
/* Выдержка-доказательство под источником в PDF */
.body .src-excerpt {{ font-size: 8pt; color: #54555a; font-style: italic; margin: 1mm 0; line-height: 1.4; }}
.body .src-title {{ font-size: 8.5pt; color: #2a2b30; margin-bottom: 0.5mm; }}
/* Citations */
.body sup.cite {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 7pt;
  vertical-align: super;
  margin-left: 1.5px;
  font-feature-settings: 'tnum';
}}
.body sup.cite a {{
  color: #c43838;
  text-decoration: none;
  font-weight: 500;
  padding: 0 1px;
}}
/* Conflict — единственный warn-цвет в документе. Pill-форма,
   тонкая рамка, JetBrains Mono uppercase для первого слова. */
.body .conflict {{
  display: inline;
  background: #fff5e6;
  color: #8a4400;
  padding: 0.5px 6px 1px;
  border-radius: 3px;
  border: 1px solid #f0d29a;
  font-weight: 500;
  font-size: 0.95em;
  line-height: 1.3;
}}
.body .undisclosed {{
  color: #909094;
  font-style: italic;
  font-size: 0.95em;
}}
/* Charts page — визуализация ключевых метрик. Каждый график на отдельной
   секции, с тонкой рамкой как для таблиц, без shadow/gradients. */
.charts-page {{
  page-break-before: always;
}}
.charts-page h2 {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 18pt;
  font-weight: 500;
  border: none;
  padding: 0;
  margin: 0 0 4mm;
}}
.charts-page .lede {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 9pt;
  color: #707075;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-bottom: 12mm;
  border-bottom: 1px solid #d6d6d8;
  padding-bottom: 4mm;
}}
.chart-figure {{
  margin: 0 0 14mm;
  page-break-inside: avoid;
}}
.chart-canvas-wrap {{
  width: 100%;
  height: 80mm;
  position: relative;
  border: 1px solid #ebebed;
  background: #ffffff;
  padding: 4mm 4mm 2mm;
  border-radius: 4px;
}}
.chart-canvas-wrap canvas {{
  width: 100% !important;
  height: 100% !important;
}}
.chart-caption {{
  margin-top: 3mm;
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 11pt;
  font-weight: 500;
  color: #16181d;
  letter-spacing: -0.005em;
}}
.chart-cites {{
  margin-top: 1.5mm;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8.5pt;
  color: #707075;
}}
.chart-cites .cite-mark {{
  color: #c43838;
  font-weight: 500;
  margin-right: 3px;
}}

/* Verification page — «Требуют ручной проверки» */
.verification-page {{
  page-break-before: always;
}}
.verification-page h2 {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 18pt;
  font-weight: 500;
  border: none;
  padding: 0;
  margin: 0 0 6mm;
}}
.verification-page .lede {{
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 10pt;
  color: #44464d;
  line-height: 1.55;
  margin-bottom: 10mm;
  padding: 0 0 5mm;
  border-bottom: 1px solid #d6d6d8;
  max-width: 145mm;
}}
/* Rounded warn-box — копия .dr-verify-warn из UI:
   тёплый бежевый фон (warn 6% непрозрачности на бумаге),
   рамка чуть темнее, мягкий радиус 5px. */
.ver-box {{
  border: 1px solid #efd9a8;
  background: #fdf6e7;
  border-radius: 5px;
  padding: 8mm 10mm 6mm;
  margin-top: 2mm;
}}
.ver-box-head {{
  font-family: 'Geist', system-ui, sans-serif;
  font-size: 11pt;
  font-weight: 600;
  color: #7c5a14;
  margin-bottom: 6mm;
  letter-spacing: -0.005em;
}}
.ver-list {{
  list-style: none;
  padding: 0;
  margin: 0;
}}
.ver-item {{
  display: grid;
  grid-template-columns: 12mm 1fr;
  align-items: baseline;
  padding: 4mm 0;
  border-bottom: 1px solid #efd9a8;
  page-break-inside: avoid;
}}
.ver-item:last-child {{ border-bottom: none; padding-bottom: 0; }}
.ver-num {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 9pt;
  font-weight: 600;
  color: #8a4400;
}}
.ver-claim {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 10.5pt;
  color: #16181d;
  font-style: italic;
  margin-bottom: 2mm;
  line-height: 1.5;
}}
.ver-issue {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 8.5pt;
  color: #707075;
  letter-spacing: 0.02em;
}}

/* Sources section */
.sources-page {{
  page-break-before: always;
}}
.sources-page h2 {{
  font-family: 'Source Serif 4', Georgia, serif;
  font-size: 18pt;
  font-weight: 500;
  border: none;
  padding: 0;
  margin: 0 0 6mm;
}}
.sources-page .lede {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 9pt;
  color: #707075;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-bottom: 12mm;
  border-bottom: 1px solid #d6d6d8;
  padding-bottom: 4mm;
}}
.src-list {{
  list-style: none;
  padding: 0;
  margin: 0;
}}
.src-row {{
  display: grid;
  grid-template-columns: 14mm 1fr;
  align-items: baseline;
  padding: 4mm 0;
  border-bottom: 1px solid #ebebed;
  page-break-inside: avoid;
  font-family: 'Geist', system-ui, sans-serif;
}}
.src-num {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 9.5pt;
  font-weight: 600;
  color: #16181d;
}}
.src-meta {{ }}
.src-bank {{
  font-size: 10.5pt;
  font-weight: 600;
  color: #16181d;
  margin-bottom: 1mm;
}}
.src-url {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 8.5pt;
  color: #44464d;
  word-break: break-all;
  margin-bottom: 1mm;
}}
.src-url a {{ color: inherit; text-decoration: none; }}
.src-head {{
  font-size: 9pt;
  color: #44464d;
  font-style: italic;
  margin-bottom: 1.5mm;
}}
.src-foot {{
  display: flex;
  gap: 5mm;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8pt;
  color: #909094;
  letter-spacing: 0.04em;
}}
.src-kind {{ text-transform: uppercase; }}
.src-trust {{ color: #16181d; letter-spacing: 0; }}
.src-date {{ color: #909094; }}
/* ── Богатые секции (рейтинг / инсайты / gaps / claim-check) ── */
.block-page {{ page-break-inside: avoid; margin-top: 9mm; }}
.rank-list, .insight-list {{ list-style: none; padding: 0; margin: 0; }}
.rank-card {{ display: flex; gap: 10px; padding: 9px 0; border-bottom: 1px solid #ededed; page-break-inside: avoid; }}
.rank-num {{ flex: 0 0 auto; width: 21px; height: 21px; border-radius: 50%; background: #b3261e; color: #fff; font-family: 'Geist', sans-serif; font-weight: 700; font-size: 10.5pt; line-height: 21px; text-align: center; }}
.rank-body {{ flex: 1; }}
.rank-head {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; }}
.rank-name {{ font-family: 'Geist', sans-serif; font-weight: 600; font-size: 11.5pt; color: #16181d; }}
.rank-score {{ font-family: 'JetBrains Mono', monospace; font-weight: 600; color: #b3261e; font-size: 11pt; }}
.rank-max {{ color: #b0b0b4; font-size: 8.5pt; }}
.rank-gap {{ font-family: 'Geist', sans-serif; font-size: 8pt; color: #9a6a00; background: #fdf3e0; padding: 1px 7px; border-radius: 8px; }}
.rank-rationale {{ font-size: 10pt; color: #3a3d44; line-height: 1.5; }}
.insight-item {{ padding: 7px 0 7px 12px; border-left: 3px solid #1f4e79; margin-bottom: 9px; page-break-inside: avoid; }}
.insight-hl {{ font-family: 'Geist', sans-serif; font-weight: 600; font-size: 11pt; color: #16181d; margin-bottom: 2px; }}
.insight-expl {{ font-size: 10pt; color: #3a3d44; line-height: 1.5; }}
.insight-impact {{ font-size: 9pt; color: #6b7280; margin-top: 3px; font-style: italic; }}
.gap-list {{ margin: 0; padding-left: 18px; }}
.gap-item {{ font-size: 10pt; color: #3a3d44; margin-bottom: 4px; }}
.gap-what {{ font-weight: 600; color: #16181d; }}
.cc-section {{ margin: 7mm 0 0; }}
.cc-box {{ display: flex; gap: 10px; flex-wrap: wrap; }}
.cc-pill {{ font-family: 'Geist', sans-serif; font-size: 9pt; padding: 4px 11px; border-radius: 12px; }}
.cc-pill.ok {{ background: #e7f4ec; color: #1a7f4b; }}
.cc-pill.warn {{ background: #fdf3e0; color: #9a6a00; }}
/* ── Авто-оглавление по заголовкам ── */
.toc-page {{ page-break-after: always; margin-top: 4mm; }}
.toc-list {{ list-style: none; padding: 0; margin: 6mm 0 0; }}
.toc-list li {{ margin: 3px 0; line-height: 1.4; }}
.toc-list a {{ text-decoration: none; color: #16181d; }}
.toc-l1 {{ font-family: 'Geist', sans-serif; font-weight: 600; font-size: 11.5pt; margin-top: 8px; }}
.toc-l2 {{ font-family: 'Geist', sans-serif; font-size: 9.5pt; padding-left: 16px; }}
.toc-l2 a {{ color: #3a3d44; }}
</style>
</head>
<body>
  <section class="cover">
    <div class="mark">
      <span>AuditLens · Bank Audit Platform</span>
      <span class="id">{_esc(audit_id)}</span>
    </div>
    <div class="eyebrow">Аналитический отчёт</div>
    <h1>{_esc(doc_title)}</h1>
    <dl class="meta">
      <dt>Дата</dt><dd>{_esc(now_iso)}</dd>
      <dt>Источников</dt><dd>{len(sources)}</dd>
      <dt>Цитирований</dt><dd>{n_cites}</dd>
      {f'<dt>Верификация</dt><dd>{_esc(meta.get("verified",""))} фактов проверено</dd>' if meta.get("verified") else ""}
    </dl>
  </section>

  {toc_html}

  <section class="body">
    {body_html}
  </section>

  {ranking_html}

  {insights_html}

  {charts_html}

  {claimcheck_html}

  {verification_html}

  {gaps_html}

  <section class="sources-page" id="sec-sources">
    <h2>Источники</h2>
    <div class="lede">Полный список с метаданными — для верификации цитат</div>
    {sources_html}
  </section>
  {charts_js}
</body>
</html>"""


def render_pdf(html_str: str) -> bytes:
    """HTML → PDF bytes через Playwright Chromium.
    Использует системный/bundled Chromium, тот же что и fetcher."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                      args=["--no-sandbox",
                                             "--disable-blink-features=AutomationControlled"])
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.set_content(html_str, wait_until="networkidle", timeout=30000)
            # Ждём загрузку шрифтов Google Fonts
            try:
                page.evaluate("document.fonts.ready")
            except Exception:
                pass
            # Если на странице есть chart-canvas — ждём флаг __chartsRendered
            # который выставляется после _runCharts(). Это надёжнее чем фикс.
            # таймаут — иначе на медленном CDN можем снять PDF до рендера.
            try:
                has_charts = page.evaluate(
                    "document.querySelector('[id^=pdfchart_]') !== null"
                )
                if has_charts:
                    page.wait_for_function(
                        "window.__chartsRendered === true",
                        timeout=12000,
                    )
                    # Маленький tail чтобы Chart.js успел отрисовать data-labels
                    page.wait_for_timeout(250)
            except Exception as e:
                log.warning("PDF chart-render wait failed: %s "
                             "(PDF будет создан, но графики могут быть пустые)", e)
            pdf = page.pdf(
                format="A4",
                print_background=True,
                margin={"top":"0mm","bottom":"0mm","left":"0mm","right":"0mm"},
                prefer_css_page_size=True,
            )
            ctx.close()
            return pdf
        finally:
            browser.close()


def export_report_to_pdf(*, question: str, report_md: str,
                          sources: list[dict] | None = None,
                          meta: dict | None = None,
                          verification: dict | None = None,
                          charts: list[dict] | None = None,
                          ranking: dict | None = None,
                          insights: list[dict] | None = None,
                          gaps: dict | None = None,
                          claim_check: dict | None = None) -> bytes:
    """Главный API. Возвращает bytes PDF'а."""
    html_str = build_pdf_html(question=question, report_md=report_md,
                                sources=sources, meta=meta,
                                verification=verification,
                                charts=charts, ranking=ranking,
                                insights=insights, gaps=gaps,
                                claim_check=claim_check)
    return render_pdf(html_str)
