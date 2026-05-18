"""Cookie warming: разовый фоновый прогон чтобы профиль OPENCLAW получил
свежие cookies на ключевых доменах.

Задача — снизить частоту captcha:
  • Если capcha СРАБАТЫВАЕТ при разогреве — пользователь решает 1 раз
  • Дальше cookies валидны 24h → автоматизированные fetch'и проходят без капчи

Запуск:
  • При старте сервера (если последний прогон >= 12h назад)
  • По cron в 00:30 (через scheduled task)
  • Вручную: POST /api/rag/warm-cookies
"""
from __future__ import annotations
import asyncio, logging, random, time
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

from .. import db
from . import cache as rag_cache

log = logging.getLogger(__name__)

# Список доменов, которые надо «прогревать» — каждое утро
WARM_TARGETS = [
    "https://www.sravni.ru/",
    "https://www.banki.ru/",
    "https://bankiros.ru/",
    "https://www.sberbank.ru/",
    "https://alfabank.ru/",
    "https://www.tbank.ru/",
    "https://www.vtb.ru/",
]

WARM_INTERVAL_H = 12
WARM_KEY = "cookie_warming:last_run"


def _human_like_browse(page, duration_s: int = 8):
    """Имитация чтения: случайные scrolls и hover. Снижает шанс bot-detection."""
    end = time.time() + duration_s
    width  = 1440
    height = 900
    while time.time() < end:
        # Random scroll
        amount = random.randint(200, 800)
        if random.random() < 0.85:
            page.mouse.wheel(0, amount)
        else:
            page.mouse.wheel(0, -amount // 2)
        # Random mouse move
        try:
            page.mouse.move(random.randint(50, width - 50),
                            random.randint(50, height - 50),
                            steps=random.randint(5, 15))
        except Exception:
            pass
        time.sleep(random.uniform(0.4, 1.2))


def warm_one(url: str, headless: bool = False, duration_s: int = 8) -> dict:
    """Открывает url в Playwright и имитирует человеческую активность."""
    from ..collectors.browser import BrowserCollector, CaptchaRequired
    b = BrowserCollector(headless=headless)
    try:
        with b._ctx(headless=headless) as ctx:
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                return {"url": url, "ok": False, "error": str(e)[:120]}
            # Проверка на капчу
            head = page.content()[:8192]
            from ..collectors.browser import CAPTCHA_MARKERS
            if any(m in head for m in CAPTCHA_MARKERS):
                # Сохраняем pending — юзер решит руками
                b._save_captcha_pending(url, "cookie_warming",
                                        workspace_dir=None,
                                        target=None)
                return {"url": url, "ok": False, "captcha": True}
            _human_like_browse(page, duration_s)
            return {"url": url, "ok": True, "duration_s": duration_s}
    except Exception as e:
        return {"url": url, "ok": False, "error": str(e)[:120]}


def warm_all(headless: bool = False) -> dict:
    """Прогоняет все WARM_TARGETS с jitter между ними. Сохраняет timestamp в кэш.

    headless=False — тёплые cookies лучше с видимым браузером (sravni мягче).
    Если рабочий день уже идёт — можно использовать headless=True (быстрее).
    """
    log.info("cookie_warming: starting %s targets, headless=%s",
             len(WARM_TARGETS), headless)
    results = []
    for i, url in enumerate(WARM_TARGETS):
        if i > 0:
            time.sleep(random.uniform(5, 12))     # jitter между прогревами
        r = warm_one(url, headless=headless)
        results.append(r)
        log.info("warm %s: %s", url, r)

    rag_cache.put(
        "warming", {"ts": datetime.now(timezone.utc).isoformat(),
                    "results": results},
        WARM_INTERVAL_H * 3600,
        WARM_KEY,
    )
    return {"results": results, "ts": datetime.now(timezone.utc).isoformat()}


def needs_warming() -> bool:
    """Проверяет, прошло ли > WARM_INTERVAL_H часов с последнего прогрева."""
    last = rag_cache.get("warming", WARM_KEY)
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(last["ts"])
        return datetime.now(timezone.utc) - ts > timedelta(hours=WARM_INTERVAL_H)
    except Exception:
        return True


async def warm_background_loop(initial_delay_s: int = 120):
    """Фоновый цикл: при старте проверяет needs_warming(), потом каждые 12h."""
    await asyncio.sleep(initial_delay_s)  # даём серверу прогреться
    while True:
        try:
            if needs_warming():
                log.info("cookie_warming: starting scheduled warm")
                # Headless=True в фоне, чтобы не открывать окно неожиданно
                # Если поймаем капчу — она в captcha_pending, юзер решит вручную
                await asyncio.get_event_loop().run_in_executor(None, warm_all, True)
            else:
                log.info("cookie_warming: skipping (recent warm)")
        except Exception as e:
            log.warning("cookie_warming loop error: %s", e)
        # Спим 12h до следующей проверки
        await asyncio.sleep(WARM_INTERVAL_H * 3600)
