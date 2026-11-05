"""Паспорт ВСП с сайта банка: форматы обслуживания, часы работы, услуги.

Источник — офисный поиск на публичном сайте банка. Адрес сайта задаётся настройкой
VSP_BANK_SITE (в коде его нет). Страница поиска сама подгружает офисы по плиткам карты:

    GET {сайт}/proxy/services/oib/branches/v2/byTiles?t=x,y,x,y&z=8..18
      → [{id, geometry{coordinates[lat,lon]}, properties{name «Доп.офис №9038/01008», code,
          naturalPersonVip, naturalPersonPb, naturalPersonMs, naturalPersonMvs, legalPerson,
          specialFormatIslamicOfice, zone24, mobile, timeZone, services[NPS…/LPS…/APS…],
          workingHoursNp/Lp/All[14] (7 дней × [работа, перерыв], мс от полуночи),
          workingHoursSpecial[[ГГГГММДД, с, по]]}}]

Замер 05.11.2026: одна плитка масштаба 8 вокруг Москвы — 507 офисов (СберПервый 29,
Private Banking 3). Сайт закрыт проверкой для простых HTTP-клиентов, поэтому читаем его
обычным браузером (Playwright) как посетитель: открываем страницу офисов и выполняем запрос
внутри неё. Без маскировки, редко (раз в месяц), с паузой между запросами; отказ или капча —
SourceBlocked, как у остальных источников.

Код ВСП в названии («№9038/01008») совпадает с кодом в реестре ЦБ («№ 9038/01008»,
иногда без ведущих нулей) — сравниваем по ГОСБ и номеру как числу.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from collections.abc import Iterable

from ..http import CAPTCHA_MARKERS
from ..models import CollectorError, SourceBlocked

log = logging.getLogger(__name__)

TILE_PATH = "/proxy/services/oib/branches/v2/byTiles"
PAGE_PATH = "/ru/oib?tab=vsp"
ZOOM = 8                         # сервис принимает 8..18; плитка 8 — область примерно 150×100 км
SPLIT_AT = 1000                  # столько офисов в плитке — дробим на 4 (на случай потолка выдачи)
_CODE = re.compile(r"(\d{4})\s*/\s*(\d{1,5})")
_DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# признак сайта → формат во вкладке
FLAG_FORMATS = {
    "naturalPersonMs": "mass",
    "naturalPersonVip": "sber_first",
    "naturalPersonPb": "private",
    "legalPerson": "legal",
    "specialFormatIslamicOfice": "islamic",
    "zone24": "zone24",
    "mobile": "mobile",
}


def tile_of(lat: float, lon: float, z: int = ZOOM) -> tuple[int, int]:
    """Номер плитки веб-меркатора (как у карт)."""
    n = 2 ** z
    lat = max(min(lat, 85.0), -85.0)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_for_points(points: Iterable[tuple[float, float]], z: int = ZOOM) -> list[tuple[int, int]]:
    """Плитки, в которых есть хотя бы одна известная точка ВСП (карточки карт, реестр)."""
    return sorted({tile_of(lat, lon, z) for lat, lon in points if lat is not None and lon is not None})


def vsp_key(name: str | None) -> str | None:
    """«Доп.офис №9038/01008» → «9038/1008»: ГОСБ и номер без ведущих нулей."""
    m = _CODE.search(name or "")
    return f"{m.group(1)}/{int(m.group(2))}" if m else None


def _hhmm(ms: int) -> str:
    m = int(ms) // 60000
    return f"{m // 60}:{m % 60:02d}"


def hours_text(arr: list | None) -> str | None:
    """14 элементов (7 дней × [работа, перерыв]) → «пн–пт 10:00–20:00 · сб 10:00–15:00 · вс выходной»."""
    if not arr or len(arr) < 14:
        return None
    days = []
    for d in range(7):
        w = arr[2 * d]
        days.append(f"{_hhmm(w[0])}–{_hhmm(w[1])}" if w else "выходной")
    out, i = [], 0
    while i < 7:
        j = i
        while j + 1 < 7 and days[j + 1] == days[i]:
            j += 1
        span = _DAYS[i] if i == j else f"{_DAYS[i]}–{_DAYS[j]}"
        out.append(f"{span} {days[i]}")
        i = j + 1
    return " · ".join(out)


def works_on(arr: list | None, weekday: int) -> bool | None:
    """Работает ли офис в день недели (0 — понедельник). None — часов нет."""
    if not arr or len(arr) < 14:
        return None
    return arr[2 * weekday] is not None


def parse_office(f: dict) -> dict | None:
    """Офис из ответа сайта → запись паспорта."""
    p = f.get("properties") or {}
    coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
    if f.get("id") is None:
        return None
    formats = sorted({fmt for flag, fmt in FLAG_FORMATS.items() if p.get(flag)})
    return {
        "site_id": str(f["id"]),
        "code": p.get("code"),
        "name": p.get("name"),
        "vsp_key": vsp_key(p.get("name")),
        "lat": coords[0], "lon": coords[1] if len(coords) > 1 else None,
        "formats": formats,
        "services": sorted(p.get("services") or []),
        "hours": {"np": p.get("workingHoursNp"), "lp": p.get("workingHoursLp"),
                  "all": p.get("workingHoursAll"), "special": p.get("workingHoursSpecial"),
                  "tz": p.get("timeZone"), "text": hours_text(p.get("workingHoursNp") or p.get("workingHoursAll"))},
    }


class BankSiteBrowser:
    """Браузер-посетитель: страница офисов открыта, запросы — изнутри неё (те же куки и
    проверки, что у человека). Один запрос в 1/rps секунд с небольшим разбросом."""

    def __init__(self, site: str, *, rps: float = 0.5, user_agent: str | None = None,
                 timeout_s: float = 60.0):
        self.site = site.rstrip("/")
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.requests = 0
        self._pw = self._browser = self._page = None

    async def __aenter__(self) -> "BankSiteBrowser":
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        # в контейнере без привилегий песочница Chromium недоступна — как в основном браузерном модуле
        self._browser = await self._pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await self._browser.new_context(locale="ru-RU", user_agent=self.user_agent)
        self._page = await ctx.new_page()
        await self._open()
        return self

    async def _open(self) -> None:
        try:
            await self._page.goto(self.site + PAGE_PATH, wait_until="domcontentloaded",
                                  timeout=self.timeout_s * 1000)
            await self._page.wait_for_timeout(6000)       # страница проходит свою проверку сама
        except Exception as e:  # noqa: BLE001
            raise CollectorError(f"banksite: страница офисов не открылась: {type(e).__name__}") from e
        head = (await self._page.content())[:20000]
        for m in CAPTCHA_MARKERS:
            if m in head:
                raise SourceBlocked("banksite", f"captcha ({m})", self.site + PAGE_PATH)

    async def __aexit__(self, *exc) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def tile(self, x: int, y: int, z: int) -> list:
        if self.interval:
            await asyncio.sleep(self.interval * (1 + random.uniform(0, 0.25)))
        self.requests += 1
        url = f"{TILE_PATH}?t={x},{y},{x},{y}&z={z}"
        status, text = await self._page.evaluate(
            """async (u)=>{const r=await fetch(u,{credentials:'include'});return [r.status, await r.text()]}""", url)
        if status in (401, 403) or any(m in (text or "")[:5000] for m in CAPTCHA_MARKERS):
            raise SourceBlocked("banksite", f"HTTP {status}", url)
        if status >= 400:
            raise CollectorError(f"banksite: HTTP {status} {url}")
        try:
            data = json.loads(text)
        except ValueError as e:
            raise CollectorError(f"banksite: не JSON для плитки {x},{y},{z}") from e
        return data if isinstance(data, list) else []


async def fetch_offices(browser: BankSiteBrowser, tiles: Iterable[tuple[int, int]], *, z: int = ZOOM,
                        progress=None) -> dict[str, dict]:
    """Офисы по списку плиток (с дроблением слишком плотных). Повторы между плитками
    сводятся по id. Ошибка одной плитки — в журнал, обход продолжается; блокировка — стоп."""
    out: dict[str, dict] = {}
    queue = [(x, y, z) for x, y in tiles]
    done = errors = 0
    while queue:
        x, y, zz = queue.pop(0)
        try:
            feats = await browser.tile(x, y, zz)
        except SourceBlocked:
            raise
        except CollectorError as e:
            errors += 1
            log.warning("%s", e)
            continue
        if len(feats) >= SPLIT_AT and zz < 12:
            queue += [(2 * x + dx, 2 * y + dy, zz + 1) for dx in (0, 1) for dy in (0, 1)]
            continue
        for f in feats:
            rec = parse_office(f)
            if rec:
                out[rec["site_id"]] = rec
        done += 1
        if progress and done % 50 == 0:
            progress(done, len(queue), len(out))
    if errors:
        log.warning("banksite: плиток с ошибкой %d", errors)
    return out
