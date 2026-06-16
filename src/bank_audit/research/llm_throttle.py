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
import os
import random
import re

log = logging.getLogger(__name__)


# Wall-clock лимит на ОДИН LLM-вызов. Профиль показал: реальная работа быстрая
# (1-3с), а 5-10-минутные простои — это медленный стрим тяжёлого вызова, который
# httpx-таймаут (поштучный на чанк) не ловит. Обрываем по wall-clock и ретраим в,
# возможно, здоровое окно. Не режет работу — такой вызов всё равно не завершился бы
# осмысленно. Тюнится LLM_CALL_WALL_S (легит-вызовы: extract ~3с, reasoning ~40с).
_WALL_S = float(os.getenv("LLM_CALL_WALL_S", "75"))

# Дифференцированная стена по ВЕСУ вызова (главная утечка времени: в «плохом окне»
# эндпоинта вызов медленно стримится, бьётся о стену и ретраится — до 5×75с=375с
# мёртвого ожидания на ОДИН логический вызов).
#   • Навигационные вызовы агента (tools+tool_choice, без большого max_tokens) —
#     короткие по природе (решение «какой tool позвать»). Короткая стена → в плохом
#     окне бросаем быстро и ретраим, не копим мёртвое время. Качество не страдает:
#     осмысленный навигационный вызов укладывается в секунды.
#   • Тяжёлые вызовы (max_tokens≥6000: conductor-план, отчёт аналитика, финальная
#     экстракция фактов) реально стримят 6-8k токенов — в медленном окне это может
#     САМО превысить 75с. Даём БОЛЬШЕ времени, чтобы дошли, а не убивались на 75с и
#     рестартовали с нуля (повторный стрим 8k токенов — хуже, чем дождаться).
_WALL_LOOP_S  = float(os.getenv("LLM_CALL_WALL_LOOP_S",  "60"))
_WALL_HEAVY_S = float(os.getenv("LLM_CALL_WALL_HEAVY_S", "120"))

# Пер-таймаутный лимит ретраев. Транзиент (обрыв соединения) флапает быстро — его
# ретраим щедро. А чистый wall-таймаут стоит ЦЕЛУЮ стену: после 3 таймаутов окно
# явно плохое, дальнейшие ретраи жгут минуты почти без шанса. Качество-нейтрально:
# вызов, упавший по таймауту 3×, почти наверняка упал бы и 4-5×.
_TIMEOUT_MAX_RETRIES = int(os.getenv("LLM_TIMEOUT_MAX_RETRIES", "2"))
# Для ТЯЖЁЛЫХ вызовов (финальная экстракция/отчёт, max_tokens≥6000) ретрай почти
# бесполезен: на медленном окне эндпоинт заново стримит те же 8000 токенов и снова
# упирается в 120с-стену. 3×120=360с мёртвого времени на ОДИН агент. Поэтому тяжёлым
# даём меньше попыток — после них caller фоллбэчит на готовый контент loop-модели.
_TIMEOUT_MAX_RETRIES_HEAVY = int(os.getenv("LLM_TIMEOUT_MAX_RETRIES_HEAVY", "2"))


def _wall_for(kwargs: dict) -> float:
    """Стена под конкретный вызов: короткая для навигации, длинная для тяжёлого
    вывода, дефолт для остального (critic ~1500 ток. и т.п.)."""
    mt = kwargs.get("max_tokens") or 0
    if kwargs.get("tools") and kwargs.get("tool_choice") and mt == 0:
        return _WALL_LOOP_S
    if mt >= 6000:
        return _WALL_HEAVY_S
    return _WALL_S

# Global semaphore — лимит параллельных in-flight LLM calls. Был 4: при 4
# параллельных агентах wave1 пул забивался ими, и tool-итерации/финалы внутри
# агентов сериализовались глобально. 8 — потолок до 429 (env-настраиваемо).
DEFAULT_MAX_CONCURRENT = int(os.getenv("LLM_MAX_CONCURRENT", "8"))
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
    wall = _wall_for(kwargs)          # стена под вес ЭТОГО вызова
    # Тяжёлым стрим-вызовам — меньше таймаут-ретраев (перестрим бесполезен).
    timeout_cap = (_TIMEOUT_MAX_RETRIES_HEAVY if wall >= _WALL_HEAVY_S
                   else _TIMEOUT_MAX_RETRIES)
    timeout_count = 0

    for attempt in range(max_retries):
        async with sem:
            try:
                # Wall-clock лимит на вызов: обрывает медленный стрим, который иначе
                # ползёт минутами (httpx read-timeout его не ловит). Таймаут → ретрай.
                return await asyncio.wait_for(coro_fn(*args, **kwargs), timeout=wall)
            except Exception as e:
                last_exc = e
                is_timeout = isinstance(e, asyncio.TimeoutError)
                if not (is_timeout or _is_rate_limit_error(e) or _is_transient_error(e)):
                    # Не rate-limit/транзиент/таймаут — пробрасываем сразу (caller обработает)
                    raise
                if is_timeout:
                    timeout_count += 1
                    if timeout_count >= timeout_cap:
                        # Окно эндпоинта стабильно плохое — стоп, не жжём ещё одну стену.
                        log.warning("[llm_throttle] %s wall-таймаутов (wall=%.0fs) — "
                                     "стоп ретраев, отдаём ошибку наверх", timeout_count, wall)
                        raise
                # Rate-limit / транзиент / wall-таймаут: backoff outside semaphore
        # Backoff (за пределами sem чтобы другие могли работать)
        if _is_rate_limit_error(last_exc):
            # Rate-limit реально требует подождать — уважаем retry-after / экспоненту.
            retry_after = _extract_retry_after(last_exc)
            delay = (retry_after if retry_after is not None
                     else base_delay * (2 ** attempt)) + random.uniform(0, 0.5)
            delay = min(delay, 30.0)
            kind = "rate-limit"
        else:
            # Обрыв соединения / таймаут флапают БЫСТРО — ретраим почти сразу, не жжём
            # минуты в экспоненциальном backoff (это и была главная утечка времени).
            delay = 1.0 + random.uniform(0, 1.0)
            kind = "транзиент/таймаут"
        log.warning("[llm_throttle] %s attempt %s/%s (%s), sleep %.1fs",
                     kind, attempt + 1, max_retries, str(last_exc)[:80] or type(last_exc).__name__, delay)
        await asyncio.sleep(delay)

    log.warning("[llm_throttle] all retries exhausted, raising")
    raise last_exc if last_exc else RuntimeError("call_with_throttle: no exception")
