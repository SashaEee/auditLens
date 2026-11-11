"""Режим отчёта не запускается на реплику в продолжение разговора."""
import pytest

from bank_audit.ai.analyst import is_followup_reply

HISTORY = [{"role": "user", "content": "Жалобы на вклады Сбера"},
           {"role": "assistant", "content": "Нашёл 35 жалоб за 90 дней. " * 20}]


@pytest.mark.parametrize("reply", [
    "хочу", "да", "Да, собери, пожалуйста.", "собери", "сформируй файл",
    "сведи эту информацию в таблицу", "Покажи все жалобы", "Сколько всего таких жалоб?",
    "Из какого источника доля рынка Сбербанка 0,87040",
    "да давай, формируй блок схему для слайда", "добавь ссылки на источник",
    "покажи ссылку на banki.ru на это обращение", "новая задача",
])
def test_reply_to_previous_answer_is_not_a_new_research(reply):
    assert is_followup_reply(reply, HISTORY)


@pytest.mark.parametrize("question", [
    "сравни с другими банками", "Сравни текущие вклады до 3-х месяцев", "выездное обслуживание",
    "Сервис Свое Дело для самозанятых в Сбере", "найди информацию про выездной сервис",
    "жалобы на эквайринг за 2026 год", "да, сравни ещё с ВТБ",
    "Добавь в таблицу по продукту драгметаллы для банков Сбербанк, Альфа-Банк, Т-Банк, ВТБ, "
    "Газпромбанк, Россельхозбанк, Совкомбанк данные о ставках и тарифах",
])
def test_new_question_stays_a_research(question):
    assert not is_followup_reply(question, HISTORY)


def test_first_message_is_never_a_reply():
    assert not is_followup_reply("хочу", [])
    assert not is_followup_reply("сформируй файл", [{"role": "user", "content": "x"}])


# ── воронка уточняющих вопросов ──────────────────────────────────────────────

def test_bank_question_is_dropped_when_comparison_is_not_asked():
    from bank_audit.ai.clarify import drop_bank_questions
    res = {"complete": False, "reason": "x", "questions": [
        {"id": "banks", "question": "Какие банки сравнить?", "type": "multi", "options": []},
        {"id": "product", "question": "По какому продукту смотреть жалобы?", "type": "single", "options": []}]}
    out = drop_bank_questions(res, asks_compare=False)
    assert [q["id"] for q in out["questions"]] == ["product"]
    assert drop_bank_questions(res, asks_compare=True) is res
    only_banks = {"complete": False, "questions": [res["questions"][0]]}
    assert drop_bank_questions(only_banks, asks_compare=False)["complete"] is True


def test_funnel_prompt_no_longer_recommends_top4_by_default():
    from bank_audit.ai import clarify
    p = clarify.SYSTEM_PROMPT_CLARIFY
    assert "ТОЛЬКО если аудитор просит сравнение" in p and "Только Сбер, без сравнения" in p
    assert "предложи топ-4" not in p


# ── «сделай презентацию» по уже полученному отчёту ───────────────────────────
def test_deck_request_is_a_short_ask_after_an_answer():
    from bank_audit.ai import analyst as A
    hist = [{"role": "user", "content": "Сравни кредиты"}, {"role": "assistant", "content": "отчёт " * 400}]
    assert A.is_deck_request("сделай презентацию", hist)
    assert A.is_deck_request("Собери слайды по этому отчёту для руководителя", hist)
    # новый содержательный вопрос с презентацией — это исследование, а не реплика
    assert not A.is_deck_request("Сравни вклады Сбера и ВТБ и сделай презентацию", hist)
    # без ответа в разговоре собирать не из чего
    assert not A.is_deck_request("сделай презентацию", [])
    assert not A.is_deck_request("сделай таблицу", hist)


def _deck_env(monkeypatch, built):
    from bank_audit.ai import agent_files
    from bank_audit.research.gptr import deck_html as H
    from bank_audit.web import runctx, userdata

    report = {"report_id": 5, "title": "Потребкредиты", "question": "Сравни", "body": "## Резюме\n\nТекст " * 200,
              "payload": {"sources": [{"n": 1, "title": "t", "domain": "d"}]}, "created_at": None}
    monkeypatch.setattr(runctx, "current_origin", lambda: {"session_id": 7, "username": "u"})
    monkeypatch.setattr(userdata, "last_deep_report", lambda s, u: report)
    monkeypatch.setattr(userdata, "get_report", lambda rid, u, as_admin=False: report if rid == 5 else None)

    async def html_build(client, **kw):
        built.append(("html", kw["title"], kw["sources"][0]["n"]))
        return b"<!doctype html>", {"slides": 9, "unknown": ["2027"], "fixed": 0}
    monkeypatch.setattr(H, "build", html_build)
    monkeypatch.setattr(agent_files, "store", lambda name, data: {
        "id": "a" * 32, "name": name, "size": len(data), "marker": "[[FILE:" + "a" * 32 + "]]"})
    monkeypatch.setenv("LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")


