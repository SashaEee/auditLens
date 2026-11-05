"""Новогоднее оформление (docs/NEW_YEAR.md): сезон, фазы, принципы цвета и поведение.

Правила, которые держат концепцию:
- праздник живёт в оформлении, а не в данных: токены --ny-* только под html.ny,
  палитра сайта (и её копия в модуле «Аудит уязвимостей») не меняется;
- красного и зелёного в праздничной палитре нет — здесь они значат «риск» и «Сбер»;
- золото не идёт в текст интерфейса (жёлтый в тексте значит «внимание»),
  исключение — цифры года на обложке;
- оформление не печатается, анимации уважают «уменьшить движение».

Логика сезона проверяется на настоящих функциях из app.jsx в браузере, поведение —
на настоящих index.html и app.js; API подменено заглушкой, сети и БД нет.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "bank_audit" / "web" / "static"
INDEX = STATIC / "index.html"
JSX = STATIC / "app.jsx"
ORIGIN = "http://auditlens.test"


def _css() -> str:
    html = INDEX.read_text(encoding="utf-8")
    css = html[html.index("<style>"):html.index("</style>")]
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    assert m, f"нет блока {selector}"
    return m.group(1)


def _fn(src: str, name: str) -> str:
    """Исходник функции верхнего уровня из app.jsx — до строки, начинающей следующее объявление."""
    start = src.index(f"function {name}(")
    nxt = re.search(r"\n(?:function |const |let |// )", src[start + 1:])
    return src[start:start + 1 + nxt.start()]


# ── палитра и стили ─────────────────────────────────────────────────────────

def test_ny_tokens_live_only_under_html_ny():
    css = _css()
    assert "--ny-" not in _block(css, ":root"), "праздничные токены не входят в палитру сайта"
    assert "--ny-" not in _block(css, "html.dark")
    light, dark = _block(css, "html.ny"), _block(css, "html.ny.dark")
    for name in ("--ny-gold", "--ny-frost", "--ny-pearl", "--ny-snow", "--ny-night"):
        assert name in light and name in dark, name


def test_ny_palette_has_no_red_or_green():
    """Красный в AuditLens — риск, зелёный — Сбер: в празднике их нет."""
    css = _css()
    for sel in ("html.ny", "html.ny.dark"):
        found = re.findall(r"(--ny-[\w-]+):\s*oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)",
                           _block(css, sel))
        for name, _lightness, c, h in found:
            c, h = float(c), float(h)
            if c < 0.03:
                continue
            assert not (h <= 45 or h >= 345), f"{sel} {name}: красный оттенок {h}"
            assert not (110 <= h <= 170), f"{sel} {name}: зелёный оттенок {h}"


def test_gold_is_not_interface_text():
    """Золото — в украшениях; текстом им набраны только цифры года на обложке."""
    css = _css()
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    gold = re.compile(r"(?<![\w-])color:\s*var\(--ny-gold\)")
    gold_text = [sel.strip() for sel, body in rules if gold.search(body)]
    assert gold_text == [".ny-yr"], gold_text


def test_ny_decor_not_printed():
    css = _css()
    pr = css[css.index("@media print{"):]
    hidden = pr[:pr.index("{display:none !important}")]
    for cls in (".ny-garland", ".ny-snow", ".ny-cover"):
        assert cls in hidden, cls


def _media_ranges(css: str, head: str) -> list[tuple[int, int]]:
    """Границы содержимого каждого блока @media с заголовком head (по парным скобкам)."""
    out, i = [], 0
    while (i := css.find(head, i)) != -1:
        j = css.index("{", i)
        depth = 0
        for k in range(j, len(css)):
            depth += {"{": 1, "}": -1}.get(css[k], 0)
            if depth == 0:
                out.append((j, k))
                break
        i = j
    return out


def test_ny_animations_respect_reduced_motion():
    css = _css()
    ranges = _media_ranges(css, "@media (prefers-reduced-motion:no-preference)")
    uses = [m.start() for m in re.finditer(r"animation:\s*ny-", css)]
    assert uses, "мерцание гирлянды не найдено"
    for pos in uses:
        assert any(a < pos < b for a, b in ranges), \
            "анимация ny- вне @media (prefers-reduced-motion:no-preference)"


def test_ny_snow_not_mounted_with_reduced_motion():
    src = JSX.read_text(encoding="utf-8")
    assert "{ny.on&&ny.snow&&!reduceMotion&&<NySnow/>}" in src
    assert "{!reduce&&<canvas ref={cv} className=\"ny-cover-snow\"" in src


# ── браузер ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def test_season_and_phase_logic(browser: Browser):
    src = JSX.read_text(encoding="utf-8")
    code = "\n".join(_fn(src, n) for n in ("nyInSeason", "nyPhaseOf", "nyYearOf", "nyLeft"))
    page = browser.new_page()
    try:
        page.set_content("<script>" + code + "</script>")
        res = page.evaluate("""() => {
          const D = (y, m, d, h = 10, mi = 0) => new Date(y, m - 1, d, h, mi);
          const season = [D(2026,12,14), D(2026,12,15), D(2027,1,14), D(2027,1,15), D(2026,11,4)]
            .map(nyInSeason);
          const phase = [D(2026,12,20), D(2026,12,31), D(2027,1,5), D(2027,1,13)].map(nyPhaseOf);
          const year = [[D(2026,12,20),'pre'], [D(2027,1,5),'new'],
                        [D(2026,11,4),'pre'], [D(2026,11,4),'new']].map(([d,p]) => nyYearOf(d,p));
          const left = [nyLeft(D(2026,12,17),'pre',2027).days,
                        nyLeft(D(2026,12,30,23,50),'pre',2027).days,
                        JSON.stringify(nyLeft(D(2026,12,31,17,48),'eve',2027))];
          return {season, phase, year, left};
        }""")
    finally:
        page.close()
    assert res["season"] == [False, True, True, False, False]
    assert res["phase"] == ["pre", "eve", "new", "old"]
    assert res["year"] == [2027, 2027, 2027, 2027]
    assert res["left"] == [15, 2, json.dumps({"h": 6, "m": 12}, separators=(",", ":"))]


def _index_html() -> str:
    html = INDEX.read_text(encoding="utf-8")
    html = re.sub(r'<script src="[^"]*babel[^"]*"></script>\s*', "", html)
    return re.sub(r'<script type="text/babel" src="/static/app\.jsx[^"]*"></script>',
                  '<script src="/static/app.js"></script>', html)


_MIME = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
         ".woff2": "font/woff2"}


def _open(browser: Browser, *, when: str, query: str = "", reduce: bool = False) -> Page:
    ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                              reduced_motion="reduce" if reduce else "no-preference")
    page = ctx.new_page()
    page.clock.set_fixed_time(when)
    digest = {"date": when[:10], "meta": {"today": True, "is_morning": True},
              "sections": {"headline": {"status": "ok",
                                        "payload": {"headline": "Сводка дня", "insights": []}}}}

    def route(r):
        url = r.request.url
        path = url.split(ORIGIN, 1)[-1].split("?", 1)[0]
        if not url.startswith(ORIGIN):
            return r.abort()
        if path == "/":
            return r.fulfill(status=200, content_type="text/html", body=_index_html())
        if path.startswith("/static/"):
            f = STATIC / path[len("/static/"):]
            if f.is_file():
                return r.fulfill(status=200, body=f.read_bytes(),
                                 content_type=_MIME.get(f.suffix, "application/octet-stream"))
            return r.fulfill(status=404, body="")
        if path == "/api/overview/digest":
            return r.fulfill(json=digest)
        if path == "/api/me":
            return r.fulfill(json={"name": "Анна Смирнова",
                                   "prefs": {"onboarded": True, "mail_promo": "seen"}})
        return r.fulfill(json={})

    page.route("**/*", route)
    page.add_init_script("try{localStorage.setItem('al-rename-1001b','1')}catch(e){}")
    page.goto(f"{ORIGIN}/?{query}#overview")
    page.wait_for_selector(".rail-brand")
    page.wait_for_timeout(400)
    return page


def test_in_season_decor_appears_and_data_untouched(browser: Browser):
    page = _open(browser, when="2026-12-17T10:30:00")
    try:
        assert page.evaluate("document.documentElement.classList.contains('ny')")
        assert page.locator(".ny-star").count() == 1
        assert page.locator(".ny-garland .ny-b").count() > 10
        assert page.locator("canvas.ny-snow").count() == 1
        assert page.get_attribute('link[rel="icon"]', "href") == "/static/favicon-ny.svg"
        cover = page.locator(".ny-cover")
        assert "15" in cover.inner_text() and "дней" in cover.inner_text()
        # снег за страницей: под содержимым и не ловит нажатия
        z, pe = page.evaluate("(() => { const s = getComputedStyle("
                              "document.querySelector('canvas.ny-snow'));"
                              " return [s.zIndex, s.pointerEvents]; })()")
        assert (z, pe) == ("-1", "none")
        assert page.evaluate(
            "getComputedStyle(document.querySelector('.ny-garland')).pointerEvents") == "none"
        assert page.evaluate(
            "document.documentElement.scrollWidth === document.documentElement.clientWidth")
        # табло прокручивается на следующий год, обложка скрывается до следующего года
        # нажатие без наведения: наведение само прокручивает год и смешало бы два события
        assert page.locator(".ny-cover.rolled").count() == 0
        page.dispatch_event(".ny-yr", "click")
        page.wait_for_selector(".ny-cover.rolled", timeout=2000)
        assert page.get_attribute(".ny-yr", "aria-label") == "2027 год. Прокрутить ещё раз"
        page.click(".ny-x")
        assert page.locator(".ny-cover").count() == 0
        assert page.evaluate("localStorage.getItem('al-ny-cover-2027')") == "1"
    finally:
        page.context.close()


def test_out_of_season_nothing_and_preview_works(browser: Browser):
    page = _open(browser, when="2026-11-04T10:00:00")
    try:
        assert not page.evaluate("document.documentElement.classList.contains('ny')")
        assert page.locator(".ny-cover, .ny-garland, .ny-star, canvas.ny-snow").count() == 0
        assert page.get_attribute('link[rel="icon"]', "href") == "/static/favicon.svg"
    finally:
        page.context.close()
    page = _open(browser, when="2026-11-04T10:00:00", query="ny=new")
    try:
        assert page.locator(".ny-cover").inner_text().count("С Новым годом") == 1
    finally:
        page.context.close()
    page = _open(browser, when="2026-12-20T10:00:00", query="ny=0")
    try:
        assert page.locator(".ny-cover, .ny-garland").count() == 0
    finally:
        page.context.close()


def test_reduced_motion_keeps_decor_still(browser: Browser):
    page = _open(browser, when="2026-12-17T10:30:00", reduce=True)
    try:
        assert page.locator(".ny-cover").count() == 1
        assert page.locator("canvas").count() == 0, \
            "без движения снег не идёт ни за страницей, ни в обложке"
        names = page.evaluate(
            "[...document.querySelectorAll('.ny-b b')].map(b => getComputedStyle(b).animationName)")
        assert names and set(names) == {"none"}
    finally:
        page.context.close()
