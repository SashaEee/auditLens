"""Локальная политика прямых исходящих соединений модуля «Лазейки»."""
from __future__ import annotations

import os

import httpx

_PROXY_ENV_NAMES = (
    "ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "all_proxy", "http_proxy", "https_proxy", "no_proxy",
)


def async_client(*, timeout: float | httpx.Timeout | None = None) -> httpx.AsyncClient:
    """Создаёт HTTP-клиент без proxy из окружения процесса."""
    return httpx.AsyncClient(trust_env=False, timeout=timeout)


def sync_client(**kwargs: object) -> httpx.Client:
    """Создаёт синхронный HTTP-клиент без proxy из окружения процесса."""
    return httpx.Client(trust_env=False, **kwargs)


def child_env() -> dict[str, str]:
    """Возвращает копию окружения для parser без proxy-переменных."""
    result = os.environ.copy()
    for name in _PROXY_ENV_NAMES:
        result.pop(name, None)
    return result


def chromium_args() -> list[str]:
    """Аргументы Chromium для прямого подключения без системного proxy."""
    return ["--no-proxy-server", "--proxy-bypass-list=<-loopback>"]
