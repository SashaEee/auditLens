"""Идентичность пользователя из Authentik (forward-auth через nginx).

Nginx перед приложением делает `auth_request` к Authentik-аутпосту
(`/outpost.goauthentik.io/auth/nginx`) и прокидывает в приложение РОВНО два
заголовка (см. `/etc/nginx/conf.d/auditlens.conf` на ВМ):

    proxy_set_header X-Authentik-Username $authentik_username;
    proxy_set_header X-Authentik-Name     $authentik_name;

Клиент их подделать НЕ может: nginx выставляет их сам из ответа аутпоста
(`auth_request_set ... $upstream_http_x_authentik_*`), перезатирая любое
клиентское значение. Email/группы/uid сейчас НЕ прокидываются — чтобы их
получить, ОАИТ должен добавить соответствующие `auth_request_set` +
`proxy_set_header` (Authentik-аутпост их отдаёт, nginx просто не форвардит).

⚠️ Приложение слушает 0.0.0.0:8000 напрямую — запрос в обход nginx может
прислать фейковый X-Authentik-Username. Снаружи порт закрыт security-группой;
доверять заголовку можно только на пути через nginx.

Локально (без nginx) заголовков нет → возвращаем dev-пользователя.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header


@dataclass(frozen=True)
class CurrentUser:
    """Аутентифицированный пользователь (или dev-fallback вне nginx)."""

    username: str        # X-Authentik-Username — стабильный уникальный ключ
    name: str            # X-Authentik-Name — отображаемое имя
    authenticated: bool  # True, если identity реально пришла от Authentik

    @property
    def is_anonymous(self) -> bool:
        return not self.authenticated


# Значение для локальной разработки без Authentik. В проде не используется:
# за nginx заголовок всегда есть.
_DEV_USER = os.getenv("DEV_USER", "local-dev")


def _fix_header_encoding(s: str) -> str:
    """HTTP-заголовки Starlette декодирует как latin-1, а Authentik кладёт имя
    в UTF-8 → кириллица приходит мохибейком. Восстанавливаем: latin-1→bytes→utf-8.
    Если строка ASCII или уже корректна — возвращаем как есть.
    """
    if not s or s.isascii():
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def get_current_user(
    x_authentik_username: Annotated[str | None, Header()] = None,
    x_authentik_name: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """FastAPI-зависимость: текущий пользователь из заголовков Authentik.

    Использование:  `user: CurrentUser = Depends(get_current_user)`.
    """
    username = (x_authentik_username or "").strip()
    if username:
        name = _fix_header_encoding((x_authentik_name or "").strip()) or username
        return CurrentUser(username=username, name=name, authenticated=True)
    # Заголовка нет → локалка или прямой доступ к :8000 в обход nginx.
    return CurrentUser(username=_DEV_USER, name=_DEV_USER, authenticated=False)
