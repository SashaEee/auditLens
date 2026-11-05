"""Домклик: адаптер-заглушка с честной проверкой доступности.

Проверка 05.11.2026: отзывов об отделениях и ипотечных центрах Сбербанка на Домклике
не найдено — отзывы там о риелторах и агентствах (agencies.domclick.ru). Страницы
domclick.ru за JS-проверкой Qrator (HTTP 401, X-Qrator-QAuth-Response: auth-required);
это не геоблокировка — robots.txt и индекс sitemap отдаются. Проходить проверку
браузером мы не стали: без отзывов об отделениях это не окупается.

Адаптер оставлен, чтобы интерфейс и хранилище были готовы, если источник откроется
(например, официальным доступом): availability() фиксирует текущее состояние.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..http import SourceHttp
from ..models import SourceBlocked, SourceBranch, ReviewPage
from .base import Area, Capabilities, Collector


class DomclickCollector(Collector):
    source = "domclick"
    capabilities = Capabilities(method="none", sorts=(), full_history=False, max_reachable=0, replies=False,
                                notes="отзывов об отделениях нет; сайт за Qrator")

    def __init__(self, settings, http: SourceHttp | None = None):
        super().__init__(settings)
        self.http = http or SourceHttp("domclick", rps=self.cfg.rps, timeout_s=self.cfg.timeout_s,
                                       max_retries=1, user_agent=settings.user_agent)

    async def close(self) -> None:
        await self.http.close()

    async def availability(self) -> str:
        """ok — главная отдаётся; иначе причина (Qrator/401)."""
        try:
            await self.http.get("https://domclick.ru/", expect_json=False)
            return "ok"
        except SourceBlocked as e:
            return e.reason

    async def discover(self, area: Area) -> AsyncIterator[SourceBranch]:
        raise SourceBlocked("domclick", "нет отзывов об отделениях; сайт за Qrator")
        yield  # pragma: no cover

    async def branch(self, source_branch_id: str) -> SourceBranch:
        raise SourceBlocked("domclick", "нет отзывов об отделениях; сайт за Qrator")

    async def pages(self, source_branch_id: str, *, sort: str | None = None,
                    cursor: str | None = None) -> AsyncIterator[ReviewPage]:
        raise SourceBlocked("domclick", "нет отзывов об отделениях; сайт за Qrator")
        yield  # pragma: no cover
