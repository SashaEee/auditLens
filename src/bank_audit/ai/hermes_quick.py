"""Hermes-движок «Быстрого» режима: агент Nous Hermes вместо single-shot цикла.

Архитектура: ОТДЕЛЬНЫЙ контейнер hermes-al (образ ~/hermes-al на VM, свой дом
/root/.hermes в volume) — никак не связан с личным Hermes владельца. Внутри
агент свободен: shell/python/веб; данные — через alsql (psql-обёртка: SELECT/
INSERT/UPDATE свободно, DROP/DELETE отрезаны), новости — пул daily_digest +
SearXNG, отзывы — локальный API AuditLens. Скиллы и память самообучаются.

Протокол: POST /v1/runs → run_id → GET /v1/runs/{id}/events (SSE) →
транслируем в наш стрим: assistant.delta → text-чанки, tool.started →
tool_call (фронтовые индикаторы), run.completed → done.

Контракт отказа: если Hermes упал ДО первого текст-чанка — поднимаем исключение,
вызывающий (stream_analysis) прозрачно откатывается на нативный quick.
"""
from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)

HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
HERMES_TIMEOUT_S = float(os.getenv("HERMES_TIMEOUT_S", "240"))


class HermesNotStreamed(RuntimeError):
    """Hermes упал до первого текст-чанка — безопасно откатиться на нативный quick."""


def _pick(ev: dict, *paths: str):
    """Достаёт значение по нескольким путям вида 'data.delta' (форма событий
    у Hermes слегка гуляет между версиями — парсим защитно)."""
    for p in paths:
        cur = ev
        ok = True
        for part in p.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


async def stream_quick_hermes(question: str, history: list[dict],
                              session_hint: str | None = None) -> AsyncIterator[str]:
    headers = {"Content-Type": "application/json"}
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"
    if session_hint:
        headers["X-Hermes-Session-Id"] = f"auditlens-{session_hint}"

    body: dict = {"input": question}
    hist = [{"role": m.get("role"), "content": str(m.get("content") or "")}
            for m in (history or [])
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if hist:
        body["conversation_history"] = hist[-8:]

    emitted = False
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(HERMES_TIMEOUT_S, connect=8.0)) as cl:
            r = await cl.post(f"{HERMES_API_URL}/v1/runs", json=body, headers=headers)
            r.raise_for_status()
            rj = r.json()
            run_id = rj.get("run_id") or rj.get("id")
            if not run_id:
                raise HermesNotStreamed(f"no run_id in {str(rj)[:200]}")

            async with cl.stream("GET", f"{HERMES_API_URL}/v1/runs/{run_id}/events",
                                 headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    et = ev.get("event") or ev.get("type") or ""

                    if et in ("assistant.delta", "message.delta", "text.delta"):
                        delta = _pick(ev, "data.delta", "delta", "data.text", "text")
                        if delta:
                            emitted = True
                            yield json.dumps({"type": "text", "chunk": str(delta)},
                                             ensure_ascii=False)
                    elif et == "tool.started":
                        name = _pick(ev, "tool", "data.tool", "data.tool_name",
                                     "tool_name", "data.name", "name")
                        if name:
                            yield json.dumps({"type": "tool_call",
                                              "name": str(name)}, ensure_ascii=False)
                    elif et in ("run.completed",):
                        # финальный текст, если дельты не стримились (короткие ответы)
                        if not emitted:
                            final = _pick(ev, "output", "data.output",
                                          "data.assistant_message.content",
                                          "data.output_text", "data.content", "content")
                            if final:
                                emitted = True
                                yield json.dumps({"type": "text", "chunk": str(final)},
                                                 ensure_ascii=False)
                        yield json.dumps({"type": "done"})
                        return
                    elif et in ("run.failed", "run.cancelled"):
                        err = str(_pick(ev, "data.error", "error") or et)
                        raise RuntimeError(f"hermes run: {err[:200]}")
            # стрим закрылся без run.completed
            if emitted:
                yield json.dumps({"type": "done"})
                return
            raise HermesNotStreamed("event stream ended without output")
    except Exception:
        if emitted:
            raise                       # частичный стрим — наверх, без отката
        raise HermesNotStreamed("hermes unavailable") from None
