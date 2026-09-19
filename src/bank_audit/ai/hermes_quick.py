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
import re
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


# Правила работы для быстрого режима. Своего системного слота у API движка нет
# (POST /v1/runs принимает только input и историю), поэтому передаём их вместе с
# заданием. Причина: на вопрос о требованиях по образовательному кредиту агент
# не открыл НИ ОДНОГО источника и сочинил ответ по памяти — с несуществующей
# «иноагентской образовательной программой» и наугад названным номером закона.
# Аудитор такую выдачу проверить не может, а именно на это и жаловались.
QUICK_RULES = """
---
Правила ответа (внутренний инструмент аудита Сбербанка):
1. Не отвечай по памяти. Каждое число и утверждение — из проверенного источника.
   Порядок обращения: сначала собственные данные (витрина, база знаний, отзывы),
   и только если без внешней страницы никак — веб. Это БЫСТРЫЙ режим: на веб
   трать не больше двух-трёх попыток, а если страницы не открываются — не
   продолжай их перебирать, а ответь тем, что есть, и честно скажи, чего не
   хватило. Если источника не нашлось вовсе: «по доступным источникам данных нет».
2. Нормы права проверяй по документам, а не по памяти. Номер акта, статью и дату
   приводи как установленный факт только если видел их в источнике. Если номер
   всплыл по памяти — можно назвать, но обязательно с пометкой «по памяти, не
   подтверждено источником»: аудитор пойдёт проверять, и непомеченная ссылка
   обойдётся дороже неточной.
3. Различай «мы этого не собираем» и «этого нет у банка». Пустая выборка — не
   доказательство отсутствия продукта.
4. Точка зрения — аудитор Сбера: другие банки это бенчмарк, а не выбор клиента.
5. Ответ обязателен всегда. Если источников мало — напиши, что удалось
   установить, чего не хватает и где это искать. Молчание недопустимо: пустой
   ответ для аудитора хуже неполного.
"""


# Ссылка на нормативный акт: номер закона, приказа, положения, постановления.
_ACT_RE = re.compile(
    r"(№\s?\d+[-\w]*|\b\d+[-‑]?ФЗ\b|\bФЗ[-\s]?№?\s?\d+)", re.IGNORECASE)
_LEGAL_WORD_RE = re.compile(
    r"закон|постановлен|приказ|положени|указани|инструкци|регламент|кодекс|"
    r"\bФЗ\b|\bст\.\s?\d|стать[еёяиую]", re.IGNORECASE)
LEGAL_NOTE = ("\n\n> ⚠ **Ссылки на нормативные акты проверьте по первоисточнику.** "
              "Быстрый режим может называть номер и дату акта по памяти — "
              "полный текст и действующую редакцию смотрите на pravo.gov.ru "
              "или в правовой системе.\n")


def needs_legal_note(text: str) -> bool:
    """Есть ли в ответе ссылка на НПА, которую аудитор пойдёт проверять.

    Промптом это не лечится до конца: модель быстрого режима называет номера
    актов по памяти даже там, где источник не открывался, — в проверке всплыл
    несуществующий «приказ № 117-Э». Молча отдавать такую ссылку нельзя:
    аудитор сошлётся на неё в работе.
    """
    if not text or not _ACT_RE.search(text):
        return False
    return bool(_LEGAL_WORD_RE.search(text))


def _with_rules(question: str) -> str:
    """Вопрос + правила. Отключается QUICK_RULES=off."""
    if os.getenv("QUICK_RULES", "on").lower() in ("off", "0", "none"):
        return question
    return f"{question}\n{QUICK_RULES}"


async def stream_quick_hermes(question: str, history: list[dict],
                              session_hint: str | None = None) -> AsyncIterator[str]:
    headers = {"Content-Type": "application/json"}
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"
    if session_hint:
        headers["X-Hermes-Session-Id"] = f"auditlens-{session_hint}"

    body: dict = {"input": _with_rules(question)}
    hist = [{"role": m.get("role"), "content": str(m.get("content") or "")}
            for m in (history or [])
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if hist:
        body["conversation_history"] = hist[-8:]

    emitted = False
    said: list[str] = []       # накопленный ответ — для проверки ссылок на НПА
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
                            said.append(str(delta))
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
                                said.append(str(final))
                                yield json.dumps({"type": "text", "chunk": str(final)},
                                                 ensure_ascii=False)
                        if emitted and needs_legal_note("".join(said)):
                            yield json.dumps({"type": "text", "chunk": LEGAL_NOTE},
                                             ensure_ascii=False)
                        if not emitted:
                            # Прогон завершился штатно, но БЕЗ единого слова.
                            # Так бывает, когда агент израсходовал бюджет ходов
                            # на инструменты (в логе движка: api_calls=14/14,
                            # last_msg_role=tool, response_len=0) — например,
                            # на браузерных попытках, падающих по таймауту.
                            # Раньше мы просто слали «готово», и аудитор видел
                            # пустой ответ. Откатываемся на собственный цикл:
                            # текста наружу ещё не ушло, откат безопасен.
                            raise HermesNotStreamed(
                                "hermes завершил прогон без ответа (бюджет ходов "
                                "израсходован на инструменты)")
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
