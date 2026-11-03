"""Персональные данные на входе ИИ-помощника: запрос о частном лице не уходит
в модель и поиск, хранится без ФИО и реквизитов. Обычные вопросы — как были."""
from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.web import pd_guard as G  # noqa: E402

# реальные запросы октября 2026 (ФИО и дата изменены)
BLOCK = ["Все о Петровой Анне Сергеевне 11.05.1981 Все о ее деятельности , связях",
         "клиента Петрову Анну Сергеевну 11.05.1981 найти на сайтах, где регистрация , какие связи у нее",
         "иванов иван иванович 01.02.1990 где работает",
         "Найди информацию о Сидорове Олеге Петровиче",
         "Анна Сергеевна Петрова, паспорт 4510 123456",
         "Проверь Кузнецову Марию Ильиничну, тел. +7 916 123-45-67"]
PASS = ["Что заявил Герман Оскарович Греф о ставках по вкладам",
        "сравни вклады Сбера и ВТБ",
        "вытащи информацию о подрядчике, его ИНН и коммерческое наименование",
        "Найди информацию о программе Своё дело за 2025 год",
        "жалобы клиентов на Ивана Ивановича Петрова не найдены",
        "ИНН 7707083893 Сбербанк отчётность 2025",
        "Расскажи про требования ЦБ 01.01.2015 указание"]


def test_private_person_queries_are_blocked():
    for q in BLOCK:
        assert G.check(q), q


def test_ordinary_questions_pass():
    for q in PASS:
        assert G.check(q) is None, q


def test_mask_removes_name_and_identifiers():
    m = G.mask(BLOCK[0])
    assert "Петров" not in m and "1981" not in m and "[ФИО]" in m and "[дата рождения]" in m
    assert G.mask("Анна Сергеевна Петрова, паспорт 4510 123456") == "[ФИО], [паспорт]"
    # в обычном вопросе ФИО и ИНН компании остаются, телефон — нет
    assert G.mask(PASS[0]) == PASS[0] and G.mask(PASS[5]) == PASS[5]
    assert G.mask("перезвоните +7 916 123-45-67") == "перезвоните [телефон]"


def test_analyze_refuses_without_calling_the_model(monkeypatch):
    from fastapi.testclient import TestClient

    from bank_audit.web import app as A
    monkeypatch.setenv("LLM_API_KEY", "x")
    logged = []
    monkeypatch.setattr(A.userdata, "touch_user", lambda *a, **k: None)
    monkeypatch.setattr(A.userdata, "log_event", lambda u, k, p: logged.append((k, p)))

    def boom(*a, **k):
        raise AssertionError("запрос о частном лице дошёл до модели")
    monkeypatch.setattr(A, "stream_analysis", boom)
    c = TestClient(A.app)
    h = {"X-Authentik-Username": "u-1", "X-Authentik-Name": "u-1"}
    r = c.post("/api/ai/analyze", json={"question": BLOCK[0], "history": []}, headers=h)
    assert r.status_code == 200
    events = [json.loads(line[5:].strip()) for line in r.text.splitlines() if line.startswith("data:")]
    assert events[-1] == {"type": "done"}
    text = "".join(e.get("chunk", "") for e in events)
    assert "персональные данные" in text and "Петров" not in text
    kind, payload = logged[-1]
    assert kind == "ai_query" and payload["blocked"] == "personal_data"
    assert "Петров" not in payload["question"] and "1981" not in payload["question"]
    # уточняющая воронка тоже не зовёт модель
    r = c.post("/api/ai/clarify", json={"question": BLOCK[1], "history": []}, headers=h)
    assert r.json() == {"complete": True, "questions": [], "reason": "personal_data"}