def _run(question, history, **kw):
    import asyncio
    import json as _json
    from bank_audit.ai import analyst as A

    async def go():
        return [_json.loads(e) async for e in A.stream_analysis(question, history, **kw)]
    return asyncio.run(go())


def test_deck_reply_builds_an_offline_html_from_the_last_report(monkeypatch):
    built: list = []
    _deck_env(monkeypatch, built)
    evs = _run("сделай презентацию", [{"role": "assistant", "content": "отчёт"}], force_deep=True)
    assert [e["type"] for e in evs] == ["route", "notice", "mode", "engine", "text", "files", "done"]
    assert evs[0]["value"] == "deck"
    evs = evs[1:]
    assert built == [("html", "Потребкредиты", 1)]
    assert "без нового исследования" in evs[0]["text"] and evs[1]["value"] == "quick"
    assert "[[FILE:" in evs[3]["chunk"] and "без интернета" in evs[3]["chunk"] and "9 слайдов" in evs[3]["chunk"]
    # ответ не просит ничего сверять вручную и не предлагает другой формат
    low = evs[3]["chunk"].lower()
    assert "вручную" not in low and "pptx" not in low and "powerpoint" not in low and "2027" not in low
    assert evs[4]["ids"] == ["a" * 32]


def test_deck_button_names_the_report_and_needs_no_history(monkeypatch):
    built: list = []
    _deck_env(monkeypatch, built)
    evs = _run("Собери презентацию по этому отчёту", [], deck_report_id=5)
    assert built == [("html", "Потребкредиты", 1)] and evs[-1]["type"] == "done"
    # чужой или несуществующий отчёт — презентации нет, вопрос идёт обычным путём
    built.clear()
    from bank_audit.ai import analyst as A

    async def nothing():
        return [e async for e in A._deck_from_report("сделай презентацию", 99)]
    import asyncio
    assert asyncio.run(nothing()) == [] and built == []


# ── Единое поле ввода: глубину выбирает помощник (ai/router.py) ──────────────
REPORT = {"role": "assistant", "content": "## Резюме\n\n" + "Вклады Сбера и ВТБ. " * 200}
SHORT = {"role": "assistant", "content": "Ставка по вкладу — 15 % годовых."}


def _decide(monkeypatch, answer, question, history=None):
    """decide() с подменённой моделью: answer — текст ответа или исключение."""
    import asyncio
    import types

    import openai

    from bank_audit.ai import router as R
    seen: dict = {}

    class _Chat:
        async def create(self, **kw):
            seen.update(kw)
            if isinstance(answer, Exception):
                raise answer
            msg = types.SimpleNamespace(content=answer)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    class _Client:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=_Chat())

    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)
    monkeypatch.setenv("LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL_SMART", "m")
    monkeypatch.delenv("AI_ROUTER", raising=False)
    return asyncio.run(R.decide(question, history or [])), seen


def test_router_takes_the_route_from_the_model_and_shows_it_the_talk(monkeypatch):
    r, seen = _decide(monkeypatch, '{"route": "report", "reason": "новый банк"}', "а по ВТБ?",
                      [{"role": "user", "content": "Сравни вклады"}, REPORT])
    assert (r.route, r.by) == ("report", "model") and r.reason == "новый банк"
    user = seen["messages"][1]["content"]
    assert "Помощник (отчёт" in user and "Аудитор: Сравни вклады" in user and "а по ВТБ?" in user
    assert len(user) < 2500                 # отчёт целиком в диспетчера не уходит


def test_router_falls_back_to_rules_when_the_model_fails(monkeypatch):
    r, _ = _decide(monkeypatch, RuntimeError("шлюз лёг"), "Сравни ипотеку Сбера и ВТБ по ставкам и взносу")
    assert (r.route, r.by) == ("report", "rules")
    r, _ = _decide(monkeypatch, "не знаю", "какая ставка по вкладу?")
    assert (r.route, r.by) == ("answer", "rules")
    r, _ = _decide(monkeypatch, RuntimeError("x"), "сформируй файл", [{"role": "user", "content": "q"}, SHORT])
    assert r.route == "reply"


def test_router_never_picks_a_route_it_cannot_run(monkeypatch):
    # презентация без отчёта в разговоре → сначала отчёт; реплика без ответа → обычный вопрос
    r, _ = _decide(monkeypatch, '{"route": "deck"}', "сделай презентацию про ипотеку Сбера")
    assert r.route == "report"
    r, _ = _decide(monkeypatch, '{"route": "reply"}', "покажи остальные")
    assert r.route == "answer"
    r, _ = _decide(monkeypatch, '{"route": "deck"}', "сделай презентацию", [REPORT])
    assert r.route == "deck"


