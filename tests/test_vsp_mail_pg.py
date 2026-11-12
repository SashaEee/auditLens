"""Ежедневное письмо раздела «Аудит сети ВСП» — на живом Postgres: выпуск дня, «раз в день»,
«с прошлого письма», доступ, регионы, пример по кнопке.

Запуск: пустая база и MAIL_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db pytest
tests/test_vsp_mail_pg.py — миграции тест накатывает сам (они идемпотентны). Без переменной тесты
пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from bank_audit import db
from bank_audit.config import ROOT
from bank_audit.web import mail_delivery as MD
from bank_audit.web import mailer, userdata
from bank_audit.web import vsp_api as V
from bank_audit.web import vsp_mail as VM

PG = os.getenv("MAIL_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: MAIL_PG_TEST_URL")

MON = datetime(2026, 11, 16, 6, 5, tzinfo=timezone.utc)          # понедельник, 09:05 МСК
TUE = MON + timedelta(days=1)


def _sql(sql: str, p: dict | None = None):
    from sqlalchemy import text
    with db.session() as s:
        r = s.execute(text(sql), p or {})
        return r.mappings().all() if r.returns_rows else None


def _cand(kind, rid, *, recent=True, subject="CU", point="b1", **payload):
    base = {"city": "Чебоксары", "address": "УЛ. МИРА, Д. 5", "name": "ВСП № 8613/0123", "gosb": "8613",
            "date": "2026-11-15"}
    if kind == "story":
        base.update(severity=3, episode=f"Пересказ истории {rid}", head=f"Пересказ истории {rid}",
                    quote=f"Слова клиента об истории {rid}", what="Вклад подменили страхованием", score=5)
    elif kind == "wait":
        base.update(minutes=90, cause="мало окон", left=False, quote=f"Ждали полтора часа {rid}")
    else:
        base = {"id": "k8", "name": "Перевод пенсии", "status": "rising", "text": "Растёт: перевод пенсии",
                "accent": "за год 39 клиентов"}
    if "episode" in payload:
        payload.setdefault("head", payload["episode"])
    # способы действия идут в первый выпуск (как в candidates)
    return {"kind": kind, "rid": rid, "recent": recent or kind == "wave", "subject": None if kind == "wave" else subject,
            "gosb": "8613", "point": None if kind == "wave" else point, "payload": {**base, **payload}}


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        for f in ("014_personalization", "016_telemetry", "083_app_notice", "087_user_email",
                  "088_user_email_confirm", "095_mail_brief", "111_app_setting", "114_vsp_daily_mail",
                  "114_vsp_daily_mail"):                       # дважды: повтор миграции безопасен
            cur.execute((ROOT / "migrations" / f"{f}.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    with eng.begin() as c:
        for t in ("app_notice", "app_email_verify", "app_mail_log", "usage_event", "app_user", "vsp_mail_item"):
            c.execute(text(f"DELETE FROM {t}"))
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    for k in ("MAIL_ENABLED", "MAIL_PAUSED", "MAIL_TEST_TO", "DIGEST_HOLIDAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAIL_CORP_DOMAINS", "corp.example.ru")
    monkeypatch.setenv("APP_BASE_URL", "https://al.example")
    monkeypatch.setenv("VSP_DB_URL", "sqlite://")                # «база сборщика настроена»
    VM._ISSUE["day"] = None
    yield eng
    VM._ISSUE["day"] = None
    eng.dispose()


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(mailer, "configured", lambda: True)

    def fake_send(to, mail, *, bulk=False, consented=False):
        if not mailer.allowed(to, consented=consented):
            raise mailer.MailError("нельзя")
        out.append({"to": to, "mail": mail, "bulk": bulk})
        return "<id@test>"
    monkeypatch.setattr(mailer, "send", fake_send)
    return out


@pytest.fixture
def world(monkeypatch):
    """Что «видит» раздел и кому он открыт — без базы сборщика."""
    w = {"cands": [], "access": {"anna", "boris", "vera"}}
    monkeypatch.setattr(VM, "candidates", lambda now: list(w["cands"]))
    monkeypatch.setattr(V, "can_vsp", lambda u, n=None: u in w["access"])
    return w


def _user(u, email, name="Анна Смирнова", **vsp_mail):
    userdata.touch_user(u, name)
    _sql("UPDATE app_user SET email = :e, email_at = now(), email_source = 'user' WHERE username = :u",
         {"e": email, "u": u})
    if vsp_mail:
        VM.save(u, vsp_mail)


def test_settings_roundtrip(pg):
    _user("anna", "anna@corp.example.ru")
    st = VM.state("anna")
    assert st["on"] is False and st["email"] == "anna@corp.example.ru" and st["corporate"] and st["hour"] == 9
    st = VM.save("anna", {"on": True, "subjects": ["SPE", "LEN", "выдумка"], "wait": False})
    assert st["on"] and st["subjects"] == ["LEN", "SPE"] and st["heavy"] and not st["wait"] and st["since"]
    since = st["since"]
    assert VM.save("anna", {"wait": True})["since"] == since          # настройка не сбрасывает дату подписки
    assert VM.save("anna", {"heavy": False, "wait": False})["on"] is False    # присылать нечего — подписки нет
    assert VM.state("anna")["on"] is False
    # прочие настройки профиля не задеты
    userdata.update_prefs("anna", {"mail": {"brief": False}})
    VM.save("anna", {"on": True, "heavy": True})
    prefs = _sql("SELECT prefs FROM app_user WHERE username = 'anna'")[0]["prefs"]
    assert prefs["mail"] == {"brief": False} and prefs["vsp_mail"]["on"] is True


def test_issue_is_built_once_and_first_one_is_not_a_flood(pg, world):
    world["cands"] = [_cand("story", "s-old", recent=False), _cand("story", "s-new"),
                      _cand("wait", "w-old", recent=False), _cand("wait", "w-new"), _cand("wave", "k:8", recent=False)]
    r = VM.build_issue(MON)
    assert r["added"] == 3 and not r["cached"]
    days = {x["rid"]: x["day"] for x in _sql("SELECT rid, day FROM vsp_mail_item")}
    # первый выпуск: свежие истории и жалобы и то, что идёт по сети сейчас; старое помечено «уже было»
    assert days == {"s-old": VM.SEED, "s-new": date(2026, 11, 16), "w-old": VM.SEED, "w-new": date(2026, 11, 16),
                    "k:8": date(2026, 11, 16)}
    assert VM.build_issue(MON)["cached"]                              # второй раз за день — ничего
    VM._ISSUE["day"] = None                                           # приложение перезапустили
    assert VM.build_issue(MON)["added"] == 0                          # повтор безопасен: дублей нет
    world["cands"] += [_cand("story", "s-late", recent=False), _cand("wave", "m:0803", recent=False)]
    assert VM.build_issue(TUE)["added"] == 2                          # дальше «новое» — всё, чего не было
    assert len(VM.issue_rows(date(2026, 11, 16), date(2026, 11, 17))) == 2
    assert len(VM.issue_rows(date(2026, 11, 13), date(2026, 11, 17))) == 5      # «уже было» в письма не идёт


def test_daily_letter_once_a_day_and_only_new(pg, sent, world):
    _user("anna", "anna@corp.example.ru", on=True)
    _user("boris", "boris@corp.example.ru", "Борис Котов")           # не подписан
    world["cands"] = [_cand("story", "s1"), _cand("wait", "w1")]
    assert MD.tick(MON).get("vsp", 0) == 1 and len(sent) == 1
    m = sent[0]
    assert m["to"] == "anna@corp.example.ru" and m["bulk"]
    assert m["mail"]["subject"] == "Сеть ВСП · Чебоксары: вклад подменили страхованием — и 1 жалоба на ожидание"
    assert "Всего за выходные:" in m["mail"]["html"]                                                  # понедельник
    assert "Анна, доброе утро." in m["mail"]["html"] and "Пересказ истории s1" in m["mail"]["html"]
    assert MD.tick(MON + timedelta(minutes=5)).get("vsp", 0) == 0 and len(sent) == 1      # второй раз за день — нет
    log = _sql("SELECT kind, ok, n_items, error FROM app_mail_log WHERE kind = 'vsp_daily'")
    assert [dict(x) for x in log] == [{"kind": "vsp_daily", "ok": True, "n_items": 2, "error": None}]
    # назавтра нового нет — письма нет, в журнале «нечего отправлять»
    assert MD.tick(TUE).get("vsp", 0) == 0 and len(sent) == 1
    assert _sql("SELECT error FROM app_mail_log WHERE kind = 'vsp_daily' AND day = '2026-11-17'")[0]["error"] \
        == "нечего отправлять"
    # послезавтра — только то, чего не было в прошлом письме
    world["cands"] += [_cand("story", "s2"), _cand("wait", "w2", left=True)]
    wed = MON + timedelta(days=2)
    assert MD.tick(wed).get("vsp", 0) == 1
    html = sent[-1]["mail"]["html"]
    assert "Пересказ истории s2" in html and "Пересказ истории s1" not in html and "ушёл, не дождавшись" in html


def test_time_window_weekend_and_missed_days(pg, sent, world):
    _user("anna", "anna@corp.example.ru", on=True)
    world["cands"] = [_cand("story", "s1")]
    assert MD.tick(MON.replace(hour=5)).get("vsp", 0) == 0                   # 08:05 МСК — рано
    assert MD.tick(MON.replace(hour=11, minute=30)).get("vsp", 0) == 0       # 14:30 МСК — утреннее письмо уже не шлём
    assert MD.tick(MON - timedelta(days=1)).get("vsp", 0) == 0               # воскресенье
    assert not _sql("SELECT 1 FROM app_mail_log WHERE kind = 'vsp_daily'") and not sent
    # пропущенный день не теряется: во вторник в письме и вчерашнее, и сегодняшнее
    VM.build_issue(MON)
    world["cands"] += [_cand("wait", "w2")]
    assert MD.tick(TUE).get("vsp", 0) == 1
    html = sent[-1]["mail"]["html"]
    assert "Пересказ истории s1" in html and "Ждали полтора часа w2" in html
    assert "Всего за 2 дня:" in sent[-1]["mail"]["html"]


def test_regions_access_and_private_address(pg, sent, world):
    _user("anna", "anna@corp.example.ru", on=True, subjects=["SPE"])
    _user("boris", "boris@mail.example.org", "Борис Котов", on=True)
    _user("vera", "vera@corp.example.ru", "Вера Орлова", on=True)
    world["access"] = {"anna", "boris"}                               # у Веры раздел закрыли
    world["cands"] = [_cand("story", "s-cu"), _cand("story", "s-spb", subject="SPE", point="b7", city="Санкт-Петербург",
                                                    episode="Пересказ питерской истории")]
    assert MD.tick(MON).get("vsp", 0) == 2
    by = {m["to"]: m["mail"] for m in sent}
    assert set(by) == {"anna@corp.example.ru", "boris@mail.example.org"}          # Вере — ничего
    assert "Пересказ питерской истории" in by["anna@corp.example.ru"]["html"]
    assert "Пересказ истории s-cu" not in by["anna@corp.example.ru"]["html"]      # чужой регион не показываем
    private = by["boris@mail.example.org"]
    assert "Пересказ" not in private["html"] and "Чебоксары" not in private["html"]
    assert VM.PRIVATE_NOTE in private["html"] and "2 тяжёлые истории" in private["subject"]
    assert "Чебоксары" not in private["subject"] and "Пересказ" not in private["preheader"]   # тема на личную — без случая
    assert not _sql("SELECT 1 FROM app_mail_log WHERE username = 'vera' AND kind = 'vsp_daily'")
    # отписалась — писем нет; раздел вернули Вере — письмо пойдёт со следующего выпуска
    VM.save("anna", {"on": False})
    world["access"].add("vera")
    world["cands"] += [_cand("story", "s3", subject="SPE")]
    assert MD.tick(TUE).get("vsp", 0) == 2 and {m["to"] for m in sent[2:]} == {"boris@mail.example.org", "vera@corp.example.ru"}


def test_failed_send_is_retried_and_paused_mail_sends_nothing(pg, sent, world, monkeypatch):
    _user("anna", "anna@corp.example.ru", on=True)
    world["cands"] = [_cand("story", "s1")]
    real = mailer.send
    monkeypatch.setattr(mailer, "send", lambda *a, **k: (_ for _ in ()).throw(mailer.MailError("сервер недоступен")))
    r = MD.tick(MON)
    assert "vsp" not in r and r["errors"] == 1
    assert not _sql("SELECT 1 FROM app_mail_log WHERE kind = 'vsp_daily'")       # место в журнале освобождено
    monkeypatch.setattr(mailer, "send", real)
    assert MD.tick(MON + timedelta(minutes=5)).get("vsp", 0) == 1 and len(sent) == 1
    monkeypatch.setenv("MAIL_PAUSED", "1")
    world["cands"] += [_cand("story", "s2")]
    assert MD.tick(TUE).get("skipped") and len(sent) == 1


def test_sample_letter_goes_to_own_address_with_a_limit(pg, sent, world):
    _user("anna", "anna@corp.example.ru")
    with pytest.raises(MD.MailUserError, match="подключите почту"):
        VM.send_test("nobody")
    world["cands"] = [_cand("story", "s1"), _cand("wait", "w-old", recent=False), _cand("wave", "k:8", recent=False)]
    r = VM.send_test("anna", "Анна Смирнова")                         # выпусков ещё не было — берётся свежее из раздела
    assert r == {"ok": True, "to": "anna@corp.example.ru", "items": 2}                # история и способ действия
    assert sent[0]["mail"]["subject"].startswith("Пример письма · ") and "Пересказ истории s1" in sent[0]["mail"]["html"]
    assert not _sql("SELECT 1 FROM vsp_mail_item")                    # пример ничего не помечает «отправленным»
    VM.send_test("anna")
    VM.send_test("anna")
    with pytest.raises(MD.MailUserError, match="три раза"):
        VM.send_test("anna")
    assert len(sent) == 3 and not _sql("SELECT 1 FROM app_mail_log WHERE kind = 'vsp_daily'")
