"""Письма об обращениях (web/inbox_mail.py): шаблон, снимки во вложении, правило «кому»,
тик рассылки на живой базе (MAIL_PG_TEST_URL, как tests/test_mail_delivery_pg.py)."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bank_audit import db
from bank_audit.web import inbox_mail as IM
from bank_audit.web import mailer

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _t(**kw):
    t = {"ticket_id": 13, "username": "u1", "author_name": "Иван Петров", "kind": "bug", "kind_label": "Ошибка",
         "section": "loophole", "section_label": "Аудит уязвимостей", "body": "Не грузится карточка уязвимости",
         "context": {"url": "#loophole", "browser": "Chrome 140 · Windows", "screen": "1440×900",
                     "errors": ["12:00 TypeError: x is undefined"]},
         "created_at": datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)}
    t.update(kw)
    return t


def test_render_corporate_has_text_images_and_reply_button(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://al.example")
    img = [{"cid": "ticket-13-1@auditlens", "data": PNG, "subtype": "png"}]
    m = IM.render(_t(), msg=None, private=False, team=True, images=img)
    assert "Обращение № 13 · Ошибка · Аудит уязвимостей" == m["subject"]
    assert "Не грузится карточка уязвимости" in m["html"] and "Иван Петров" in m["html"]
    assert "cid:ticket-13-1@auditlens" in m["html"] and m["images"][0][0] == "ticket-13-1@auditlens"
    assert "#pulse?tab=inbox&amp;ticket=13" in m["html"] and "Ответить в «Пульсе»" in m["html"]
    assert "TypeError" in m["html"] and "Chrome 140" in m["text"]


def test_render_section_owner_without_pulse_link_and_private_redacted(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://al.example")
    m = IM.render(_t(), msg=None, private=False, team=False, images=[])
    assert "Ответить в «Пульсе»" not in m["html"] and "по разделу «Аудит уязвимостей»" in m["html"]
    p = IM.render(_t(), msg=None, private=True, team=False, images=[])
    assert "Не грузится" not in p["html"] and "Не грузится" not in p["text"] and "Иван" not in p["html"]
    assert p["subject"] == "Обращение № 13 в AuditLens"


def test_render_follow_up_message():
    msg = {"msg_id": 40, "body": "Ещё и кнопка не жмётся", "created_at": datetime(2026, 11, 5, 13, tzinfo=timezone.utc)}
    m = IM.render(_t(), msg=msg, private=False, team=True, images=[])
    assert m["subject"].startswith("Новое сообщение в обращении № 13")
    assert "Ещё и кнопка не жмётся" in m["html"] and "Само обращение" in m["html"]


def test_mailer_attaches_ticket_images_inline(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "bot@example.ru")
    mail = {"subject": "s", "text": "t", "html": '<img src="cid:a@x">',
            "images": [("a@x", PNG, "png"), ("unused@x", PNG, "png")]}
    msg = mailer.build("to@example.ru", mail)
    cids = [p["Content-ID"] for p in msg.walk() if p.get("Content-ID")]
    assert cids == ["<a@x>"]                       # картинку без ссылки из письма не вкладываем


def test_routes_default_owner_and_cleaning(monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "owner-1")
    monkeypatch.setattr(IM, "_get", lambda k: None)
    assert IM.routes() == {"all": ["owner-1"], "sections": {}, "saved": False}
    monkeypatch.setattr(IM, "_get", lambda k: {"all": "owner-1, ;owner-1", "sections": {"loophole": ["a@b.ru", ""],
                                                                                          "x": []}})
    assert IM.routes() == {"all": ["owner-1"], "sections": {"loophole": ["a@b.ru"]}, "saved": True}


# ── тик на живой базе ────────────────────────────────────────────────────────

PG = os.getenv("MAIL_PG_TEST_URL")


@pytest.fixture
def pg(monkeypatch):
    if not PG:
        pytest.skip("нужна живая база: MAIL_PG_TEST_URL")
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        for f in ("014_personalization", "016_telemetry", "081_user_ticket", "083_app_notice", "087_user_email",
                  "088_user_email_confirm", "111_app_setting"):
            cur.execute((ROOT / "migrations" / f"{f}.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    with eng.begin() as c:
        for t in ("user_ticket_file", "user_ticket_msg", "user_ticket", "app_notice", "app_mail_log",
                  "app_setting", "app_user"):
            c.execute(text(f"DELETE FROM {t}"))
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    for k in ("MAIL_ENABLED", "MAIL_PAUSED", "MAIL_TEST_TO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAIL_CORP_DOMAINS", "corp.example.ru")
    monkeypatch.setenv("APP_BASE_URL", "https://al.example")
    monkeypatch.setenv("ADMIN_USERS", "owner-1")
    monkeypatch.setenv("PULSE_USERS", "dev-3")
    yield eng
    eng.dispose()


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(mailer, "configured", lambda: True)

    def fake_send(to, mail, *, bulk=False, consented=False):
        out.append({"to": to, "mail": mail})
        return "<id@test>"
    monkeypatch.setattr(mailer, "send", fake_send)
    return out


def _sql(sql, p=None):
    from sqlalchemy import text
    with db.session() as s:
        r = s.execute(text(sql), p or {})
        try:
            return r.scalar()
        except Exception:  # noqa: BLE001
            return None


def test_tick_sends_every_ticket_to_owner_and_section_route(pg, sent):
    _sql("""INSERT INTO app_user (username, display_name, email, email_source)
            VALUES ('owner-1', 'Владелец', 'owner@corp.example.ru', 'user'),
                   ('dev-2', 'Разработчик', NULL, NULL), ('u1', 'Иван Петров', NULL, NULL)""")
    _sql("INSERT INTO user_ticket (username, kind, section, section_label, body) VALUES ('u1','bug','reviews','Аудит отзывов','старое')")
    assert IM.tick()["started"]                                         # старое обращение не рассылаем
    assert sent == []
    IM.set_routes(["owner-1"], {"loophole": ["dev-2", "dev2@corp.example.ru", "dev-3 <Dev3@corp.example.ru>"]},
                  "owner-1")
    tid = _sql("""INSERT INTO user_ticket (username, kind, section, section_label, body, context)
                  VALUES ('u1','bug','loophole','Аудит уязвимостей','Не грузится', '{"url":"#loophole"}')
                  RETURNING ticket_id""")
    _sql("INSERT INTO user_ticket_file (ticket_id, mime, data) VALUES (:t, 'image/png', :d)", {"t": tid, "d": PNG})
    _sql("""INSERT INTO app_notice (username, kind, title, link, ref)
            VALUES ('owner-1', 'team_ticket', 'Обращение', '#pulse', CAST(:r AS jsonb))""",
         {"r": '{"no": %d}' % tid})
    r = IM.tick()
    assert r["tickets"] == 1 and r["mails"] == 3
    to = sorted(x["to"] for x in sent)
    assert to == ["dev2@corp.example.ru", "dev3@corp.example.ru", "owner@corp.example.ru"]   # dev-2 без почты — пропущен
    d3 = next(x["mail"] for x in sent if x["to"].startswith("dev3"))
    assert "Ответить в «Пульсе»" in d3["html"] and "по разделу «Аудит уязвимостей»" in d3["html"]   # dev-3 в PULSE_USERS
    owner_mail = next(x["mail"] for x in sent if x["to"].startswith("owner"))
    assert "Ответить в «Пульсе»" in owner_mail["html"] and owner_mail["images"]
    assert _sql("SELECT emailed_at IS NOT NULL FROM app_notice WHERE kind = 'team_ticket'")   # колокольчик не дублирует
    # дописал автор — письмо; ответ команды — нет; повторный тик ничего не шлёт
    _sql("INSERT INTO user_ticket_msg (ticket_id, author, role, body) VALUES (:t, 'owner-1', 'team', 'Смотрим')", {"t": tid})
    _sql("INSERT INTO user_ticket_msg (ticket_id, author, role, body) VALUES (:t, 'u1', 'user', 'Ещё деталь')", {"t": tid})
    sent.clear()
    r = IM.tick()
    assert r["messages"] == 1 and len(sent) == 3 and "Ещё деталь" in sent[0]["mail"]["html"]
    sent.clear()
    assert IM.tick()["mails"] == 0 and sent == []
    d = IM.describe()
    assert d["sections"]["loophole"][0]["ok"] is False and d["all"][0]["email"] == "owner@corp.example.ru"
