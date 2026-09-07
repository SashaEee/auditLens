#!/usr/bin/env python
"""Рендерит SVG-ассеты README так, как их увидит читатель на GitHub.

GitHub масштабирует картинку под ширину колонки, поэтому судить о размере
шрифта по числу в `viewBox` нельзя — надо смотреть на итоговые пиксели.
Скрипт снимает каждый ассет на десктопной ширине (900) и мобильной (360),
в светлой и тёмной подложке.

    python scripts/preview_readme_assets.py [имя.svg ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "readme"
OUT = ASSETS / "_preview"

# Ширины, на которых GitHub реально показывает README.
WIDTHS = [("desktop", 900), ("mobile", 360)]
BACKDROPS = [("light", "#ffffff"), ("dark", "#0d1117")]


def main() -> int:
    from playwright.sync_api import sync_playwright

    names = sys.argv[1:] or sorted(p.name for p in ASSETS.glob("*.svg"))
    if not names:
        print("нет ассетов в", ASSETS)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for name in names:
            src = (ASSETS / name).resolve()
            if not src.exists():
                print("нет файла:", src)
                continue
            svg = src.read_text(encoding="utf-8")
            for wname, w in WIDTHS:
                for bname, bg in BACKDROPS:
                    page.set_viewport_size({"width": w, "height": 600})
                    # Разметку встраиваем, а не подключаем ссылкой: Chromium
                    # не отдаёт file:// подресурсы странице about:blank, и
                    # картинка приезжала битой. Ассеты самодостаточны, так что
                    # встраивание показывает ровно то же, что покажет GitHub.
                    page.set_content(
                        f'<body style="margin:0;background:{bg}">'
                        f'<div id="wrap" style="width:100%">{svg}</div>'
                        f'<style>#wrap svg{{width:100%;height:auto;display:block}}</style>'
                        f'</body>')
                    page.wait_for_timeout(120)
                    out = OUT / f"{Path(name).stem}.{wname}.{bname}.png"
                    page.locator("#wrap").screenshot(path=str(out))
                    print("снято:", out.relative_to(ROOT))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