def test_router_off_means_rules(monkeypatch):
    from bank_audit.ai import router as R
    monkeypatch.setenv("AI_ROUTER", "0")
    import asyncio
    r = asyncio.run(R.decide("Что такое овердрафт?", []))
    assert (r.route, r.by) == ("answer", "rules")


def test_route_from_the_request_wins_and_is_announced(monkeypatch):
    """Человек нажал «Хватит короткого ответа» — отчёт не запускается, даже если
    вопрос по словам «исследовательский»; маршрут уходит клиенту первым событием."""
    from bank_audit.ai import analyst as A
    from bank_audit.ai import hermes_quick as HQ
    from bank_audit.ai import router as R

    async def boom(*a, **k):
        raise AssertionError("диспетчер не нужен: маршрут уже выбран")
    monkeypatch.setattr(R, "decide", boom)

    async def quick(question, history, session_hint=None):
        yield '{"type": "text", "chunk": "ок"}'
        yield '{"type": "done"}'
    monkeypatch.setattr(HQ, "stream_quick_hermes", quick)
    monkeypatch.setenv("QUICK_ENGINE", "hermes")
    evs = _run("Сравни вклады Сбера и ВТБ по ставкам, срокам и условиям пополнения", [],
               route="answer", route_by="user")
    assert evs[0] == {"type": "route", "value": "answer", "by": "user"}
    assert {"type": "mode", "value": "quick"} in evs and evs[-1]["type"] == "done"
    assert A is not None


def test_no_route_in_the_request_asks_the_router(monkeypatch):
    from bank_audit.ai import hermes_quick as HQ
    from bank_audit.ai import router as R

    async def decide(question, history=None):
        return R.Route("reply", "model", 900, "вопрос о сказанном")
    monkeypatch.setattr(R, "decide", decide)

    async def quick(question, history, session_hint=None):
        yield '{"type": "done"}'
    monkeypatch.setattr(HQ, "stream_quick_hermes", quick)
    monkeypatch.setenv("QUICK_ENGINE", "hermes")
    evs = _run("откуда цифра 15 %?", [SHORT])
    assert evs[0] == {"type": "route", "value": "reply", "by": "model"}


def test_digest_marks_reports_and_keeps_it_short():
    from bank_audit.ai import router as R
    d = R.digest([{"role": "user", "content": "вопрос " * 200}, REPORT, SHORT,
                  {"role": "assistant", "content": "x" * 100, "kind": "report"}])
    assert d.count("Помощник (отчёт") == 2 and "Помощник (короткий ответ" in d
    assert len(d) < 2000
    assert "только начинается" in R.digest([])
    assert R.has_report([REPORT]) and not R.has_report([SHORT]) and R.has_answer([SHORT])


def test_prepare_starts_short_answers_without_waiting_for_questions(monkeypatch):
    """Справке уточняющие вопросы не нужны: проверка полноты отменяется, человек не ждёт."""
    import asyncio

    from bank_audit.ai import clarify as C
    from bank_audit.ai import router as R
    state = {"cancelled": False, "asked": 0}

    async def slow_questions(question, history=None):
        state["asked"] += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return {"complete": False, "questions": [{"id": "q"}]}

    async def decide(question, history=None):
        await asyncio.sleep(0)
        return R.Route("answer", "model", 800)
    monkeypatch.setattr(C, "generate_clarifications", slow_questions)
    monkeypatch.setattr(R, "decide", decide)

    async def go():
        res = await C.prepare("какая ставка по вкладу?", [])
        await asyncio.sleep(0)
        return res
    res = asyncio.run(go())
    assert res["route"] == "answer" and res["complete"] is True and res["questions"] == []
    assert state == {"cancelled": True, "asked": 1}


def test_prepare_asks_questions_only_for_a_report(monkeypatch):
    import asyncio

    from bank_audit.ai import clarify as C
    from bank_audit.ai import router as R

    async def questions(question, history=None):
        return {"complete": False, "reason": "нет продукта", "questions": [{"id": "product"}]}

    async def decide(question, history=None):
        return R.Route("report", "model", 700)
    monkeypatch.setattr(C, "generate_clarifications", questions)
    monkeypatch.setattr(R, "decide", decide)
    res = asyncio.run(C.prepare("Сравни условия с конкурентами", []))
    assert res["route"] == "report" and res["complete"] is False and res["questions"] == [{"id": "product"}]

    async def hangs(question, history=None):
        await asyncio.sleep(30)
    monkeypatch.setattr(C, "generate_clarifications", hangs)
    res = asyncio.run(C.prepare("Сравни условия с конкурентами", [], timeout=0.05))
    assert res["route"] == "report" and res["complete"] is True and res["reason"] == "timeout"
