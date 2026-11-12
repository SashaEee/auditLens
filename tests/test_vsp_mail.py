"""Ежедневное письмо раздела «Аудит сети ВСП»: настройка, отбор в выпуск, содержание и шаблон (без базы)."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import pytest

from bank_audit.web import mail_templates as T
from bank_audit.web import vsp_mail as VM

NOW = datetime(2026, 11, 16, 6, 5, tzinfo=timezone.utc)      # понедельник, 09:05 по Москве
MON, FRI = date(2026, 11, 16), date(2026, 11, 13)
SECRET = ("Чебоксары", "Мира, 5", "ВСП 8613/0123", "подменили вклад страхованием", "Прождала два часа",
          "переводят пенсию")


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://al.example")


def _story(rid, subject="CU", score=5, day=MON, **kw):
    return {"kind": "story", "rid": rid, "day": day, "subject": subject, "gosb": "8613", "point": f"b{rid}",
            "payload": {"city": "Чебоксары", "address": "428000, ЧУВАШСКАЯ РЕСП., Г. ЧЕБОКСАРЫ, УЛ. МИРА, Д. 5",
                        "name": "ВСП № 8613/0123", "gosb": "8613", "date": "2026-11-14", "severity": 3,
                        "episode": "По словам клиента, ему подменили вклад страхованием жизни.",
                        "what": "Вклад подменили страхованием", "inside": False, "vulnerable": True, "money": 300000,
                        "score": score, **kw}}


def _wait(rid, subject="CU", minutes=120, left=False, point="b9", day=MON):
    return {"kind": "wait", "rid": rid, "day": day, "subject": subject, "gosb": "8613", "point": point,
            "payload": {"city": "Чебоксары", "address": "УЛ. МИРА, Д. 5", "name": "ВСП № 8613/0123", "gosb": "8613",
                        "date": "2026-11-15", "minutes": minutes, "cause": "мало окон", "left": left,
                        "quote": "Прождала два часа, работает одно окно"}}


WAVE = {"kind": "wave", "rid": "k:8", "day": MON, "subject": None, "gosb": None, "point": None,
        "payload": {"id": "k8", "name": "Перевод пенсии без согласия", "status": "rising",
                    "text": "Растёт: клиенты пишут, что их пенсию переводят в банк без согласия",
                    "accent": "за год 39 клиентов, годом раньше — 15"}}


def test_prefs_are_normalized():
    assert VM.prefs_of(None) == {"on": False, "subjects": [], "heavy": True, "wait": True, "since": None}
    p = VM.prefs_of({"vsp_mail": {"on": True, "subjects": ["SPE", "нет такого", 5, "SPE"], "wait": False}})
    assert p["on"] and p["subjects"] == ["SPE"] and p["heavy"] and not p["wait"]
    assert not VM.prefs_of({"vsp_mail": {"on": True, "heavy": False, "wait": False}})["on"]   # нечего присылать
    assert VM.prefs_of({"vsp_mail": "мусор"})["on"] is False


def test_first_issue_takes_only_the_freshest():
    cands = [{"kind": "story", "rid": "a", "recent": True}, {"kind": "story", "rid": "b", "recent": False},
             {"kind": "wait", "rid": "c", "recent": True}, {"kind": "wave", "rid": "k:8", "recent": False}]
    rows = {r["rid"]: r["day"] for r in VM.plan_issue(cands, set(), MON)}
    assert rows == {"a": MON, "b": VM.SEED, "c": MON, "k:8": VM.SEED}      # старое помечено «уже было»
    known = {("story", "a"), ("story", "b"), ("wave", "k:8")}
    later = VM.plan_issue(cands + [{"kind": "story", "rid": "d", "recent": False},
                                   {"kind": "wave", "rid": "m:0803", "recent": False}], known, MON)
    assert {r["rid"]: r["day"] for r in later} == {"c": MON, "d": MON, "m:0803": MON}


def test_compose_filters_by_region_and_choice():
    rows = [_story("s1", score=3), _story("s2", score=9), _story("s3", subject="SPE"),
            _wait("w1", minutes=30), _wait("w2", minutes=150, left=True), _wait("w3", subject="SPE", point="b7"), WAVE]
    p = {"on": True, "subjects": [], "heavy": True, "wait": True}
    d = VM.compose(rows, p, FRI, MON)
    assert (d["n_stories"], d["n_wait"], d["n_waves"], d["total"]) == (3, 3, 1, 7)
    assert [s["rid"] for s in d["stories"]][0] == "s2" and d["wait"][0]["rid"] == "w2"      # важное и долгое — первыми
    assert d["n_wait_long"] == 2 and d["n_wait_left"] == 1 and d["period"] == "за выходные"
    assert d["wait_repeat"] == [{"point": "b9", "n": 2, "city": "Чебоксары", "address": "УЛ. МИРА, Д. 5",
                                 "name": "ВСП № 8613/0123", "gosb": "8613"}]
    spb = VM.compose(rows, {**p, "subjects": ["SPE"]}, FRI, MON)
    assert (spb["n_stories"], spb["n_wait"], spb["n_waves"]) == (1, 1, 1) and "Санкт-Петербург" in spb["scope"]
    only_wait = VM.compose(rows, {**p, "heavy": False}, FRI, MON)
    assert (only_wait["n_stories"], only_wait["n_waves"], only_wait["n_wait"]) == (0, 0, 3)
    empty = VM.compose([], p, date(2026, 11, 16), date(2026, 11, 17))
    assert empty["total"] == 0 and empty["period"] == "за сутки"
    # срок письма — по выпускам, которые в него вошли, а не по тому, давно ли было прошлое письмо
    tue = date(2026, 11, 17)
    assert VM.compose([_story("s9", day=tue)], p, date(2026, 11, 3), tue)["period"] == "за сутки"
    assert VM.compose([_story("s9", day=tue), _wait("w9", day=MON)], p, FRI, tue)["period"] == "за 2 дня"
    assert VM.compose([_story("s9", day=tue), _wait("w9", day=MON)], {**p, "wait": False}, FRI, tue)["period"] == "за сутки"


def test_letter_shows_places_words_and_links():
    d = VM.compose([_story("2gis:1"), _wait("yandex:2", left=True), _wait("yandex:3"), WAVE],
                   {"on": True, "subjects": [], "heavy": True, "wait": True}, FRI, MON)
    m = VM.render(d, NOW, "Наталья Иванова")
    assert m["subject"] == "Сеть ВСП за выходные: 1 тяжёлая история, 2 жалобы на ожидание"
    h = m["html"]
    assert "Наталья, доброе утро." in h and "Понедельник, 16 ноября · вся сеть" in h
    assert "Чебоксары, ул. Мира, д. 5 · ВСП 8613/0123 · ГОСБ 8613" in h          # адрес — как в разделе: без индекса и региона
    assert "По словам клиента, ему подменили вклад страхованием жизни." in h and "300 000 ₽ — по словам клиента" in h
    assert "«Прождала два часа, работает одно окно»" in h and "ждал 2 ч, по словам клиента" in h
    assert "ушёл, не дождавшись" in h and "2 жалобы на ожидание за выходные" in h
    assert "Растёт: клиенты пишут, что их пенсию переводят в банк без согласия" in h
    # ссылки ведут к самому случаю, досье и карточке способа; отписка — одной ссылкой
    for go in ("go=vsp%3Fstory%3D2gis%3A1", "go=vsp%3Fs%3Dwait%26point%3Db9", "go=vsp%3Fs%3Dschemes%26scheme%3Dk8",
               "go=vsp%3Fmail%3Doff", "go=vsp%3Fmail%3D1"):
        assert go in h, go
    assert "Отписаться" in h and "Отписаться от этого письма: https://al.example/?go=vsp%3Fmail%3Doff" in m["text"]
    assert VM.NOTE in h and "по будням после 9:00" in h
    assert "виноват" not in h.replace(VM.NOTE, "")             # вину письмо не утверждает


def test_letter_follows_mail_rules():
    d = VM.compose([_story("2gis:1"), _wait("yandex:2")], {"on": True, "subjects": [], "heavy": True, "wait": True},
                   FRI, MON)
    m = VM.render(d, NOW, "ivanova-12345")                      # логин вместо имени — здороваемся без имени
    h = m["html"]
    assert "Доброе утро." in h and "ivanova" not in h
    assert re.findall(r' src="([^"]*)"', h) == [f"cid:{T.LOGO_CID}"]
    assert 'href="http://' not in h and "url(" not in h
    assert set(re.findall(r"font-family:([^;\"]+)", h)) == {T.FONT} and "-apple-system" not in h
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", h + m["text"])          # без эмодзи
    assert m["text"].strip() and "Открыть раздел: https://al.example/?go=vsp" in m["text"]


def test_private_address_gets_numbers_only():
    d = VM.compose([_story("2gis:1"), _wait("yandex:2"), WAVE], {"on": True, "subjects": [], "heavy": True,
                                                                "wait": True}, FRI, MON)
    m = VM.render(d, NOW, "Наталья Иванова", private=True)
    for part in ("subject", "preheader", "html", "text"):
        for s in SECRET:
            assert s not in m[part], (part, s)
    assert VM.PRIVATE_NOTE in m["html"] and "1 жалоба на долгое ожидание" not in m["subject"]
    assert "тяжёлая история" in m["html"] and "Отписаться" in m["html"]


def test_sample_letter_is_marked():
    d = VM.compose([_wait("yandex:2")], {"on": True, "subjects": ["CU"], "heavy": True, "wait": True}, FRI, MON)
    m = VM.render(d, NOW, sample=True)
    assert m["subject"].startswith("Пример письма · ") and "запросили пример" in m["html"]
    assert "Чувашия" in m["html"] or "Чуваш" in m["html"]


def test_pictographs_are_dropped_from_mail_text():
    assert VM.plain("не тот талон \U0001F9D0 никого на входе нет \u2757\uFE0F") == "не тот талон никого на входе нет"
    assert VM.plain("Ждали 40 минут — «окно №3», 10:30–11:10") == "Ждали 40 минут — «окно №3», 10:30–11:10"
    assert VM.plain(None) == ""


def test_address_is_short_and_readable():
    assert VM._addr("191011, Г. САНКТ-ПЕТЕРБУРГ, УЛ. ДУМСКАЯ, Д.1-3, ЛИТЕР А") == "ул. Думская, д.1-3, Литер а"
    assert VM._addr("352120, КРАСНОДАРСКИЙ КРАЙ, Г. ТИХОРЕЦК, УЛ. ЭНГЕЛЬСА, 91") == "ул. Энгельса, 91"
    assert VM._addr("367000, РЕСП. ДАГЕСТАН, Г. МАХАЧКАЛА, ПР-Т ИМАМА ШАМИЛЯ, Д. 12") == "пр-т Имама Шамиля, д. 12"
    assert VM._addr("Казань, ул. Баумана, 5") == "ул. Баумана, 5"                 # уже обычным регистром — не трогаем
    assert VM._addr("413840, САРАТОВСКАЯ ОБЛ., Г. БАЛАКОВО, ШОССЕ ЭНТУЗИАСТОВ, 3") == "Шоссе Энтузиастов, 3"
    assert VM._addr("169900, РЕСП. КОМИ, Г. ВОРКУТА, УЛ.ЛЕНИНА, 31") == "ул. Ленина, 31"
    assert VM._addr("652840, Г. МЫСКИ, УЛ.СОВЕТСКАЯ, Д.42") == "ул. Советская, д.42"
    assert VM._addr(None) == ""
    d = VM.compose([_wait("w1")], {"on": True, "subjects": [], "heavy": True, "wait": True}, FRI, MON)
    h = VM.render(d, NOW)["html"]
    assert "тяжёл" not in h.split("Долгое ожидание")[0].split("Что нового")[1]     # нулей в плитках письма нет

