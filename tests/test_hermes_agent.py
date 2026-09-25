"""Быстрый режим на Hermes: адаптер, MCP-сервер инструментов, формат данных.

Без сети и без БД: прогон Hermes подменяется генератором событий, MCP-сервер
вызывается через ASGI-транспорт.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bank_audit.ai import agent_tools as T
from bank_audit.ai import hermes_quick as H


# ── чистка текста ────────────────────────────────────────────────────────────

def test_sanitize_local_links():
    s = ("См. [жалобы](http://127.0.0.1:8000/#reviews?theme=chargeback&days=7), "
         "[API](http://127.0.0.1:8000/api/reviews/feed?q=1) и http://localhost:8000/x.")
    out = H.sanitize(s)
    assert "[жалобы](#reviews?theme=chargeback&days=7)" in out
    assert "API" in out and "127.0.0.1" not in out and "localhost" not in out


def test_sanitize_theme_keys_and_citations():
    s = ("Тема chargeback растёт, `block_161` тоже 【582†fragments】. "
         "[жалобы](#reviews?theme=chargeback&days=7)")
    out = H.sanitize(s)
    assert "chargeback растёт" not in out and "block_161" not in out.split("](")[0]
    assert "Чарджбэк" in out and "161-ФЗ" in out and "【" not in out
    assert "(#reviews?theme=chargeback&days=7)" in out
    assert H.sanitize("данные `mcp__auditlens__market_offers`") == "данные «Предложения банков»"


def test_bare_addresses_become_links_and_keep_keys():
    s = ("Тема в AuditLens: #reviews?theme=chargeback&days=7&tab=complaints\n"
         "- https://www.banki.ru/services/responses/bank/response/13387250\n"
         "Уже ссылка: [отзыв](https://www.banki.ru/x) и [тема](#reviews?theme=chargeback).")
    out = H.sanitize(s)
    assert "[открыть в AuditLens](#reviews?theme=chargeback&days=7&tab=complaints)" in out
    assert "[banki.ru](https://www.banki.ru/services/responses/bank/response/13387250)" in out
    assert "[отзыв](https://www.banki.ru/x)" in out and "[тема](#reviews?theme=chargeback)" in out
    assert "Чарджбэк" not in out
    assert H.sanitize("источник: https://cbr.ru/press/.") == "источник: [cbr.ru](https://cbr.ru/press/)."


def test_safe_cut_keeps_open_link():
    buf = "Главное: всплеск. Подробнее [в отзывах](#reviews?th"
    cut = H.safe_cut(buf)
    assert buf[:cut].endswith("Подробнее ")
    assert H.safe_cut("готово [ссылка](#r) дальше") == len("готово [ссылка](#r) ")


@pytest.mark.parametrize("text,stub", [
    ("", True), ("Давай разберусь детальнее.", True), ("Сейчас проверю данные…", True),
    ("I reached the iteration limit and couldn't generate a summary.", True),
    ("**Всплеск чарджбэка — это билеты на концерт.** 6 из 15 жалоб…", False),
])
def test_is_stub(text, stub):
    assert H.is_stub(text) is stub


def test_legal_note_trigger():
    assert H.needs_legal_note("по ст. 7 закона 161-ФЗ")
    assert not H.needs_legal_note("по [закону 161-ФЗ](https://pravo.gov.ru/x)")
    assert not H.needs_legal_note("ставка 19% годовых")


def test_tool_labels():
    assert H.tool_label("mcp__auditlens__complaint_theme") == "Разбор темы жалоб"
    assert H.tool_label("skill_view") == "Навык"
    assert H.tool_label("browser_click") == "Браузер"


# ── поток ответа ─────────────────────────────────────────────────────────────

def _collect(gen) -> list[dict]:
    async def run():
        return [json.loads(x) async for x in gen]
    return asyncio.run(run())


def _fake_runs(*runs):
    """Подменяет _one_run: каждый вызов отдаёт следующий список событий."""
    it = iter(runs)
    calls = []

    async def fake(question, history, headers, model, retry, deadline, trace):
        calls.append({"retry": retry, "model": model})
        for ev in next(it):
            if isinstance(ev, Exception):
                raise ev
            yield ev
    return fake, calls


def _text(evs):
    return "".join(e.get("chunk", "") for e in evs if e["type"] == "text")


def test_narration_before_tool_is_dropped(monkeypatch):
    answer = "**Главное:** всплеск чарджбэка — билеты на концерт. " * 8
    fake, _ = _fake_runs([{"delta": "Сейчас посмотрю сигналы."},
                          {"tool": "mcp__auditlens__complaint_signals"},
                          {"delta": answer}, {"final": answer}])
    monkeypatch.setattr(H, "_one_run", fake)
    evs = _collect(H.stream_quick_hermes("почему растёт чарджбэк", []))
    assert "Сейчас посмотрю" not in _text(evs)
    assert _text(evs).strip() == answer.strip()
    assert {"type": "tool_call", "name": "Сигналы жалоб"} in evs
    meta = next(e for e in evs if e["type"] == "run_meta")
    assert meta["tools"] == ["mcp__auditlens__complaint_signals"]
    assert evs[-1] == {"type": "done"}


def test_short_answer_from_final(monkeypatch):
    fake, _ = _fake_runs([{"tool": "x"}, {"final": "Ставка 19% годовых [Рынок](http://127.0.0.1:8000/#market?cat=deposit)."}])
    monkeypatch.setattr(H, "_one_run", fake)
    evs = _collect(H.stream_quick_hermes("q", []))
    assert _text(evs) == "Ставка 19% годовых [Рынок](#market?cat=deposit)."


def test_stub_retries_once_then_answers(monkeypatch):
    fake, calls = _fake_runs([{"final": "Давай разберусь детальнее."}],
                             [{"final": "Ответ по данным: 15 жалоб за неделю."}])
    monkeypatch.setattr(H, "_one_run", fake)
    evs = _collect(H.stream_quick_hermes("q", []))
    assert [c["retry"] for c in calls] == [False, True]
    assert "15 жалоб" in _text(evs)
    assert next(e for e in evs if e["type"] == "run_meta")["empty_attempts"] == 1


def test_two_stubs_fall_back(monkeypatch):
    fake, _ = _fake_runs([{"final": ""}], [{"final": "Сейчас проверю"}])
    monkeypatch.setattr(H, "_one_run", fake)
    with pytest.raises(H.HermesNotStreamed):
        _collect(H.stream_quick_hermes("q", []))


def test_unavailable_before_text_falls_back(monkeypatch):
    fake, _ = _fake_runs([httpx.ConnectError("нет соединения")])
    monkeypatch.setattr(H, "_one_run", fake)
    with pytest.raises(H.HermesNotStreamed):
        _collect(H.stream_quick_hermes("q", []))


def test_break_after_text_marks_incomplete(monkeypatch):
    long = "Факт. " * 60
    fake, _ = _fake_runs([{"delta": long}, H.HermesNotStreamed("обрыв")])
    monkeypatch.setattr(H, "_one_run", fake)
    evs = _collect(H.stream_quick_hermes("q", []))
    assert _text(evs).startswith("Факт.") and "неполным" in _text(evs)


def test_model_route_passed(monkeypatch):
    fake, calls = _fake_runs([{"final": "Нормальный ответ по существу вопроса."}])
    monkeypatch.setattr(H, "_one_run", fake)
    _collect(H.stream_quick_hermes("q", [], model="sonnet"))
    assert calls[0]["model"] == "sonnet"


def test_instructions_have_date():
    s = H.instructions()
    assert "МСК" in s and "AuditLens" in s
    assert "Предыдущая попытка" in H.instructions(retry=True)


# ── данные инструментов ──────────────────────────────────────────────────────

def test_out_is_compact_clean_json():
    s = T.out({"a": 1.23456, "b": None, "c": [], "d": {"x": ""}, "e": "ок"})
    assert json.loads(s) == {"a": 1.23, "e": "ок"}


def test_links_to_pages():
    assert T.link_reviews(theme="chargeback", days=7) == "#reviews?theme=chargeback&days=7&tab=complaints"
    assert T.link_reviews("ВТБ").startswith("#reviews?bank=")
    assert T.link_market("deposit") == "#market?cat=deposit"
    assert T.link_doc(12) == "#knowledge?doc=12"


def test_enums_from_codebook():
    assert "chargeback" in T.THEME_KEYS and "no_issue" not in T.THEME_KEYS
    assert "Вклад" in T.PRODUCT_LABELS
    assert "deposit" in T.CATEGORY_IDS
    assert "chargeback — Оспаривание операций" in T.THEMES_HELP


def test_sql_only_select():
    assert "SELECT" in T.tool_sql("update bank set name='x'")
    assert "SELECT" in T.tool_sql("select 1; drop table bank")


# ── MCP-сервер ───────────────────────────────────────────────────────────────

def _mcp(monkeypatch, key="k"):
    from bank_audit.ai import mcp_server as M
    monkeypatch.setattr(M, "MCP_KEY", key)
    monkeypatch.setattr(M, "_server", None)
    return M


def _rpc(M, *calls):
    """Несколько запросов к одному серверу: менеджер сессий запускается один раз
    (как в приложении — на всё время жизни). calls: (method, params, headers, client)."""
    async def run():
        srv = M.server()
        res = []
        async with srv.session_manager.run():
            for method, params, headers, client in calls:
                tr = httpx.ASGITransport(app=M.asgi_app(), client=client or ("127.0.0.1", 5000))
                async with httpx.AsyncClient(transport=tr, base_url="http://127.0.0.1:8000") as cl:
                    h = {"Authorization": "Bearer k", "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream", **(headers or {})}
                    res.append(await cl.post("/", headers=h, json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}))
        return res
    return asyncio.run(run())


def test_mcp_lists_tools_with_enums(monkeypatch):
    M = _mcp(monkeypatch)
    (r,) = _rpc(M, ("tools/list", None, None, None))
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {t.name for t in T.TOOLS}
    theme = tools["complaint_theme"]["inputSchema"]["properties"]["theme"]
    assert "chargeback" in theme["enum"]


def test_mcp_guard(monkeypatch):
    M = _mcp(monkeypatch)
    bad, proxied, remote, ok = _rpc(
        M, ("tools/list", None, {"Authorization": "Bearer bad"}, None),
        ("tools/list", None, {"X-Forwarded-For": "1.2.3.4"}, None),
        ("tools/list", None, None, ("10.0.0.5", 5000)),
        ("tools/list", None, None, None))
    assert (bad.status_code, proxied.status_code, remote.status_code, ok.status_code) == \
        (401, 404, 401, 200)
    M2 = _mcp(monkeypatch, key="")
    assert _rpc(M2, ("tools/list", None, None, None))[0].status_code == 404


def test_mcp_tool_error_is_data(monkeypatch):
    M = _mcp(monkeypatch)

    def boom():
        raise RuntimeError("нет базы")
    spec = T.ToolSpec("day_brief", "Выпуск дня", boom, "x")
    monkeypatch.setattr(T, "TOOLS", [spec])
    (r,) = _rpc(M, ("tools/call", {"name": "day_brief", "arguments": {}}, None, None))
    text = r.json()["result"]["content"][0]["text"]
    assert json.loads(text)["error"].startswith("инструмент упал")
