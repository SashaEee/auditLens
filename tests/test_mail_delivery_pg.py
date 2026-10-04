"""Своя почта и рассылка писем — на живом Postgres (SQL здесь — главное: захват
событий, «раз в день» по уникальному индексу, ON CONFLICT).

Запуск: пустая база и MAIL_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db pytest
tests/test_mail_delivery_pg.py — миграции 014/083/087/088 тест накатывает сам (они
идемпотентны). Без переменной тесты пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

from bank_audit import db
from bank_audit.config import ROOT
from bank_audit.web import mail_delivery as MD
from bank_audit.web import mailer, notices, userdata

PG = os.getenv("MAIL_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: MAIL_PG_TEST_URL")

MON_9 = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)         # понедельник, 09:00 МСК


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        for f in ("014_personalization", "016_telemetry", "083_app_notice", "087_user_email",
                  "088_user_email_confirm"):
            cur.execute((ROOT / "migrations" / f"{f}.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    with eng.begin() as c:
        for t in ("app_notice", "app_email_verify", "app_mail_log", "usage_event", "app_user"):
            c.execute(text(f"DELETE FROM {t}"))
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    for k in ("MAIL_ENABLED", "MAIL_PAUSED", "MAIL_TEST_TO", "DIGEST_HOLIDAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAIL_CORP_DOMAINS", "corp.example.ru")
    monkeypatch.setenv("APP_BASE_URL", "https://al.example")
    yield eng
    eng.dispose()


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(mailer, "configured", lambda: True)

    def fake_send(to, mail, *, bulk=False, consented=False):
        if not mailer.allowed(to, consented=consented):
            raise mailer.MailError("нельзя")
        out.append({"to": to, "mail": mail, "consented": consented, "bulk": bulk})
        return "<id@test>"
    monkeypatch.setattr(mailer, "send", fake_send)
    return out


def _sql(sql: str, p: dict | None = None):
    from sqlalchemy import text
    with db.session() as s:
        r = s.execute(text(sql), p or {})
        return r.mappings().all() if r.returns_rows else None


def _code(mail: dict) -> str:
    return re.search(r"Код подтверждения: (\d{6})", mail["text"]).group(1)


def test_address_is_confirmed_by_code_and_not_overwritten_by_login_system(pg, sent):
    st = MD.start("u1", " Anna@Example.ORG ", "Анна Смирнова")
    assert st["pending"]["email"] == "anna@example.org" and not st["pending"]["corporate"]
    assert st["email"] is None and len(sent) == 1 and sent[0]["consented"]
    assert "Анна, здравствуйте!" in sent[0]["mail"]["html"]
    code = _code(sent[0]["mail"])
    with pytest.raises(MD.MailUserError) as e:                # повтор раньше минуты
        MD.start("u1", "anna@example.org")
    assert e.value.status == 429
    with pytest.raises(MD.MailUserError, match="осталось 4 попытки"):
        MD.confirm("u1", "000000" if code != "000000" else "111111")
    st = MD.confirm("u1", f"{code[:3]} {code[3:]}", "Анна Смирнова")     # пробел из письма не мешает
    assert (st["email"], st["source"], st["active"], st["pending"]) == ("anna@example.org", "user", True, None)
    welcome = sent[-1]
    assert welcome["to"] == "anna@example.org" and "без подробностей" in welcome["mail"]["html"]
    assert _sql("SELECT kind FROM app_mail_log ORDER BY id") == [{"kind": "verify"}, {"kind": "welcome"}]
    # адрес, указанный вручную, система входа не перезаписывает…
    userdata.touch_user("u1", "Анна Смирнова", email="anna.s@corp.example.ru")
    assert MD.address("u1") == {"email": "anna@example.org", "source": "user"}
    # …и после отключения не возвращает сама
    assert MD.remove("u1")["email"] is None
    userdata.touch_user("u1", None, email="anna.s@corp.example.ru")
    assert MD.address("u1") is None
    # а тем, кто ничего не указывал, адрес из системы входа записывается
    userdata.touch_user("u2", "Павел Орлов", email="p.orlov@corp.example.ru")
    assert MD.address("u2") == {"email": "p.orlov@corp.example.ru", "source": "sso"}
    assert MD.state("u2")["active"] is False                  # без MAIL_ENABLED=1 писем нет


def test_five_wrong_codes_burn_the_code(pg, sent):
    MD.start("u1", "anna@corp.example.ru")
    code = _code(sent[0]["mail"])
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(4):
        with pytest.raises(MD.MailUserError):
            MD.confirm("u1", wrong)
    with pytest.raises(MD.MailUserError, match="пять раз"):
        MD.confirm("u1", wrong)
    with pytest.raises(MD.MailUserError, match="Сначала"):
        MD.confirm("u1", code)                                # правильный уже не поможет
    assert MD.address("u1") is None


def test_one_address_cannot_be_flooded_by_many_people(pg, sent):
    for i in range(5):
        MD.start(f"u{i}", "victim@example.org")
    with pytest.raises(MD.MailUserError, match="сегодня уже"):
        MD.start("u9", "victim@example.org")
    assert len(sent) == 5


def _notice(u, kind, minutes_ago, *, link="case:12:talk:5", ref=None, read=False, count=1):
    _sql("""INSERT INTO app_notice (username, kind, title, actor, link, ref, count, created_at, updated_at, read_at)
            VALUES (:u, :k, :t, 'colleague', :l, CAST(:r AS jsonb), :c, :ts, :ts, :rd)""",
         {"u": u, "k": kind, "t": f"{kind} в деле «Карты»", "l": link, "c": count,
          "r": json.dumps(ref or {"case": "Карты", "snippet": "@Анна посмотрите выписку"}, ensure_ascii=False),
          "ts": MON_9 - timedelta(minutes=minutes_ago), "rd": MON_9 if read else None})


def _users():
    _sql("""INSERT INTO app_user (username, display_name, email, email_source) VALUES
            ('colleague', 'Ирина Котова', NULL, NULL),
            ('u1', 'Анна Смирнова', 'anna@corp.example.ru', 'user'),
            ('u2', 'Павел Орлов', 'pavel@example.org', 'user'),
            ('u3', 'Из системы входа', 's@corp.example.ru', 'sso')""")


def test_tick_sends_personal_now_and_the_rest_in_the_morning(pg, sent, monkeypatch):
    _users()
    _notice("u1", "case_mention", 15)                         # личное, 10 минут прошло → сразу
    _notice("u1", "case_mention", 2)                          # слишком свежее → в сводку
    _notice("u1", "case_items", 20, link="case:12", count=3)  # не личное → в сводку
    _notice("u1", "case_mention", 30, read=True)              # прочитано → никуда
    _notice("u1", "report_shared", 3 * 24 * 60, link="report:4", ref={"report": "Ставки"})   # → в сводку
    _notice("u1", "case_items", 10 * 24 * 60, link="case:12")  # старше недели → никуда
    _notice("u2", "case_reply", 40)                           # личная почта → без подробностей
    _notice("u3", "case_mention", 40)                         # адрес из системы входа → не пишем
    r = MD.tick(MON_9)
    assert r == {"users": 2, "instant": 2, "digest": 1, "errors": 0}
    by = {(x["to"], "digest" if x["bulk"] else "instant"): x["mail"] for x in sent}
    inst1, dig1, inst2 = (by[("anna@corp.example.ru", "instant")], by[("anna@corp.example.ru", "digest")],
                          by[("pavel@example.org", "instant")])
    assert "Карты" in inst1["html"] and "посмотрите выписку" in inst1["html"]
    assert dig1["subject"].startswith("Сводка AuditLens за 5 октября: 3 события") and "Анна, доброе утро." in dig1["text"]
    assert "Карты" not in inst2["html"] and "выписку" not in inst2["html"] and "Ирина" not in inst2["html"]
    assert "Ответ на ваше сообщение в деле" in inst2["subject"] and "№ 12" in inst2["subject"]
    # каждое событие — один раз: второй проход ничего не шлёт
    assert MD.tick(MON_9 + timedelta(minutes=1)) == {"users": 2, "instant": 0, "digest": 0, "errors": 0}
    left = _sql("SELECT username, kind FROM app_notice WHERE emailed_at IS NULL AND read_at IS NULL ORDER BY 1, 2")
    assert [tuple(x.values()) for x in left] == [("u1", "case_items"), ("u3", "case_mention")]
    # сводка — раз в день, даже если днём появилось новое
    _notice("u1", "case_items", -60, link="case:14")
    assert MD.tick(MON_9 + timedelta(hours=2))["digest"] == 0
    # адреса из системы входа — после MAIL_ENABLED=1
    monkeypatch.setenv("MAIL_ENABLED", "1")
    assert MD.tick(MON_9 + timedelta(hours=2, minutes=5))["users"] == 3


def test_no_morning_digest_on_weekends_and_settings_respected(pg, sent):
    _users()
    _notice("u1", "case_items", 20, link="case:12")
    sat = MON_9 - timedelta(days=2)
    _sql("UPDATE app_notice SET updated_at = :t", {"t": sat - timedelta(minutes=20)})
    assert MD.tick(sat)["digest"] == 0
    _sql("""UPDATE app_user SET prefs = '{"mail": {"digest": false, "instant": false}}' WHERE username = 'u1'""")
    _notice("u1", "case_mention", 15)
    assert MD.tick(MON_9) == {"users": 2, "instant": 0, "digest": 0, "errors": 0}


def test_failed_send_releases_events_for_the_next_tick(pg, sent, monkeypatch):
    _users()
    _notice("u1", "case_mention", 15)

    def boom(*a, **k):
        raise mailer.MailError("сервер недоступен")
    monkeypatch.setattr(mailer, "send", boom)
    r = MD.tick(MON_9)
    assert r["errors"] == 2 and r["instant"] == r["digest"] == 0
    assert _sql("SELECT count(*) AS n FROM app_notice WHERE emailed_at IS NOT NULL") == [{"n": 0}]
    # место сводки освобождено — следующий тик попробует снова (у u2 событий нет — «нечего отправлять»)
    assert _sql("SELECT username, error FROM app_mail_log WHERE kind = 'digest'") == [
        {"username": "u2", "error": "нечего отправлять"}]
    assert _sql("SELECT ok, error FROM app_mail_log WHERE kind = 'instant'") == [
        {"ok": False, "error": "сервер недоступен"}]


def test_new_activity_on_an_emailed_notice_is_emailed_again(pg, sent):
    _users()
    notices.notify(["u1"], "ticket", actor=None, link="inbox:7", ref={"no": 7, "reply": True})
    _sql("UPDATE app_notice SET emailed_at = now()")
    notices.notify(["u1"], "ticket", actor=None, link="inbox:7", ref={"no": 7, "status_label": "Сделано"})
    assert _sql("SELECT count(*) AS n, bool_and(emailed_at IS NULL) AS fresh FROM app_notice") == [
        {"n": 1, "fresh": True}]


def test_pulse_shows_who_connected_mail_without_counting_service_accounts(pg, sent):
    from bank_audit.web import telemetry
    _users()                               # u1 — Sigma, u2 — личная, u3 — из системы входа
    _sql("""INSERT INTO app_user (username, display_name, email, email_source, prefs) VALUES
            ('svc', 'Служебная', 'svc@corp.example.ru', 'user', '{"pulse_hidden": true}')""")
    _sql("""INSERT INTO usage_event (username, kind, page, payload) VALUES
            ('u1', 'page_view', 'overview', '{}'), ('u2', 'page_view', 'overview', '{}'),
            ('colleague', 'page_view', 'overview', '{}'),
            ('colleague', 'ui', 'overview', '{"action": "mail_promo", "step": "shown"}'),
            ('colleague', 'ui', 'overview', '{"action": "mail_promo", "step": "later"}'),
            ('u1', 'ui', 'overview', '{"action": "mail_promo", "step": "shown"}'),
            ('u1', 'ui', 'overview', '{"action": "mail_promo", "step": "connect"}')""")
    _sql("""INSERT INTO app_mail_log (username, kind, to_addr, ok) VALUES
            ('u1', 'instant', 'anna@corp.example.ru', true), ('u1', 'verify', 'anna@corp.example.ru', true),
            ('u2', 'digest', 'pavel@example.org', false)""")
    MD.start("colleague", "irina@example.org")                    # ждёт кода
    b = telemetry._mail_brief(14, telemetry.excluded(None))
    assert (b["connected"], b["sigma"], b["private"], b["sso"]) == (3, 1, 1, 1)
    assert (b["active"], b["active_connected"], b["pending"]) == (3, 2, 1)
    assert (b["sent"], b["codes"], b["failed"]) == (1, 2, 1)     # коды: u1 + colleague
    assert b["promo"] == {"shown": 2, "connect": 1, "later": 1}
    rows = {r["username"]: r for r in b["people"]}
    assert rows["u1"]["kind"] == "sigma" and rows["u1"]["sent"] == 1 and rows["u1"]["active"]
    assert rows["u2"]["kind"] == "private" and rows["u2"]["failed"] == 1
    assert rows["svc"]["excluded"] and not rows["u1"]["excluded"]
