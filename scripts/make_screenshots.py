"""Снимает скриншоты UI для README.md через Playwright.

Запуск:
    cd <repo> && .venv/bin/python scripts/_make_screenshots.py

Требует:
    - запущенный сервер на http://127.0.0.1:8000
    - playwright + chromium (уже в .venv)

Сохраняет в docs/img/01_main_ui.png, 02_deep_research_in_progress.png и т.д.
Для деморежима использует sample-вопрос про карту ветерана СВО.
"""
from __future__ import annotations
import asyncio, os, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
OUT.mkdir(parents=True, exist_ok=True)

URL = "http://127.0.0.1:8000"
SAMPLE_Q = ("Сравни условия премиальных дебетовых карт Сбер Прайм, "
            "Тинькофф Premium, Альфа Wealth по комиссиям, кешбэку, привилегиям")


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ playwright не установлен. pip install playwright && playwright install chromium")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,        # retina-quality
        )
        page = await ctx.new_page()

        # 01: главный dashboard (analytics view — это первая страница)
        print(f"→ {URL}")
        await page.goto(URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)   # React + Babel компилируется
        await page.screenshot(path=str(OUT / "01_dashboard.png"), full_page=False)
        print(f"  ✅ 01_dashboard.png (analytics dashboard)")

        # 02: переходим в ИИ-аналитик (чат) — по sidebar
        try:
            ai_link = page.get_by_text("ИИ-аналитик").first
            await ai_link.click()
            await page.wait_for_timeout(1500)
            await page.screenshot(path=str(OUT / "02_chat_welcome.png"), full_page=False)
            print(f"  ✅ 02_chat_welcome.png (чат-интерфейс)")
        except Exception as e:
            print(f"  ⚠ 02 (chat-welcome) пропущен: {e}")

        # 03: вопрос введён, deep mode включён
        try:
            await page.wait_for_selector("textarea.chat-textarea", timeout=10000)
            ta = page.locator("textarea.chat-textarea").first
            await ta.click()
            await ta.fill(SAMPLE_Q)
            await page.wait_for_timeout(500)
            deep_btn = page.get_by_role("button", name="Deep Research")
            if await deep_btn.count() > 0:
                await deep_btn.first.click()
                await page.wait_for_timeout(500)
            await page.screenshot(path=str(OUT / "03_question_with_deep.png"), full_page=False)
            print(f"  ✅ 03_question_with_deep.png")
        except Exception as e:
            print(f"  ⚠ 03 пропущен: {e}")

        # 04: остальные секции sidebar — Источники / База знаний / Отзывы
        try:
            for label, fname in [
                ("Источники",   "04_sources.png"),
                ("База знаний", "05_knowledge_base.png"),
                ("Отзывы",      "06_reviews.png"),
                ("Рынок",       "07_market.png"),
            ]:
                lnk = page.get_by_text(label).first
                if await lnk.count() > 0:
                    await lnk.click()
                    await page.wait_for_timeout(1200)
                    await page.screenshot(path=str(OUT / fname), full_page=False)
                    print(f"  ✅ {fname}")
        except Exception as e:
            print(f"  ⚠ дополнительные секции пропущены: {e}")

        await browser.close()
        print(f"\n✨ Готово. Файлы в {OUT}/")


if __name__ == "__main__":
    asyncio.run(main())
