"""Быстрый режим ИИ-аналитика: агент Hermes (контейнер hermes-al).

Hermes — харнесс агента: цикл с инструментами, навыки, память, самообучение.
Что он умеет в AuditLens, задают не этот модуль, а его настройки
(deploy/hermes-al): SOUL.md — кто он и как отвечает, навыки auditlens-* —
методика по областям, MCP-сервер AuditLens (ai/mcp_server.py) — данные теми же
функциями, что рисуют вкладки. Этот модуль только проводит вопрос туда и ответ
обратно в чат.

Протокол: POST /v1/runs → run_id → GET /v1/runs/{id}/events (SSE).
  assistant.delta → текст, tool.started → шаг в интерфейсе,
  run.completed → финал (поле output — авторитетный текст ответа).

Что делаем на своей стороне и почему:
  • Контекст запроса — в штатное поле instructions (системное сообщение
    прогона), а не приклеиванием правил к вопросу.
  • Текст, написанный ДО очередного вызова инструмента («Сейчас посмотрю…»),
    — рассуждение вслух, а не ответ: придерживаем начало ответа и сбрасываем
    его, если за ним последовал инструмент.
  • Пустой финал или заглушка («Давай разберусь детальнее.» — так кончался
    бюджет шагов) — одна повторная попытка, затем откат на нативный цикл.
  • Ссылки на внутренние адреса (127.0.0.1) у пользователя не откроются —
    переписываем на страницы AuditLens или убираем.
  • Пользователь ушёл — останавливаем прогон, чтобы агент не работал впустую.

Контракт отказа: HermesNotStreamed — Hermes не дал ни слова ответа; вызывающий
(stream_analysis) откатывается на нативный быстрый цикл.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import time
from typing import AsyncIterator
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
# Предел всего прогона. Агенту дано до 40 шагов; типовой ответ — 20–60 с.
HERMES_TIMEOUT_S = float(os.getenv("HERMES_TIMEOUT_S", "360"))
# Повторять прогон с пустым финалом, только если с начала прошло меньше этого.
HERMES_RETRY_BEFORE_S = float(os.getenv("HERMES_RETRY_BEFORE_S", "150"))
# Маршрут модели (model_routes в конфиге Hermes); пусто — модель по умолчанию.
HERMES_MODEL = os.getenv("HERMES_MODEL", "")
# Сколько знаков ответа придержать, прежде чем показывать: короткий текст
# перед вызовом инструмента — рассуждение, его не показываем.
HOLD_CHARS = int(os.getenv("HERMES_HOLD_CHARS", "240"))

MSK = ZoneInfo("Europe/Moscow")


class HermesNotStreamed(RuntimeError):
    """Hermes не дал ни слова ответа — безопасно откатиться на нативный цикл."""


def _pick(ev: dict, *paths: str):
    """Значение по одному из путей вида 'data.delta' (форма событий Hermes
    немного разная между версиями — разбираем защитно)."""
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


# ── подписи шагов ────────────────────────────────────────────────────────────

_BUILTIN_LABELS = {
    "terminal": "Терминал", "process": "Терминал", "execute_code": "Код",
    "skill_view": "Навык", "skills_list": "Навыки", "skill_manage": "Обучение",
    "memory": "Память", "session_search": "История диалогов", "todo": "План",
    "web_search": "Веб-поиск", "web_extract": "Чтение страницы",
    "read_file": "Файл", "write_file": "Файл", "patch": "Файл",
    "search_files": "Поиск по файлам", "delegate_task": "Помощник",
    "vision_analyze": "Картинка",
}


_SKILL_LABELS = {
    "auditlens-complaints": "жалобы", "auditlens-market": "рынок",
    "auditlens-research": "документы и новости", "auditlens-data": "база данных",
    "auditlens-loopholes": "уязвимости",
}


def tool_label(name: str, preview: str | None = None) -> str:
    """Подпись шага. Для навыка — какой именно («Навык: жалобы»): четыре
    одинаковых «Навык» подряд ничего не говорят пользователю."""
    from .agent_tools import label_for
    if re.match(r"mcp_+auditlens_+", name):
        return label_for(name)
    if name.startswith("browser_"):
        return "Браузер"
    base = _BUILTIN_LABELS.get(name, name)
    if name == "skill_view" and preview:
        m = re.search(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", preview)
        if m:
            return f"{base}: {_SKILL_LABELS.get(m.group(0), m.group(0))}"
    return base


# ── чистка текста ────────────────────────────────────────────────────────────

_LOCAL = r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?"
_LOCAL_PAGE_LINK = re.compile(r"\]\(" + _LOCAL + r"/?(#[a-z]+[^)\s]*)\)")
_LOCAL_LINK = re.compile(r"\[([^\]]+)\]\(" + _LOCAL + r"[^)\s]*\)")
_LOCAL_BARE = re.compile(r"`?" + _LOCAL + r"[^\s)`]*`?")


# Артефакты цитирования некоторых моделей: 【582†fragments】, 【…】
_CITE_ART = re.compile(r"【[^】]{0,80}】")
_LINK_TARGET = re.compile(r"(\]\([^)\s]*\))")
_TOOL_NAME = re.compile(r"`?mcp_+auditlens_+[a-z_]+`?")


def _theme_words() -> dict[str, str]:
    """Служебный ключ темы → русская подпись (chargeback → «Чарджбэк»)."""
    from ..rag import review_codebook as cb
    return {k: v[1] for k, v in cb.ISSUES.items() if "_" in k or k == "chargeback"}


_THEME_RX = None


def _plain_keys(text: str) -> str:
    """Ключи тем в тексте — на подписи; адреса ссылок (#reviews?theme=…) не трогаем."""
    global _THEME_RX
    words = _theme_words()
    if _THEME_RX is None:
        _THEME_RX = re.compile(r"`?\b(" + "|".join(sorted(words, key=len, reverse=True))
                               + r")\b`?", re.I)
    parts = _LINK_TARGET.split(text)
    for i in range(0, len(parts), 2):          # нечётные — адреса ссылок
        parts[i] = _THEME_RX.sub(lambda m: words.get(m.group(1).lower(), m.group(1)), parts[i])
    return "".join(parts)


plain_keys = _plain_keys


class PlainKeysStream:
    """_plain_keys для текста, идущего кусками (отчёт): ключ может разрезаться
    между кусками («charge|back»), ссылка — на адресе («](#reviews?theme=ch|…»).
    Придерживаем хвост из латиницы и незакрытый адрес ссылки до следующего
    куска; остальное чистим и отдаём сразу."""

    _TAIL = re.compile(r"[A-Za-z_]+$")

    def __init__(self):
        self._buf = ""

    def feed(self, chunk: str) -> str:
        buf = self._buf + (chunk or "")
        cut = len(buf)
        link = buf.rfind("](")
        if link >= 0 and ")" not in buf[link + 2:] and len(buf) - link < 400:
            cut = link
        else:
            m = self._TAIL.search(buf)
            if m:
                cut = m.start()
        self._buf = buf[cut:]
        return _plain_keys(buf[:cut]) if cut else ""

    def finish(self) -> str:
        out, self._buf = self._buf, ""
        return _plain_keys(out) if out else ""


# Голые адреса: страница AuditLens (#reviews?theme=…) и http-ссылка вне markdown.
# Рендер чата делает кликабельными только [текст](адрес), а чистка ключей тем
# испортила бы голый адрес (theme=chargeback → theme=Чарджбэк — пустой фильтр).
_APP_PAGES = "overview|foryou|reviews|market|banks|knowledge|sources|loophole"
_BARE_APP = re.compile(r"(?<![\w(\[/=])`?(#(?:" + _APP_PAGES + r")\?[^\s)\]`]+)`?")
_BARE_URL = re.compile(r"(?<![(\[\w])(https?://[^\s)\]<>`]+?)(?=[.,;:!?»]*(?:\s|$|\)))")


def _linkify(text: str) -> str:
    from urllib.parse import urlparse

    def url(m):
        u = m.group(1)
        host = urlparse(u).netloc.removeprefix("www.") or u
        return f"[{host}]({u})"
    text = _BARE_APP.sub(lambda m: f"[открыть в AuditLens]({m.group(1)})", text)
    return _BARE_URL.sub(url, text)


def sanitize(text: str) -> str:
    """Чистка ответа перед показом: внутренние адреса → страницы AuditLens или
    текст; голые адреса → ссылки; служебные ключи тем → русские подписи (адреса
    ссылок не трогаем); артефакты цитирования — вон."""
    text = _LOCAL_PAGE_LINK.sub(r"](\1)", text)
    text = _LOCAL_LINK.sub(r"\1", text)
    text = _LOCAL_BARE.sub("", text)
    text = _linkify(text)
    text = _CITE_ART.sub("", text)
    text = _TOOL_NAME.sub(lambda m: "«" + tool_label(m.group(0).strip("`")) + "»", text)
    return _plain_keys(text)


def safe_cut(buf: str) -> int:
    """Докуда можно отдать текст, не разорвав ссылку: до последнего пробела и
    не дальше начала незакрытой markdown-ссылки."""
    cut = max(buf.rfind(" "), buf.rfind("\n")) + 1
    open_br = buf.rfind("[", 0, cut)
    if open_br >= 0:
        rest = buf[open_br:]
        closed = re.match(r"\[[^\]]*\]\([^)]*\)", rest)
        if not closed and ("](" in rest or "]" not in rest):
            cut = min(cut, open_br)
    return max(cut, 0)


_STUB_RX = re.compile(
    r"^\W*(давай(те)?\s+(разбер|посмотр|провер)|сейчас\s+(посмотр|провер|загруж|найд)|"
    r"(одну\s+)?минут|я\s+(посмотрю|проверю|разберусь)|let me|i reached the iteration limit|"
    r"i couldn'?t generate|nothing to report)", re.I)


def is_stub(text: str) -> bool:
    """Ответ-заглушка: пусто или короткая фраза «сейчас разберусь»."""
    t = (text or "").strip()
    if not t:
        return True
    return len(t) < 160 and bool(_STUB_RX.search(t))


# ── нормативные акты ─────────────────────────────────────────────────────────

_ACT_RE = re.compile(r"(№\s?\d+[-\w]*|\b\d+[-‑]?ФЗ\b|\bФЗ[-\s]?№?\s?\d+)", re.IGNORECASE)
_LEGAL_WORD_RE = re.compile(
    r"закон|постановлен|приказ|положени|указани|инструкци|регламент|кодекс|"
    r"\bФЗ\b|\bст\.\s?\d|стать[еёяиую]", re.IGNORECASE)
LEGAL_NOTE = ("\n\n> ⚠ **Ссылки на нормативные акты проверьте по первоисточнику.** "
              "Быстрый режим может называть номер и дату акта по памяти — "
              "полный текст и действующую редакцию смотрите на pravo.gov.ru "
              "или в правовой системе.\n")


def needs_legal_note(text: str) -> bool:
    """Есть ли в ответе ссылка на НПА, которую аудитор пойдёт проверять
    (однажды агент назвал несуществующий «приказ № 117-Э»)."""
    if not text or not _ACT_RE.search(text):
        return False
    if re.search(r"\]\(https?://", text):
        return False            # акты названы со ссылками на источник — проверяемо
    return bool(_LEGAL_WORD_RE.search(text))


# ── прогон ───────────────────────────────────────────────────────────────────

def instructions(retry: bool = False) -> str:
    now = _dt.datetime.now(MSK)
    s = (f"Сегодня {now:%d.%m.%Y}, {now:%H:%M} МСК. Вопрос задан в чате ИИ-аналитика "
         "AuditLens; ответ увидит аудитор в этом же чате (markdown).")
    if retry:
        s += (" Предыдущая попытка закончилась без ответа. Ответь по данным, которые "
              "дают инструменты AuditLens, за несколько шагов; чего не хватило — "
              "назови одной строкой.")
    return s


def _headers(session_hint: str | None) -> dict:
    h = {"Content-Type": "application/json"}
    if HERMES_API_KEY:
        h["Authorization"] = f"Bearer {HERMES_API_KEY}"
    if session_hint:
        h["X-Hermes-Session-Id"] = f"auditlens-{session_hint}"
    return h


async def _stop(run_id: str, headers: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as cl:
            await cl.post(f"{HERMES_API_URL}/v1/runs/{run_id}/stop", headers=headers)
    except Exception:  # noqa: BLE001 — остановка best-effort
        pass


async def _one_run(question: str, history: list[dict], headers: dict, model: str | None,
                   retry: bool, deadline: float, trace: dict) -> AsyncIterator[dict]:
    """Один прогон Hermes. Отдаёт события: {"tool": name} | {"delta": text} |
    {"final": text, "usage": …}. Останавливает прогон, если его бросили."""
    body: dict = {"input": question, "instructions": instructions(retry)}
    if model:
        body["model"] = model
    hist = [{"role": m.get("role"), "content": str(m.get("content") or "")}
            for m in (history or [])
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if hist:
        body["conversation_history"] = hist[-8:]
    run_id = None
    finished = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HERMES_TIMEOUT_S, connect=8.0)) as cl:
            r = await cl.post(f"{HERMES_API_URL}/v1/runs", json=body, headers=headers)
            r.raise_for_status()
            rj = r.json()
            run_id = rj.get("run_id") or rj.get("id")
            if not run_id:
                raise HermesNotStreamed(f"нет run_id: {str(rj)[:200]}")
            trace.setdefault("run_ids", []).append(run_id)
            async with cl.stream("GET", f"{HERMES_API_URL}/v1/runs/{run_id}/events",
                                 headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if time.monotonic() > deadline:
                        raise asyncio.TimeoutError("прогон дольше предела")
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
                        d = _pick(ev, "data.delta", "delta", "data.text", "text")
                        if d:
                            yield {"delta": str(d)}
                    elif et == "tool.started":
                        name = _pick(ev, "tool", "data.tool", "data.tool_name", "tool_name",
                                     "data.name", "name")
                        if name:
                            yield {"tool": str(name),
                                   "preview": str(_pick(ev, "preview", "data.preview") or "")}
                    elif et == "tool.completed" and _pick(ev, "error", "data.error") is True:
                        yield {"tool_error": str(_pick(ev, "tool", "data.tool") or "")}
                    elif et == "run.completed":
                        finished = True
                        yield {"final": str(_pick(ev, "output", "data.output",
                                                  "data.assistant_message.content",
                                                  "data.output_text", "data.content",
                                                  "content") or ""),
                               "usage": _pick(ev, "usage", "data.usage")}
                        return
                    elif et in ("run.failed", "run.cancelled"):
                        finished = True
                        raise RuntimeError(f"hermes: {str(_pick(ev, 'data.error', 'error') or et)[:200]}")
        raise HermesNotStreamed("поток событий закрылся без run.completed")
    finally:
        if run_id and not finished:
            # пользователь ушёл, таймаут или сбой — агент не должен работать впустую
            asyncio.ensure_future(_stop(run_id, headers))


async def stream_quick_hermes(question: str, history: list[dict],
                              session_hint: str | None = None,
                              model: str | None = None) -> AsyncIterator[str]:
    headers = _headers(session_hint)
    model = model or HERMES_MODEL or None
    t0 = time.monotonic()
    deadline = t0 + HERMES_TIMEOUT_S
    trace: dict = {"model": model or "default", "tools": []}
    shown = False            # что-то из ответа уже отдано пользователю
    said: list[str] = []     # отданный текст — для проверки ссылок на НПА
    pending = ""             # придержанное начало ответа / хвост до безопасного разреза

    def emit(text: str) -> str:
        said.append(text)
        return json.dumps({"type": "text", "chunk": text}, ensure_ascii=False)

    usage = None
    for attempt in (0, 1):
        final = None
        broken = False
        try:
            async for ev in _one_run(question, history, headers, model, attempt == 1,
                                     deadline, trace):
                if "tool_error" in ev:
                    trace.setdefault("tool_errors", []).append(ev["tool_error"])
                elif "tool" in ev:
                    name = ev["tool"]
                    trace["tools"].append(name)
                    if not shown:
                        pending = ""          # текст до инструмента — рассуждение вслух
                    yield json.dumps({"type": "tool_call",
                                      "name": tool_label(name, ev.get("preview"))},
                                     ensure_ascii=False)
                elif "delta" in ev:
                    pending += ev["delta"]
                    if not shown and len(pending) < HOLD_CHARS:
                        continue
                    cut = safe_cut(pending)
                    if cut:
                        shown = True
                        yield emit(sanitize(pending[:cut]))
                        pending = pending[cut:]
                elif "final" in ev:
                    final, usage = ev["final"], ev.get("usage")
        except (HermesNotStreamed, asyncio.TimeoutError) as e:
            log.warning("[hermes] прогон прерван: %s", e)
            if not shown:
                raise HermesNotStreamed(str(e) or "таймаут прогона") from None
            broken = True
        except Exception:
            if not shown:
                raise HermesNotStreamed("hermes недоступен") from None
            raise                       # частичный стрим — наверх, без отката

        if shown:
            if pending:                 # хвост после последнего безопасного разреза
                yield emit(sanitize(pending))
                pending = ""
            if broken:
                yield emit("\n\n⚠ _Агент прервался, ответ может быть неполным._")
                trace["broken"] = True
            break
        text = (final if final is not None and final.strip() else pending).strip()
        if not is_stub(text):
            shown = True
            yield emit(sanitize(text))
            break
        elapsed = time.monotonic() - t0
        log.warning("[hermes] попытка %d без ответа (%r, %.0f с)", attempt + 1, text[:80],
                    elapsed)
        trace["empty_attempts"] = attempt + 1
        if attempt == 0 and elapsed < HERMES_RETRY_BEFORE_S:
            pending = ""
            continue
        raise HermesNotStreamed("hermes завершил прогон без ответа")

    if shown and needs_legal_note("".join(said)):
        yield emit(LEGAL_NOTE)
    trace["seconds"] = round(time.monotonic() - t0, 1)
    if usage:
        trace["usage"] = usage
    yield json.dumps({"type": "run_meta", **trace}, ensure_ascii=False)
    yield json.dumps({"type": "done"})
