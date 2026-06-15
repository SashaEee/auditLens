"""LLM Throttle — обёртка над AsyncOpenAI клиентом с rate-limit защитой.

Phase 2: pipeline делает 30-50 LLM вызовов на один запрос (entity discovery,
topic classify, query plan ×N, core_schema, fact extract ×N, normalize,
outline plan, narrative ×6-8, gap fill ×N). При параллельности > 8 Fireworks
возвращает 429 RATE_LIMIT_EXCEEDED.

Решение:
  • Semaphore (max_concurrent=4)  — ограничивает параллельные in-flight calls
  • Exponential backoff на 429 (с jitter)  — повтор с ростом задержки
  • Не trap всё подряд (только LLM ratelimits, остальные exceptions проходят)
"""
from __future__ import annotations
import asyncio
import logging
import random
import re

log = logging.getLogger(__name__)


# Global semaphore — лимит параллельных in-flight LLM calls
DEFAULT_MAX_CONCURRENT = 4
_global_sem: asyncio.Semaphore | None = None


def get_semaphore(max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> asyncio.Semaphore:
    """Lazy-create global semaphore (одна на event-loop)."""
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(max_concurrent)
    return _global_sem


def reset_semaphore(max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> None:
    """Reset для тестов / новой сессии."""
    global _global_sem
    _global_sem = asyncio.Semaphore(max_concurrent)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Heuristic: это 429 rate limit?"""
    msg = str(exc).lower()
    if "429" in msg:
        return True
    if "rate_limit" in msg or "rate limit" in msg:
        return True
    if "rate-limit" in msg:
        return True
    # OpenAI-style error
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code == 429:
        return True
    return False


_TRANSIENT_PATTERNS = (
    "connection termination", "upstream connect error", "disconnect/reset",
    "before headers", "connection reset", "connection error", "connection aborted",
    "server disconnected", "remoteprotocolerror", "read timeout", "timed out",
    "timeout", "502", "503", "504", "bad gateway", "service unavailable",
    "gateway timeout", "internal server error", "overloaded", "temporarily",
)


def _is_transient_error(exc: Exception) -> bool:
    """Транзиентные сбои сети/шлюза (обрыв соединения, 5xx, таймаут) — их НУЖНО
    ретраить (баг: эндпоинт cloud.ru роняет соединения под нагрузкой агентского
    v2, а throttle раньше ретраил только rate-limit → каскадные потери)."""
    msg = str(exc).lower()
    if any(p in msg for p in _TRANSIENT_PATTERNS):
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code in (408, 500, 502, 503, 504, 520, 522, 524):
        return True
    # httpx/openai сетевые классы по имени типа (без жёсткого импорта)
    tname = type(exc).__name__.lower()
    if any(x in tname for x in ("connect", "timeout", "protocol", "apiconnection")):
        return True
    return False


def _extract_retry_after(exc: Exception) -> float | None:
    """Если provider говорит retry-after — выдернуть."""
    # Many providers put 'retry-after: N' in error message
    m = re.search(r"retry[- _]?after[^\d]{0,5}(\d+)", str(exc), re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def patch_client_throttle(client, max_concurrent: int = DEFAULT_MAX_CONCURRENT):
    """Monkey-patch client.chat.completions.create чтобы добавить throttle.

    После патча все вызовы client.chat.completions.create(...) проходят
    через semaphore + exponential backoff на rate limit.
    """
    reset_semaphore(max_concurrent)
    original_create = client.chat.completions.create

    async def throttled_create(*args, **kwargs):
        return await call_with_throttle(original_create, *args, **kwargs)

    client.chat.completions.create = throttled_create
    log.warning("[llm_throttle] client patched: max_concurrent=%s", max_concurrent)
    return client


async def call_with_throttle(coro_fn, *args, max_retries: int = 5,
                                base_delay: float = 2.0, **kwargs):
    """Выполняет coro_fn(*args, **kwargs) с semaphore + exponential backoff.

    coro_fn — это async callable который возвращает coroutine.
    Пример:
        result = await call_with_throttle(
            client.chat.completions.create,
            model="...", messages=[...]
        )
    """
    sem = get_semaphore()
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        async with sem:
            try:
                return await coro_fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if not (_is_rate_limit_error(e) or _is_transient_error(e)):
                    # Не rate-limit и не транзиент — пробрасываем сразу (caller обработает)
                    raise
                # Rate-limit / транзиентный обрыв: backoff outside semaphore
        # Backoff (за пределами sem чтобы другие могли работать)
        retry_after = _extract_retry_after(last_exc)
        if retry_after is None:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
        else:
            delay = retry_after + random.uniform(0, 0.5)
        delay = min(delay, 30.0)   # cap 30s
        _kind = "rate-limit" if _is_rate_limit_error(last_exc) else "транзиент"
        log.warning("[llm_throttle] %s on attempt %s/%s (%s), sleep %.1fs",
                     _kind, attempt + 1, max_retries, str(last_exc)[:80], delay)
        await asyncio.sleep(delay)

    log.warning("[llm_throttle] all retries exhausted, raising")
    raise last_exc if last_exc else RuntimeError("call_with_throttle: no exception")
