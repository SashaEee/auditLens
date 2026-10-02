"""MCP-сервер AuditLens: инструменты аналитика для агента Hermes.

Hermes подключается к нему как к MCP-серверу (config.yaml → mcp_servers.auditlens)
и видит инструменты из ai/agent_tools.py как обычные функции с описаниями и
схемами аргументов — вместо psql и curl с ручным URL-кодированием.

Доступ. Сервер смонтирован в приложение на /mcp/ и отвечает только если:
  • задан AGENT_MCP_KEY и запрос несёт его в Authorization: Bearer;
  • запрос пришёл напрямую на 127.0.0.1, а не через внешний прокси
    (у проксированного есть X-Forwarded-For / X-Real-IP).
Без ключа сервер выключен (404). Инструменты только читают данные.
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
import time
from typing import Annotated, Any

from pydantic import Field

from . import agent_tools as T

log = logging.getLogger(__name__)

MCP_KEY = os.getenv("AGENT_MCP_KEY", "")
TOOL_TIMEOUT_S = float(os.getenv("AGENT_TOOL_TIMEOUT_S", "45"))

# Описания аргументов — модель видит их в схеме инструмента.
ARG_HELP = {
    "bank": "Банк: «Сбербанк», «ВТБ», «Альфа-Банк», «Газпромбанк»…",
    "product": "Продукт по кодификатору жалоб; пусто — все продукты",
    "days": "Окно в днях",
    "theme": "Ключ темы жалоб (перечень с подписями — в описании complaint_theme)",
    "query": "Что искать, своими словами",
    "category": "Категория продуктов витрины «Рынок»",
    "banks": "Банки для сравнения со Сбером, например [\"ВТБ\", \"Газпромбанк\"]",
    "limit": "Сколько записей вернуть",
    "document_id": "Номер документа из результата knowledge_search",
    "url": "Полная ссылка",
    "sites": "Ограничить сайтами, например [\"cbr.ru\", \"pravo.gov.ru\"]",
    "fresh_days": "Только материалы не старше N дней",
    "doc_type": "Тип документа: pdf, html, xlsx",
}


def _wrap(spec: T.ToolSpec):
    """async-обёртка: синхронный инструмент — в пуле потоков, с таймаутом
    и понятной модели ошибкой вместо трассировки."""
    fn = spec.fn

    async def run(**kwargs: Any) -> str:
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(asyncio.to_thread(fn, **kwargs), TOOL_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("[mcp] %s: таймаут %ss", spec.name, TOOL_TIMEOUT_S)
            return T.out({"error": f"инструмент не ответил за {int(TOOL_TIMEOUT_S)} с"})
        except Exception as e:  # noqa: BLE001
            log.warning("[mcp] %s(%s): %s", spec.name, kwargs, e, exc_info=True)
            return T.out({"error": f"инструмент упал: {type(e).__name__}: {str(e)[:200]}"})
        log.info("[mcp] %s %s → %d зн за %.1f с", spec.name,
                 {k: v for k, v in kwargs.items() if v not in (None, "")}, len(res or ""),
                 time.monotonic() - t0)
        return res or T.out({"error": "пустой ответ"})

    run.__name__ = spec.name
    run.__doc__ = spec.description
    # Сигнатура — от исходной функции, с описаниями типовых аргументов:
    # FastMCP строит из неё JSON-схему инструмента.
    params = []
    hints = inspect.get_annotations(fn, eval_str=True)
    for p in inspect.signature(fn).parameters.values():
        ann = hints.get(p.name, p.annotation)
        if p.name in ARG_HELP:
            ann = Annotated[ann, Field(description=ARG_HELP[p.name])]
        params.append(p.replace(annotation=ann, kind=inspect.Parameter.KEYWORD_ONLY))
    run.__signature__ = inspect.Signature(params, return_annotation=str)
    run.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    return run


def build():
    """FastMCP со всеми инструментами. Без состояния, ответы JSON — Hermes
    держит одно соединение, повторные вызовы дёшевы."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    srv = FastMCP(
        "auditlens",
        instructions=("Инструменты AuditLens: жалобы клиентов, рынок продуктов, банки, "
                      "база знаний, новости, веб. Числа совпадают с вкладками AuditLens."),
        stateless_http=True, json_response=True, streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "127.0.0.1", "localhost"],
            allowed_origins=[]),
    )
    for spec in T.TOOLS:
        srv.add_tool(_wrap(spec), name=spec.name, title=spec.label, description=spec.description)
    return srv


class Guard:
    """ASGI-обёртка: ключ + только прямые локальные запросы."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        client = (scope.get("client") or ("", 0))[0]
        proxied = "x-forwarded-for" in headers or "x-real-ip" in headers
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        ok = (bool(MCP_KEY) and not proxied and client in ("127.0.0.1", "::1")
              and hmac.compare_digest(token, MCP_KEY))
        if not ok:
            status = 404 if not MCP_KEY or proxied else 401
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": b"not found" if status == 404
                        else b"unauthorized"})
            return
        return await self.app(scope, receive, send)


_server = None
_app = None


def server():
    """Сервер и его ASGI-приложение собираются один раз: менеджер сессий
    (нужен в lifespan приложения) появляется только при сборке приложения."""
    global _server, _app
    if _server is None:
        _server = build()
        _app = _server.streamable_http_app()
    return _server


def asgi_app():
    server()
    return Guard(_app)
