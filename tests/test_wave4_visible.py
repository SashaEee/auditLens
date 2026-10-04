"""Волна 4 аудита 03.10 «видно и не теряется»."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

STATIC = Path(__file__).resolve().parents[1] / "src" / "bank_audit" / "web" / "static"


def test_app_js_is_built_from_current_jsx():
    """КАР-01: страница грузит предсобранный app.js — он собран из текущего app.jsx."""
    sha = hashlib.sha256((STATIC / "app.jsx").read_bytes()).hexdigest()
    head = (STATIC / "app.js").open(encoding="utf-8").readline()
    assert f"sha256 {sha}" in head, "app.jsx изменён без пересборки: node scripts/build_frontend.js"


def test_index_serves_built_bundle_without_babel(tmp_path, monkeypatch):
    from bank_audit.web import app as A
    html = A._index_html_with_bust()
    assert '<script src="/static/app.js?v=' in html
    assert "babel.min.js" not in html and "text/babel" not in html
    # сборка не совпала с исходником — старый путь с Babel, а не устаревший UI
    (tmp_path / "index.html").write_text((STATIC / "index.html").read_text(encoding="utf-8"),
                                         encoding="utf-8")
    (tmp_path / "app.jsx").write_text("const x = <b/>;", encoding="utf-8")
    (tmp_path / "app.js").write_text("// Собрано из app.jsx (sha256 deadbeef)\n", encoding="utf-8")
    monkeypatch.setattr(A, "STATIC_DIR", tmp_path)
    A._BUILT_SHA.clear()
    html = A._index_html_with_bust()
    assert "text/babel" in html and "/static/app.jsx?v=" in html
    A._BUILT_SHA.clear()


def test_ops_alerts_once_a_day_to_owner(monkeypatch):
    """ПЛТ-01: сбои эксплуатации — письмом владельцу, одно в сутки на событие;
    без журнала отправок не шлём вовсе (иначе письмо каждые 30 минут)."""
    from bank_audit.notifier import alerts as AL
    from bank_audit.web import mailer
    ev = [{"key": "ingest_stale", "title": "Сбор тарифов не обновлял данные 40 ч", "detail": "…"}]
    sent, marked, today = [], [], set()
    monkeypatch.setattr(AL, "ops_events", lambda: ev)
    monkeypatch.setattr(AL, "_sent_today", lambda keys: today & set(keys))
    monkeypatch.setattr(AL, "_mark_sent", lambda keys: (marked.extend(keys), today.update(keys)))
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda to, mail, **k: sent.append((to, mail["subject"])))
    monkeypatch.setenv("ALERTS_TO", "owner@example.com")
    r = AL.run_once()
    assert r["sent"] == 1 and sent == [("owner@example.com", "AuditLens: Сбор тарифов не обновлял данные 40 ч")]
    assert marked == ["ingest_stale"]
    assert AL.run_once()["sent"] == 0 and len(sent) == 1          # сегодня уже слали
    assert AL.run_once(force=True)["sent"] == 1                    # «Отправить сейчас»

    def broken(keys):
        raise RuntimeError("no table")
    monkeypatch.setattr(AL, "_sent_today", broken)
    assert AL.run_once()["sent"] == 0


def test_report_run_status_is_logged(monkeypatch):
    """ПУЛ-02: итог прогона (готов / остановлен / сорвался) и время — в событии
    ai_run_end и в payload отчёта."""
    import asyncio
    import json
    from bank_audit.web import app as A
    from bank_audit.web import userdata
    events, saved = [], []
    monkeypatch.setattr(userdata, "log_event", lambda u, k, p=None: events.append((k, p)))
    monkeypatch.setattr(userdata, "add_message", lambda *a, **k: None)
    monkeypatch.setattr(userdata, "parse_query_signals", lambda q: {})
    monkeypatch.setattr(userdata, "save_report", lambda *a, **k: saved.append(k["payload"]) or 7)
    monkeypatch.setattr(userdata, "count_reports", lambda u: 1)
    monkeypatch.setattr(userdata, "title_session_from_report", lambda *a: None)

    async def inner(items):
        for it in items:
            yield json.dumps(it, ensure_ascii=False)

    async def run(items, stop_after=None):
        out = []
        gen = A._persisting_stream(inner(items), "u", 1, "вопрос")
        async for ev in gen:
            out.append(ev)
            if stop_after is not None and len(out) >= stop_after:
                await gen.aclose()
                break
        return out
    body = "Отчёт. " * 200
    asyncio.run(run([{"type": "mode", "value": "deep"}, {"type": "report_title", "title": "Т"},
                     {"type": "text", "chunk": body}, {"type": "done"}]))
    assert events[-1][0] == "ai_run_end" and events[-1][1]["status"] == "ok"
    assert saved[-1]["status"] == "ok" and "elapsed_s" in saved[-1]
    asyncio.run(run([{"type": "mode", "value": "deep"}, {"type": "report_title", "title": "Т"},
                     {"type": "text", "chunk": body}, {"type": "text", "chunk": "ещё"}], stop_after=4))
    assert events[-1][1]["status"] == "stopped" and saved[-1]["status"] == "stopped"
    asyncio.run(run([{"type": "mode", "value": "deep"},
                     {"type": "text", "chunk": "⚠ Не удалось построить план"}, {"type": "done"}]))
    assert events[-1][1]["status"] == "failed"
