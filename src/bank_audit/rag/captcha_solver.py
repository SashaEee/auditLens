"""Captcha auto-solver: интеграция с 2Captcha API для Yandex SmartCaptcha.

Активируется только если задан `TWOCAPTCHA_KEY` в env. Без ключа — graceful
no-op, fallback на UI «Решить капчу».

Стоимость: ~$0.99 за 1000 решений Yandex SmartCaptcha. Free trial $0.50 = 50.
Для аудитного use-case 50 капч/месяц обычно достаточно.

API docs: https://2captcha.com/2captcha-api#yandex-smartcaptcha

Альтернатива: CapSolver, AntiCaptcha — interface схожий, можно переключить.
"""
from __future__ import annotations
import logging, os, re, time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_2CAPTCHA_BASE = "https://2captcha.com"
_POLL_INTERVAL = 5
_MAX_WAIT = 180  # 3 минуты на решение


def _extract_sitekey(html: str) -> str | None:
    """Yandex SmartCaptcha sitekey обычно в <div data-sitekey="...">
    или в JS: smartCaptcha.render({sitekey: 'xxx', ...})."""
    m = re.search(r'data-sitekey="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'sitekey["\s:]+["\']([A-Za-z0-9_\-]{20,})["\']', html)
    if m:
        return m.group(1)
    return None


def solve_via_2captcha(url: str, browser=None):
    """Проводит SmartCaptcha-вызов через 2Captcha и инжектит токен в страницу.
    Возвращает FetchResult с уже валидным content (или None при неудаче).

    Алгоритм:
      1. Открываем URL в Playwright (с прогретым профилем)
      2. Извлекаем sitekey из HTML
      3. Submit task в 2Captcha API
      4. Polling каждые 5с до получения токена (max 3 мин)
      5. Inject token в скрытое поле + submit form
      6. Ждём навигации, возвращаем итоговый HTML
    """
    api_key = os.getenv("TWOCAPTCHA_KEY")
    if not api_key:
        log.warning("solve_via_2captcha: TWOCAPTCHA_KEY не задан — пропуск")
        return None

    from ..collectors.browser import BrowserCollector
    from .fetcher import FetchResult

    own_browser = browser or BrowserCollector(headless=True)

    # Открываем страницу через Playwright чтобы извлечь sitekey
    with own_browser._ctx() as ctx:
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("captcha solve: navigate failed: %s", e)
            return None

        html = page.content()
        sitekey = _extract_sitekey(html)
        if not sitekey:
            log.warning("captcha solve: sitekey not found")
            return None

        # 1. Submit task
        try:
            with httpx.Client(timeout=30) as c:
                submit = c.post(f"{_2CAPTCHA_BASE}/in.php", data={
                    "key":     api_key,
                    "method":  "yandex",
                    "sitekey": sitekey,
                    "pageurl": url,
                    "json":    1,
                })
                sub_data = submit.json()
                if sub_data.get("status") != 1:
                    log.warning("2captcha submit failed: %s", sub_data)
                    return None
                req_id = sub_data["request"]
                log.info("2captcha task submitted: id=%s", req_id)

                # 2. Polling
                deadline = time.time() + _MAX_WAIT
                token = None
                while time.time() < deadline:
                    time.sleep(_POLL_INTERVAL)
                    poll = c.get(f"{_2CAPTCHA_BASE}/res.php", params={
                        "key":    api_key,
                        "action": "get",
                        "id":     req_id,
                        "json":   1,
                    })
                    pd = poll.json()
                    if pd.get("status") == 1:
                        token = pd["request"]
                        log.info("2captcha solved in %.0fs", _MAX_WAIT - (deadline - time.time()))
                        break
                    if pd.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                        log.warning("2captcha error: %s", pd)
                        return None
                if not token:
                    log.warning("2captcha timeout")
                    return None
        except Exception as e:
            log.warning("2captcha API error: %s", e)
            return None

        # 3. Inject token в страницу
        try:
            page.evaluate("""(token) => {
                // Пытаемся найти поле smart-token
                const inputs = document.querySelectorAll('input[name="smart-token"]');
                inputs.forEach(i => i.value = token);
                // Триггерим callback (некоторые реализации smartCaptcha слушают)
                if (window.smartCaptcha && window.smartCaptcha.execute) {
                    window.smartCaptcha.execute(token);
                }
                // Submit form если есть
                const form = document.querySelector('form');
                if (form) form.submit();
            }""", token)
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            log.warning("captcha token inject failed: %s", e)
            return None

        # 4. После решения — забираем итоговый HTML
        final_html = page.content()
        return FetchResult(
            url=url, final_url=page.url,
            status=200,
            content=final_html.encode("utf-8"),
            content_type="text/html",
            via="playwright_solved",
            captcha=False,
        )


def is_available() -> bool:
    return bool(os.getenv("TWOCAPTCHA_KEY"))
