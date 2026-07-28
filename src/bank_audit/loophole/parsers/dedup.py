"""Ключи дедупликации: источников (targets парсеров) и записей (URL/текст)."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from ...hashing import sha256_text

_TRACKING_PARAMS = {"gclid", "yclid", "fbclid", "_openstat"}
_TG_HANDLE_RE = re.compile(r"^@(?P<name>[A-Za-z][A-Za-z0-9_]{4,31})$")
_WS_RE = re.compile(r"\s+")


def normalize_target(target: str) -> str:
    """Нормализованный ключ источника: netloc + path + query без трекинга.

    Схема отбрасывается (http/https — один источник), host в lowercase без
    «www.», дефолтные порты убраны, trailing slash и fragment убраны,
    utm_*/gclid/yclid/fbclid/_openstat выкинуты из query. Telegram-хендлы
    (@name), ссылки (t.me/name) и bare-формы приводятся к «t.me/<name>».
    Пустой ввод → "".
    """
    t = (target or "").strip().rstrip(".,);]")
    if not t:
        return ""
    m = _TG_HANDLE_RE.match(t)
    if m:
        return f"t.me/{m.group('name').lower()}"
    if "://" not in t:
        t = "https://" + t
    try:
        parts = urlsplit(t)
    except ValueError:
        return t.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("t.me", "telegram.me"):
        path = (parts.path or "").strip("/")
        name = path.split("/")[0] if path else ""
        return f"t.me/{name.lower()}" if name else "t.me"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    path = (parts.path or "").rstrip("/") or "/"
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ))
    return netloc + path + (f"?{query}" if query else "")


def normalize_page_text(text: str | None, fallback: str | None = None) -> str | None:
    """Trim + схлопывание пробельных последовательностей; None если пусто."""
    src = (text or "").strip() or (fallback or "").strip()
    if not src:
        return None
    return _WS_RE.sub(" ", src)


def page_text_sha256(text: str | None, fallback: str | None = None) -> str | None:
    """sha256 нормализованного полного текста страницы; None если текста нет."""
    norm = normalize_page_text(text, fallback)
    return sha256_text(norm) if norm is not None else None
