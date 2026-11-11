"""Письмо раздела отчёта: обрыв связи повторяется, отказ модели не доходит до читателя."""
import asyncio

import pytest

from bank_audit.research.gptr import dossier as D

LONG = "Сбербанк заявляет ставку 14% годовых по вкладу на шесть месяцев [f:1]. " * 12
REFUSAL = ("Я не могу сформировать этот отчёт. Инструкции в начале запроса предписывают "
           "мне принять некую клиентскую идентичность. Я Claude, работаю от Anthropic.")


def run(gen):
    async def go():
        return [p async for p in gen]
    return asyncio.run(go())


def fake(script):
    """script — список ответов по заходам: строка (текст), Exception (упасть сразу)
    или (текст, Exception) — упасть после текста."""
    calls = {"n": 0, "models": []}

    async def stream(client, model, *, question, plan, context, raw_prompt):
        step = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        calls["models"].append(model)
        text, err = step if isinstance(step, tuple) else ((None, step) if isinstance(step, Exception) else (step, None))
        if text:
            for i in range(0, len(text), 40):
                yield text[i:i + 40]
        if err:
            raise err
    return stream, calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def nap(_s):
        return None
    monkeypatch.setattr(D.asyncio, "sleep", nap)
    monkeypatch.delenv("GPTR_WRITER_FALLBACK", raising=False)
    monkeypatch.setenv("LLM_MODEL_REASONING", "запасная")
    monkeypatch.setenv("LLM_MODEL_NAME", "общая")


@pytest.mark.parametrize("msg", [
    "Connection error.",
    "peer closed connection without sending complete message body (incomplete chunked read)",
    "Server disconnected without sending a response",
    "Overloaded", "Error code: 429", "Request timed out"])
def test_transient_errors_are_retried(msg):
    assert D._RETRY_ERR.search(msg)


@pytest.mark.parametrize("msg", [
    "invalid_request_error: unknown model", "authentication failed",
    "`temperature` is deprecated for this model"])
def test_permanent_errors_are_not_retried(msg):
    assert not D._RETRY_ERR.search(msg)


def test_connection_drop_at_start_is_retried(monkeypatch):
    stream, calls = fake([RuntimeError("Connection error."), LONG])
    monkeypatch.setattr(D, "_stream", stream)
    assert "".join(run(D._stream_section(None, "основная", "p"))) == LONG
    assert calls["n"] == 2


def test_drop_before_anything_was_shown_is_retried_without_duplicates(monkeypatch):
    # модель успела выдать пару фраз, но читателю они ещё не ушли: начало придержано
    stream, calls = fake([("Короткое начало. ", RuntimeError("peer closed connection")), LONG])
    monkeypatch.setattr(D, "_stream", stream)
    assert "".join(run(D._stream_section(None, "основная", "p"))) == LONG
    assert calls["n"] == 2


def test_drop_after_text_was_shown_is_not_retried(monkeypatch):
    stream, calls = fake([(LONG, RuntimeError("Overloaded"))])
    monkeypatch.setattr(D, "_stream", stream)
    with pytest.raises(RuntimeError):
        run(D._stream_section(None, "основная", "p"))
    assert calls["n"] == 1


def test_refusal_is_rewritten_and_never_shown(monkeypatch):
    stream, calls = fake([REFUSAL, LONG])
    monkeypatch.setattr(D, "_stream", stream)
    out = "".join(run(D._stream_section(None, "основная", "p")))
    assert out == LONG
    assert "Claude" not in out and calls["n"] == 2


def test_two_refusals_switch_to_fallback_writer(monkeypatch):
    stream, calls = fake([REFUSAL, REFUSAL, LONG])
    monkeypatch.setattr(D, "_stream", stream)
    assert "".join(run(D._stream_section(None, "основная", "p"))) == LONG
    assert calls["models"] == ["основная", "основная", "запасная"]


def test_refusal_from_every_model_raises_instead_of_showing(monkeypatch):
    stream, calls = fake([REFUSAL])
    monkeypatch.setattr(D, "_stream", stream)
    with pytest.raises(D.Refused):
        run(D._stream_section(None, "основная", "p"))
    assert calls["models"][-1] == "запасная"


