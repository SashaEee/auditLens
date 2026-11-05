"""HTTP-транспорт источников: ограничение частоты, повторы, распознавание блокировки.

Правила (общие для всех адаптеров):
- частота — не выше заданной для хоста (token bucket с паузой между запросами);
- повторы — только на временные сбои (сеть, 429, 5xx), с растущей паузой;
- 403, капча, «подтвердите, что вы не робот» — SourceBlocked: источник
  останавливается в этом прогоне, обходить защиту сборщик не пытается.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from .models import CollectorError, NotFound, SourceBlocked

log = logging.getLogger(__name__)

CAPTCHA_MARKERS = (
    "SmartCaptcha", "smartcaptcha", "showcaptcha", "checkcaptcha", "captcha-delivery",
    "Подтвердите, что запросы отправляли вы", "похожи на автоматические",
    "Вы не робот", "Are you not a robot", '"type":"captcha"',
)


class RateLimiter:
    """Не чаще rps запросов в секунду (минимальный интервал + небольшой разброс)."""

    def __init__(self, rps: float, jitter: float = 0.25):
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self.jitter = jitter
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            gap = self.interval * (1 + random.uniform(0, self.jitter))
            self._next = time.monotonic() + gap


def looks_blocked(status: int, text: str) -> str | None:
    """Причина блокировки по ответу или None."""
    if status in (401, 403):
        return f"HTTP {status}"
    head = text[:20000]
    for m in CAPTCHA_MARKERS:
        if m in head:
            return f"captcha ({m})"
    return None


class SourceHttp:
    """Клиент одного источника: свой лимит частоты, свои повторы, свой журнал."""

    def __init__(self, source: str, *, rps: float, timeout_s: float, max_retries: int,
                 user_agent: str, headers: dict[str, str] | None = None,
                 client: httpx.AsyncClient | None = None):
        self.source = source
        self.limiter = RateLimiter(rps)
        self.max_retries = max_retries
        self.requests = 0
        self.client = client or httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept-Language": "ru-RU,ru;q=0.9", **(headers or {})})

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, url: str, *, params: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None, expect_json: bool = True) -> Any:
        """GET с лимитом и повторами. Возвращает JSON (или текст при expect_json=False)."""
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            await self.limiter.wait()
            self.requests += 1
            try:
                r = await self.client.get(url, params=params, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt >= self.max_retries:
                    raise CollectorError(f"{self.source}: сеть: {type(e).__name__}") from e
                log.info("%s: сеть (%s), повтор через %.0f с", self.source, type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            text = r.text
            why = looks_blocked(r.status_code, text)
            if why:
                raise SourceBlocked(self.source, why, str(r.url))
            if r.status_code == 404:
                raise NotFound(f"{self.source}: 404 {r.url}")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt >= self.max_retries:
                    raise CollectorError(f"{self.source}: HTTP {r.status_code} {r.url}")
                ra = r.headers.get("retry-after")
                wait = float(ra) if ra and ra.isdigit() else delay
                log.info("%s: HTTP %s, повтор через %.0f с", self.source, r.status_code, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, 120)
                continue
            if r.status_code >= 400:
                raise CollectorError(f"{self.source}: HTTP {r.status_code} {r.url}")
            if not expect_json:
                return text
            try:
                return r.json()
            except ValueError as e:
                raise CollectorError(f"{self.source}: не JSON от {r.url}") from e
        raise CollectorError(f"{self.source}: повторы исчерпаны")
