"""Плашка «В облаках турбулентность» (web/banner.py): включает владелец, видят все."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.web import app as A  # noqa: E402 — до подмены базы: импорт приложения её сбрасывает
from bank_audit.web import banner as B  # noqa: E402

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def lite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    eng = create_engine(f"sqlite:///{tmp_path / 'b.db'}", future=True)
    with eng.begin() as c:
        c.execute(text("""CREATE TABLE app_banner (banner_id TEXT PRIMARY KEY,
            active BOOLEAN NOT NULL DEFAULT 0, until TIMESTAMP, title TEXT, body TEXT,
            updated_by TEXT, updated_at TIMESTAMP)"""))
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    yield eng
    eng.dispose()


def test_no_row_means_no_banner(lite):
    st = B.state(NOW)
    assert st["active"] is False and st["on"] is False


def test_missing_table_means_no_banner(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, future=True))
    assert B.state(NOW)["active"] is False


def test_on_until_off(lite):
    st = B.set_state(active=True, days=None, by="owner", now=NOW)
    assert st["active"] and st["until"] is None and st["v"]

    st = B.set_state(active=True, days=3, by="owner", now=NOW)
    assert st["active"] and st["until"].startswith("2026-10-15")
    assert B.state(NOW + timedelta(days=2, hours=23))["active"] is True
    late = B.state(NOW + timedelta(days=3, minutes=1))
    assert late["on"] is True and late["active"] is False          # срок вышел — плашки нет

    st = B.set_state(active=False, days=7, by="owner", now=NOW)
    assert st["active"] is False and st["until"] is None


def test_days_and_text_are_bounded(lite):
    st = B.set_state(active=True, days=999, by="owner", title="  Заголовок \n с переносом ",
                     body="x" * 1000, now=NOW)
    assert st["until"].startswith((NOW + timedelta(days=B.MAX_DAYS)).date().isoformat())
    assert st["title"] == "Заголовок с переносом" and len(st["body"]) == B.MAX_TEXT


def test_reenable_changes_version(lite):
    v1 = B.set_state(active=True, days=None, by="owner", now=NOW)["v"]
    B.set_state(active=False, days=None, by="owner", now=NOW + timedelta(hours=1))
    v2 = B.set_state(active=True, days=None, by="owner", now=NOW + timedelta(hours=2))["v"]
    assert v1 != v2                     # свёрнутая плашка снова раскроется у всех


def test_api_owner_only(lite, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ADMIN_USERS", "owner-1")
    c = TestClient(A.app)
    hdr = lambda u: {"X-Authentik-Username": u, "X-Authentik-Name": u}  # noqa: E731
    assert c.get("/api/banner", headers=hdr("user-1")).json() == {"banner": None}
    assert c.post("/api/admin/banner", json={"active": True}, headers=hdr("user-1")).status_code == 403
    r = c.post("/api/admin/banner", json={"active": True, "days": 3}, headers=hdr("owner-1"))
    assert r.status_code == 200 and r.json()["active"] is True
    b = c.get("/api/banner", headers=hdr("user-1")).json()["banner"]
    assert b and b["id"] == "turbulence" and b["until"]