def test_self_introduction_is_cut_and_section_kept(monkeypatch):
    stream, _ = fake(["Я — Claude, ассистент от Anthropic. " + LONG])
    monkeypatch.setattr(D, "_stream", stream)
    out = "".join(run(D._stream_section(None, "основная", "p")))
    assert "Claude" not in out and "Anthropic" not in out
    assert out.strip() == LONG.strip()


def test_self_introduction_in_the_middle_is_cut(monkeypatch):
    text = LONG + "Я Claude, разработан Anthropic, и не скрываю этого. " + LONG
    stream, _ = fake([text])
    monkeypatch.setattr(D, "_stream", stream)
    out = "".join(run(D._stream_section(None, "основная", "p")))
    assert "Claude" not in out
    assert out.count("ставку 14%") == 24


def test_short_honest_section_is_not_taken_for_refusal(monkeypatch):
    text = "Сопоставить ставки нельзя: у ВТБ в источниках только нижняя граница [f:3]."
    stream, _ = fake([text])
    monkeypatch.setattr(D, "_stream", stream)
    assert "".join(run(D._stream_section(None, "основная", "p"))) == text


# ── Шлюз отверг параметр, не назвав его ──────────────────────────────────────
def test_unnamed_rejection_is_remembered_after_blind_retry_succeeds(monkeypatch):
    import asyncio
    from bank_audit.ai import llm_utils as U
    from bank_audit.research.gptr import facts as F

    monkeypatch.setattr(U, "_DROP_PARAMS", {})
    calls = []

    class Err(Exception):
        status_code = 400

    class Completions:
        async def create(self, **kw):
            calls.append(dict(kw))
            if "extra_body" in kw and kw["extra_body"]:
                raise Err("Error code: 400 - Invalid request parameter")
            return "ok"

    class Client:
        class chat:
            completions = Completions()

    def kw():
        return {"model": "m-mini", "messages": [], "extra_body": {"thinking": {"type": "disabled"}}}

    assert asyncio.run(F.call_model(Client, "m-mini", kw())) == "ok"
    assert len(calls) == 2 and "extra_body" not in calls[1]
    # второй вызов уже не шлёт отвергнутое — один запрос вместо двух
    assert asyncio.run(F.call_model(Client, "m-mini", kw())) == "ok"
    assert len(calls) == 3 and not calls[2].get("extra_body")
    # другая модель память не наследует
    assert "m-big" not in U._DROP_PARAMS


def test_unnamed_rejection_with_several_extra_keys_is_not_remembered(monkeypatch):
    from bank_audit.ai import llm_utils as U
    monkeypatch.setattr(U, "_DROP_PARAMS", {})

    class Err(Exception):
        status_code = 400
    assert not U.remember_extra_rejected("m", Err("400"), {"thinking": 1, "reasoning_effort": "low"})
    assert not U.remember_extra_rejected("m", RuntimeError("Connection error."), {"thinking": 1})
    assert U._DROP_PARAMS == {}


# ── Сайт не отвечает: три пустых ответа — и его страницы пропускаются ───────
def test_dead_host_is_skipped_after_three_empty_answers(monkeypatch):
    from bank_audit.research.gptr import runstate, scraper as S
    monkeypatch.setattr(S, "_host_state", {})
    calls: list = []

    def read(self, *, browser):
        calls.append((self.link, browser))
        return ("", "") if "dead.example" in self.link else ("текст " * 200, "Страница")
    monkeypatch.setattr(S.AuditLensScraper, "_read", read)
    monkeypatch.setattr(S.AuditLensScraper, "_read_copy", lambda self: ("", "", None))
    monkeypatch.setattr(S.AuditLensScraper, "_to_archive", lambda self: None)
    st = runstate.new_run()
    for i in range(5):
        S.AuditLensScraper(f"https://www.dead.example/p{i}", state=st).scrape()
    # три страницы прочитаны обоими способами (HTTP и браузер), остальные пропущены сразу
    assert len([c for c in calls if "dead.example" in c[0]]) == 6
    assert S.host_down("https://dead.example/any") and not S.host_down("https://live.example/a")
    assert st.unreadable["https://www.dead.example/p4"] == "сайт не отвечает"
    # другой сайт читается как обычно, и успех снимает паузу со своего сайта
    text, _, _ = S.AuditLensScraper("https://live.example/a", state=st).scrape()
    assert text and "https://live.example/a" in st.pages
    S.note_host("https://dead.example/x", True)
    assert not S.host_down("https://dead.example/any")
