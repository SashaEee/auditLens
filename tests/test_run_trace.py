"""Лента шагов прогона ИИ-помощника — «как получен ответ» (ai/run_trace.py)."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.ai import run_trace as T  # noqa: E402


def _rec(events, finished=True):
    clock = {"t": 0.0}
    rec = T.Recorder(lambda: clock["t"])
    for sec, ev in events:
        clock["t"] = sec
        rec.feed(ev)
    rec.finish(finished)
    return rec


def test_quick_answer_trace_counts_repeated_tools_and_marks_writing():
    rec = _rec([
        (0, {"type": "route", "value": "answer", "by": "model"}),
        (0, {"type": "mode", "value": "quick"}),
        (0, {"type": "engine", "value": "hermes"}),
        (2, {"type": "tool_call", "name": "Навык: жалобы"}),
        (4, {"type": "tool_call", "name": "Сюжеты жалоб"}),
        (6, {"type": "tool_call", "name": "Сюжеты жалоб"}),
        (9, {"type": "tool_call", "name": "Веб-поиск"}),
        (11, {"type": "text", "chunk": "Коротко: "}),
        (12, {"type": "text", "chunk": "три вещи"}),
        (14, {"type": "done"}),
    ])
    steps = [(e["t"], e["k"], e["label"], e.get("n")) for e in rec.trace]
    assert steps == [
        (0, "route", "Отвечаю короткой справкой", None),
        (2, "tool", "Навык: жалобы", None),
        (4, "tool", "Сюжеты жалоб", 2),
        (9, "tool", "Веб-поиск", None),
        (11, "write", "Пишу ответ", None),
        (14, "done", "Готово", None),
    ]
    assert rec.run == {}


def test_report_trace_keeps_phases_skips_heartbeats_and_collects_run_summary():
    rec = _rec([
        (0, {"type": "route", "value": "report", "by": "model"}),
        (0, {"type": "mode", "value": "deep"}),
        (0, {"type": "phase", "value": "planning"}),
        (0, {"type": "stage_status", "stage": "conductor", "label": "Разбираю вопрос", "detail": "какая задача"}),
        # повтор подписи с секундами — не шаг
        (10, {"type": "stage_status", "stage": "conductor", "label": "Разбираю вопрос", "progress_elapsed": 10}),
        (20, {"type": "plan", "attributes": ["состав сервиса", "тарифы"], "subqueries": ["a", "b", "c"]}),
        (20, {"type": "phase", "value": "research"}),
        (40, {"type": "progress", "stage": "research", "pages": 48, "blocked": 1, "sites": 6,
              "hosts": [["bank-a.example", 12], ["nalog.gov.ru", 6]], "last_host": "bank-a.example"}),
        (80, {"type": "phase", "value": "extraction"}),
        (120, {"type": "facts_summary", "total": 214, "pages": 90, "by_stance": {"declared": 120}}),
        (130, {"type": "phase", "value": "writing"}),
        (131, {"type": "outline", "sections": ["Что входит", "Как подключается"]}),
        (132, {"type": "stage_status", "stage": "analyst", "label": "Пишу раздел: Что входит"}),
        (200, {"type": "stage_status", "stage": "analyst", "label": "Рисую карточки: осталось 3"}),
        (210, {"type": "stage_status", "stage": "analyst", "label": "Рисую карточки: осталось 1"}),
        (220, {"type": "text", "chunk": "## Что входит"}),
        (240, {"type": "phase", "value": "done"}),
        (240, {"type": "done"}),
    ])
    labels = [e["label"] for e in rec.trace if e["k"] == "stage"]
    assert labels == ["Разбираю вопрос", "Пишу раздел: Что входит", "Рисую карточки: осталось 1"]
    assert [e["label"] for e in rec.trace if e["k"] == "phase"] == [
        "planning", "research", "extraction", "writing", "done"]
    assert not any(e["k"] == "write" for e in rec.trace)          # у отчёта «Пишу ответ» не ставится
    assert rec.trace[-1] == {"t": 240, "k": "done", "label": "Готово"}
    assert rec.run == {
        "contract": ["состав сервиса", "тарифы"], "subqueries": 3,
        "pages": 48, "blocked": 1, "sites": 6, "hosts": [["bank-a.example", 12], ["nalog.gov.ru", 6]],
        "facts": {"total": 214, "pages": 90, "by_stance": {"declared": 120}},
        "outline": ["Что входит", "Как подключается"],
    }


def test_interrupted_report_remembers_where_it_stopped():
    rec = _rec([
        (0, {"type": "mode", "value": "deep"}),
        (0, {"type": "phase", "value": "planning"}),
        (15, {"type": "phase", "value": "research"}),
    ], finished=False)
    assert rec.trace[-1] == {"t": 15, "k": "stop", "label": "Остановлено"}
    assert rec.run["stopped_phase"] == "research"
    rec.finish(True)                                              # повторное закрытие ничего не меняет
    assert rec.trace[-1]["k"] == "stop"


def test_deck_answer_is_not_marked_as_writing_and_trace_is_capped():
    rec = _rec([
        (0, {"type": "route", "value": "deck", "by": "user"}),
        (0, {"type": "mode", "value": "quick"}),
        (0, {"type": "engine", "value": "deck"}),
        (0, {"type": "stage_status", "stage": "deck", "label": "Рисую презентацию по отчёту", "estimate_s": 140}),
        (130, {"type": "text", "chunk": "Презентация по отчёту…"}),
        (131, {"type": "done"}),
    ])
    assert [e["k"] for e in rec.trace] == ["route", "stage", "done"]
    assert rec.trace[0]["label"] == "Собираю презентацию по готовому отчёту"
    trace: list = []
    for i in range(400):
        T.add(trace, i, "tool", f"шаг {chr(1040 + i % 30)}{i}")
    assert len(trace) == T.MAX_STEPS
    T.add(trace, 1, "stage", "   ")                              # пустая подпись — не шаг
    assert len(trace) == T.MAX_STEPS


def test_trace_and_run_summary_are_saved_with_the_answer(monkeypatch):
    """Ход работы не пропадает после ответа: лента шагов — в meta сообщения и в payload
    отчёта, сводка прогона отчёта — в payload; клиенту поток идёт без изменений."""
    import asyncio
    import json
    from bank_audit.web import app as A
    from bank_audit.web import userdata
    saved, messages = [], []
    monkeypatch.setattr(userdata, "log_event", lambda u, k, p=None: None)
    monkeypatch.setattr(userdata, "add_message", lambda sid, role, body, meta=None: messages.append(meta) or 1)
    monkeypatch.setattr(userdata, "parse_query_signals", lambda q: {})
    monkeypatch.setattr(userdata, "save_report", lambda *a, **k: saved.append(k["payload"]) or 7)
    monkeypatch.setattr(userdata, "count_reports", lambda u: 1)
    monkeypatch.setattr(userdata, "title_session_from_report", lambda *a: None)

    async def run(items, stop_after=None):
        async def inner():
            for it in items:
                yield json.dumps(it, ensure_ascii=False)
        out = []
        gen = A._persisting_stream(inner(), "u", 1, "вопрос")
        async for ev in gen:
            out.append(json.loads(ev))
            if stop_after is not None and len(out) >= stop_after:
                await gen.aclose()
                break
        return out

    report = [{"type": "route", "value": "report", "by": "model"}, {"type": "mode", "value": "deep"},
              {"type": "phase", "value": "planning"},
              {"type": "stage_status", "stage": "conductor", "label": "Разбираю вопрос"},
              {"type": "plan", "steps": [], "task": "product", "subjects": ["sberbank"],
               "subject_labels": {"sberbank": "Сбербанк"}, "attributes": ["тарифы"], "subqueries": ["q"]},
              {"type": "phase", "value": "research"},
              {"type": "progress", "stage": "research", "pages": 12, "blocked": 0, "sites": 3,
               "hosts": [["bank-a.example", 9]], "last_host": "bank-a.example"},
              {"type": "report_title", "title": "Т"}, {"type": "text", "chunk": "Отчёт. " * 200},
              {"type": "phase", "value": "done"}, {"type": "done", "timings": {"plan": 5}}]
    out = asyncio.run(run(report))
    assert [e["type"] for e in out if e["type"] in ("route", "plan", "progress")] == ["route", "plan", "progress"]
    payload = saved[-1]
    assert [e["k"] for e in payload["trace"]] == ["route", "phase", "stage", "phase", "phase", "done"]
    assert payload["run"]["pages"] == 12 and payload["run"]["contract"] == ["тарифы"]
    assert payload["plan"]["subject_labels"] == {"sberbank": "Сбербанк"}
    assert messages[-1]["trace"] == payload["trace"]

    # прогон оборван на сборе: отчёта нет (текста не было), но лента в сообщении не нужна — сообщения нет
    saved.clear(); messages.clear()
    asyncio.run(run(report[:7], stop_after=6))
    assert saved == [] and messages == []

    # короткий ответ: лента — в meta сообщения
    quick = [{"type": "route", "value": "answer", "by": "model"}, {"type": "mode", "value": "quick"},
             {"type": "tool_call", "name": "Сюжеты жалоб"}, {"type": "text", "chunk": "Коротко."}, {"type": "done"}]
    asyncio.run(run(quick))
    assert [e["k"] for e in messages[-1]["trace"]] == ["route", "tool", "write", "done"]


def test_live_hosts_and_step_caption_helpers():
    """Живая строка сбора: сколько разных сайтов прочитано и какой — последним; данные
    AuditLens сайтами не считаются. Подпись шага не обрывается посреди слова."""
    from bank_audit.research.gptr import stream as S
    urls = ["https://www.bank-a.example/a", "#reviews?view=stories", "https://nalog.gov.ru/x",
            "https://bank-a.example/b", "https://www.cbr.ru/doc.pdf"]
    assert S._hosts_live(urls) == {"sites": 3, "last_host": "cbr.ru"}
    assert S._hosts_live([]) == {"sites": 0, "last_host": ""}
    long = "Аудитору нужен полный профиль сервиса для самозанятых: " + "что входит, как подключается, " * 12
    cut = S._clip(long, 120)
    assert len(cut) <= 121 and cut.endswith("…") and not cut[:-1].endswith((" ", ","))
    assert long.startswith(cut[:-1])
    assert S._clip("  Коротко   и ясно ") == "Коротко и ясно"
