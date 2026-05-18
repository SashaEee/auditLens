"""TTL-кэш на Postgres-таблице rag_cache.

Используется для:
  • fetch         — кэш raw HTML/JSON ответов (TTL 1-24h)
  • answer        — синтез RAG-ответа (TTL 30min — пользователь может уточнить вопрос)
  • search        — результат vector search для частых запросов (TTL 6h)

Минималистичный API. Не Redis — просто SQL.
"""
from __future__ import annotations
import hashlib, json, logging
from datetime import datetime, timezone, timedelta
from typing import Any
from sqlalchemy import text
from .. import db

log = logging.getLogger(__name__)


def _make_key(namespace: str, *parts: Any) -> str:
    """Стабильный детерминированный ключ из произвольных аргументов."""
    s = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{h}"


def get(namespace: str, *parts: Any) -> Any | None:
    """Возвращает кэшированное значение или None если истёк/нет."""
    key = _make_key(namespace, *parts)
    with db.session() as s:
        row = s.execute(text("""
            SELECT value FROM rag_cache
             WHERE cache_key = :k AND expires_at > now()
        """), {"k": key}).first()
    return row[0] if row else None


def put(namespace: str, value: Any, ttl_seconds: int, *parts: Any) -> None:
    """Записывает value в кэш с TTL."""
    key = _make_key(namespace, *parts)
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    with db.session() as s:
        s.execute(text("""
            INSERT INTO rag_cache(cache_key, namespace, value, expires_at)
            VALUES (:k, :ns, CAST(:v AS jsonb), :ex)
            ON CONFLICT (cache_key) DO UPDATE
              SET value = EXCLUDED.value,
                  expires_at = EXCLUDED.expires_at,
                  created_at = now()
        """), {"k": key, "ns": namespace,
               "v": json.dumps(value, ensure_ascii=False, default=str),
               "ex": expires})


def delete(namespace: str, *parts: Any) -> None:
    """Принудительная инвалидация."""
    key = _make_key(namespace, *parts)
    with db.session() as s:
        s.execute(text("DELETE FROM rag_cache WHERE cache_key=:k"), {"k": key})


def cleanup_expired() -> int:
    """Очистка просроченных записей. Вызывается фоновым job.
    Возвращает count удалённых."""
    with db.session() as s:
        n = s.execute(text("""
            DELETE FROM rag_cache WHERE expires_at < now() RETURNING 1
        """)).rowcount
    if n:
        log.info("rag_cache: cleaned %s expired entries", n)
    return n or 0


# Контекст-менеджер для удобного use-pattern: cached(ns, ttl, *parts) → fn
def cached(namespace: str, ttl_seconds: int, *parts: Any):
    """Декоратор-фабрика. Использование:

        @cached('fetch', 3600, url, headers_hash)
        def do_fetch(url, headers_hash):
            return real_fetch(url)

    Не самый идиоматичный API, для одиночных вызовов лучше get/put напрямую.
    """
    def deco(fn):
        def wrapper(*args, **kwargs):
            cv = get(namespace, *parts)
            if cv is not None:
                return cv
            v = fn(*args, **kwargs)
            put(namespace, v, ttl_seconds, *parts)
            return v
        return wrapper
    return deco
