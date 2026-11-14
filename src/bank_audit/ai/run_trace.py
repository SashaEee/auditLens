"""Лента шагов прогона ИИ-помощника — «как получен ответ».

Пока ответ готовится, интерфейс показывает ход работы по событиям потока. Чтобы
этот ход не пропадал после ответа (и был виден при открытии разговора из
истории), сервер собирает из тех же событий короткую ленту: что произошло и на
какой секунде, — и сводку прогона отчёта: что вошло в план, сколько прочитано,
сколько фактов выписано. Лента кладётся в meta сообщения и в payload отчёта.

Интерфейс собирает такую же ленту на лету (app.jsx, traceAdd) — правила
склейки здесь и там одни и те же.
"""
from __future__ import annotations

import re
from typing import Callable

MAX_STEPS = 120          # длиннее лента не нужна: дальше идут повторы
ROUTE_LABELS = {
    "report": "Вопрос требует сбора источников — собираю отчёт",
    "reply": "Отвечаю по уже собранному, без нового сбора",
    "deck": "Собираю презентацию по готовому отчёту",
    "answer": "Отвечаю короткой справкой",
}
_NUM = re.compile(r"\d+")


def _clean(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def add(trace: list[dict], t: float, kind: str, label: str, detail: str = "") -> None:
    """Добавить шаг. Повтор того же шага не множит строки: у инструмента растёт
    счётчик («Сюжеты жалоб ×2»), а подпись со счётчиком внутри («Рисую карточки:
    осталось 3») обновляется на месте."""
    label = _clean(label, 200)
    if not label:
        return
    detail = _clean(detail, 240)
    last = trace[-1] if trace else None
    if last and last["k"] == kind and last["label"] == label:
        if kind == "tool":
            last["n"] = last.get("n", 1) + 1
        return
    if (last and kind == "stage" and last["k"] == "stage" and _NUM.search(label)
            and _NUM.sub("#", last["label"]) == _NUM.sub("#", label)):
        last["label"] = label
        if detail:
            last["detail"] = detail
        else:
            last.pop("detail", None)
        return
    if len(trace) >= MAX_STEPS:
        return
    step = {"t": int(max(0, round(t))), "k": kind, "label": label}
    if detail:
        step["detail"] = detail
    trace.append(step)


class Recorder:
    """Собирает ленту шагов и сводку прогона из событий потока ответа.

    clock — секунды от начала прогона. feed() получает каждое событие потока,
    finish() закрывает ленту: готово или оборвано (остановка, обрыв связи)."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self.trace: list[dict] = []
        self.run: dict = {}
        self.phase: str | None = None
        self.mode: str | None = None
        self.route: str | None = None
        self.engine: str | None = None
        self._wrote = False
        self._closed = False

    def feed(self, data: dict) -> None:
        t = data.get("type")
        now = self._clock()
        if t == "route":
            self.route = data.get("value")
            add(self.trace, now, "route", ROUTE_LABELS.get(self.route or "", ROUTE_LABELS["answer"]))
        elif t == "mode":
            self.mode = data.get("value")
        elif t == "engine":
            self.engine = data.get("value")
        elif t == "phase" and isinstance(data.get("value"), str):
            self.phase = data["value"]
            add(self.trace, now, "phase", self.phase)
        elif t == "stage_status":
            # повтор подписи с секундами (progress_elapsed) — не новый шаг
            if data.get("progress_elapsed") is None and data.get("label"):
                add(self.trace, now, "stage", data["label"], data.get("detail") or "")
        elif t == "tool_call" and data.get("name"):
            add(self.trace, now, "tool", str(data["name"]))
        elif t == "text" and data.get("chunk"):
            if (not self._wrote and self.mode != "deep" and self.route != "deck"
                    and self.engine != "deck"):
                add(self.trace, now, "write", "Пишу ответ")
            self._wrote = True
        elif t == "plan":
            attrs = data.get("attributes")
            if isinstance(attrs, list):
                self.run["contract"] = [str(a)[:80] for a in attrs[:24]]
            subq = data.get("subqueries")
            if isinstance(subq, list) and subq:
                self.run["subqueries"] = len(subq)
        elif t == "progress" and data.get("stage") == "research":
            for key in ("pages", "blocked", "sites"):
                if isinstance(data.get(key), int):
                    self.run[key] = data[key]
            if isinstance(data.get("hosts"), list):
                self.run["hosts"] = data["hosts"][:8]
        elif t == "facts_summary":
            self.run["facts"] = {k: data.get(k) for k in ("total", "pages", "by_stance", "critic",
                                                          "subjects") if data.get(k) is not None}
        elif t == "outline" and isinstance(data.get("sections"), list):
            self.run["outline"] = [str(s)[:160] for s in data["sections"][:30]]

    def finish(self, finished: bool) -> None:
        if self._closed:
            return
        self._closed = True
        if finished:
            add(self.trace, self._clock(), "done", "Готово")
            return
        add(self.trace, self._clock(), "stop", "Остановлено")
        if self.mode == "deep" and self.phase and self.phase != "done":
            self.run["stopped_phase"] = self.phase
