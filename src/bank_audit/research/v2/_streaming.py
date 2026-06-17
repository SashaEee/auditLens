"""Стрим reasoning-токенов наружу (Этап 1 «премиального стриминга»).

Факт (проверено вживую на cloud.ru Foundation Models): finальный content
БУФЕРИЗУЕТСЯ проксей и приходит пачкой — пословный стрим ответа невозможен.
А вот delta.reasoning_content (ход мысли модели) СТРИМИТСЯ инкрементально и
приходит раньше content. Поэтому стримим наружу ТОЛЬКО reasoning; content
собираем целиком (как раньше) — парсинг JSON / чистка цитат не меняются.

Потребление стрима обёрнуто в существующий throttle (semaphore + wall +
backoff), поэтому rate-защита не теряется. См. llm_throttle.patch_client_throttle
(сохраняет client.chat.completions._orig_create).
"""
from __future__ import annotations

import os

from ..llm_throttle import call_with_throttle


def stream_reasoning_enabled() -> bool:
    """Глобальный тумблер фичи. Дефолт ВЫКЛ — безопасный rollout в проде."""
    return os.getenv("V2_STREAM_REASONING", "0").strip().lower() in (
        "1", "true", "yes", "on")


async def stream_completion(client, *, on_reasoning=None, **create_kwargs):
    """stream=True вызов LLM. Возвращает (content, reasoning, tool_calls).

    on_reasoning(chunk) — sync-callback: chunk:str для reasoning-дельты по мере
    прихода; chunk=None — сигнал RESET (стрим ретраится, ранее утёкшее наружу
    невалидно). Обычно кладёт SSE-событие в asyncio.Queue оркестратора. content и
    reasoning собираются целиком; tool_calls аккумулируются по index (на случай
    использования со стрим-tool-calling, хотя Этап 1 этим не пользуется).
    """
    create_kwargs["stream"] = True
    orig = getattr(client.chat.completions, "_orig_create", None)
    if orig is None:
        # Контракт: throttle-патч ДОЛЖЕН быть применён (он сохраняет _orig_create).
        # Тихий fallback на throttled .create дал бы вложенный throttle (asyncio.
        # Semaphore не реентрантна) → self-deadlock + двойной wall. Лучше явно упасть.
        raise RuntimeError("stream_completion: client не пропатчен "
                           "patch_client_throttle (нет _orig_create)")
    content: list[str] = []
    reasoning: list[str] = []
    tcs: dict = {}
    _attempt = {"n": 0}

    async def _consume(**kw):
        # call_with_throttle может ретраить _consume на транзиенте/таймауте.
        _attempt["n"] += 1
        if _attempt["n"] > 1 and on_reasoning:
            # Часть reasoning уже утекла наружу до обрыва — просим потребителя
            # сбросить накопленное (on_reasoning(None) = reset), иначе во фронт-
            # панели задвоится ход мысли.
            try:
                on_reasoning(None)
            except Exception:
                pass
        # Чистим аккумуляторы на каждой попытке, иначе данные задвоятся.
        content.clear(); reasoning.clear(); tcs.clear()
        stream = await orig(**kw)
        async for ch in stream:
            if not getattr(ch, "choices", None):
                continue
            d = ch.choices[0].delta
            if d is None:
                continue
            # reasoning_content — нестандартное поле OpenAI SDK: берём через
            # getattr / model_extra, чтобы не падать на моделях без него.
            rc = getattr(d, "reasoning_content", None)
            if rc is None:
                ex = getattr(d, "model_extra", None) or {}
                rc = ex.get("reasoning_content") or getattr(d, "reasoning", None)
            if rc:
                reasoning.append(rc)
                if on_reasoning:
                    try:
                        on_reasoning(rc)
                    except Exception:
                        pass  # проблема доставки наружу НЕ должна рушить вызов
            c = getattr(d, "content", None)
            if c:
                content.append(c)
            for tc in (getattr(d, "tool_calls", None) or []):
                e = tcs.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    e["id"] = tc.id
                if tc.function and tc.function.name:
                    e["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    e["arguments"] += tc.function.arguments
        return None

    # call_with_throttle сам обернёт _consume в semaphore + asyncio.wait_for со
    # стеной _wall_for(create_kwargs) + backoff на rate-limit/транзиенте.
    await call_with_throttle(_consume, **create_kwargs)
    return "".join(content), "".join(reasoning), list(tcs.values())
