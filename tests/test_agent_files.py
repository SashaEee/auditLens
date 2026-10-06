"""Файлы от ИИ-помощника: агент отдаёт файл командой al-share, в ответе — метка
[[FILE:<id>]], пользователь видит «Скачать».

Чистые функции и доступ — всегда; цикл «принять → привязать → скачать» — на живом
Postgres: AGENT_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db (миграцию 096
тест накатывает сам). Без переменной эти тесты пропускаются.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.ai import agent_files as AF  # noqa: E402

PG = os.getenv("AGENT_PG_TEST_URL")
ID = "0123456789abcdef0123456789abcdef"


def test_clean_name_and_types():
    assert AF.clean_name("/root/x/Жалобы\u0000 ипотека.xlsx") == "Жалобы ипотека.xlsx"
    assert AF.clean_name("..\\..\\a.csv") == "a.csv"
    long = AF.clean_name("я" * 200 + ".xlsx")
    assert len(long) <= 120 and long.endswith(".xlsx")
    with pytest.raises(AF.FileError):
        AF.store("script.sh", b"echo")             # тип не из списка — до базы
    with pytest.raises(AF.FileError):
        AF.store("a.xlsx", b"")


def test_markers_found_and_replaced():
    text = f"Готово.\n[[FILE:{ID}]]\nЕщё раз [[FILE:{ID}]] и чужая [[FILE:{'f' * 32}]]"
    assert AF.marker_ids(text) == [ID, "f" * 32]
    out = AF.replace_markers(text, [{"id": ID, "name": "Жалобы.xlsx"}])
    assert "«Жалобы.xlsx»" in out and "[[FILE:" not in out


def test_upload_access_like_mcp(monkeypatch):
    from bank_audit.ai import mcp_server as M
    monkeypatch.setattr(M, "MCP_KEY", "k")
    assert M.access_status({"authorization": "Bearer k"}, "127.0.0.1") == 200
    assert M.access_status({"authorization": "Bearer x"}, "127.0.0.1") == 401
    assert M.access_status({"authorization": "Bearer k", "x-real-ip": "1.2.3.4"}, "127.0.0.1") == 404
    assert M.access_status({"authorization": "Bearer k"}, "10.0.0.5") == 401
    monkeypatch.setattr(M, "MCP_KEY", "")
    assert M.access_status({"authorization": "Bearer "}, "127.0.0.1") == 404


def test_hermes_adapter_reports_file_ids(monkeypatch):
    """Метки в ответе агента → событие files с номерами (до run_meta)."""
    from bank_audit.ai import hermes_quick as H

    async def fake_run(*a, **k):
        yield {"tool": "terminal"}
        yield {"final": f"**Готово:** собрал выгрузку на 42 жалобы.\n\n[[FILE:{ID}]]\n\nВнутри два листа."}

    monkeypatch.setattr(H, "_one_run", fake_run)

    async def collect():
        return [json.loads(x) async for x in H.stream_quick_hermes("выгрузи в Excel", [])]
    evs = asyncio.run(collect())
    kinds = [e["type"] for e in evs]
    assert kinds.index("files") < kinds.index("run_meta")
    assert next(e for e in evs if e["type"] == "files")["ids"] == [ID]


@pytest.fixture
def pg(monkeypatch):
    if not PG:
        pytest.skip("нужна живая база: AGENT_PG_TEST_URL")
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    from bank_audit.config import ROOT
    from bank_audit.web import app  # noqa: F401 — импорт приложения сам настраивает базу: до подмены
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        for f in ("014_personalization", "096_agent_file"):
            cur.execute((ROOT / "migrations" / f"{f}.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    with eng.begin() as c:
        for t in ("agent_file", "report_share", "report"):
            c.execute(text(f"DELETE FROM {t}"))
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    yield eng
    eng.dispose()


def test_store_claim_get_cycle(pg):
    f = AF.store("/root/Жалобы на ипотеку.xlsx", b"PK\x03\x04data")
    assert f["marker"] == f"[[FILE:{f['id']}]]" and f["name"] == "Жалобы на ипотеку.xlsx"
    assert AF.get(f["id"], "anna") is None                         # ещё ничей — не скачать
    got = AF.claim([f["id"], "nope"], "anna", 7)
    assert got == [{"id": f["id"], "name": "Жалобы на ипотеку.xlsx", "size": 8,
                    "mime": AF.TYPES["xlsx"]}]
    assert bytes(AF.get(f["id"], "anna")["data"]) == b"PK\x03\x04data"
    assert AF.get(f["id"], "pavel") is None                        # чужому — нет
    assert AF.get(f["id"], "pavel", is_admin=True) is not None
    assert AF.claim([f["id"]], "pavel") == []                      # чужой файл не перепривязать
    assert AF.claim([f["id"]], "anna")[0]["id"] == f["id"]         # свой — повторно можно


def test_persisting_stream_claims_and_saves(pg, monkeypatch):
    """Обёртка стрима: files → привязка и имена; в истории — «имя» вместо метки."""
    from bank_audit.web import app as A
    f = AF.store("выгрузка.csv", b"a;b\n1;2\n")
    saved = {}
    monkeypatch.setattr(A.userdata, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(A.userdata, "save_report", lambda *a, **k: saved.setdefault("payload", k.get("payload")) and 1)
    monkeypatch.setattr(A.userdata, "add_message",
                        lambda sid, role, body, meta: saved.update(body=body, meta=meta))

    async def inner():
        yield json.dumps({"type": "mode", "value": "quick"})
        yield json.dumps({"type": "text", "chunk": f"Файл готов:\n[[FILE:{f['id']}]]"})
        yield json.dumps({"type": "files", "ids": [f["id"]]})
        yield json.dumps({"type": "done"})

    async def collect():
        return [json.loads(x) async for x in A._persisting_stream(inner(), "anna", 5, "выгрузи")]
    evs = asyncio.run(collect())
    files_ev = next(e for e in evs if e["type"] == "files")
    assert files_ev["files"][0]["name"] == "выгрузка.csv"
    assert "«выгрузка.csv»" in saved["body"] and "[[FILE:" not in saved["body"]
    assert saved["payload"]["files"][0]["id"] == f["id"]   # короткий ответ с файлом — тоже отчёт
    assert saved["meta"]["files"][0]["id"] == f["id"]


def test_shared_report_opens_file_to_colleague(pg):
    """Файл открывается тем, кому открыт отчёт с ним: поделились лично или всем;
    постороннему — нет; после отзыва — нет."""
    from sqlalchemy import text
    f = AF.store("Жалобы.xlsx", b"PK\x03\x04")
    AF.claim([f["id"]], "anna")
    with pg.begin() as c:
        rid = c.execute(text("""INSERT INTO report (username, question, body, payload)
                                VALUES ('anna', 'q', 'b', CAST(:p AS jsonb)) RETURNING report_id"""),
                        {"p": json.dumps({"mode": "quick", "files": [{"id": f["id"], "name": "Жалобы.xlsx"}]})}).scalar()
    assert AF.get(f["id"], "pavel") is None
    with pg.begin() as c:
        c.execute(text("INSERT INTO report_share (report_id, owner, shared_with) VALUES (:r, 'anna', 'pavel')"),
                  {"r": rid})
    assert AF.get(f["id"], "pavel") is not None
    assert AF.get(f["id"], "olga") is None
    with pg.begin() as c:
        c.execute(text("UPDATE report_share SET revoked_at = now() WHERE report_id = :r"), {"r": rid})
        c.execute(text("INSERT INTO report_share (report_id, owner, shared_with) VALUES (:r, 'anna', NULL)"),
                  {"r": rid})
    assert AF.get(f["id"], "olga") is not None                 # поделились со всеми
