"""Аудит-дело, этап 3: строки истории без рода, подписи, статусы.

SQL ленты и истории проверяется на живой базе (тесты здесь без БД).
"""
from __future__ import annotations

from bank_audit.web import userdata as U


def _n(u):
    return {"e-1": "Елена В.", "v-2": "Ольга Х."}.get(u, u)


def test_history_lines_are_events_not_gendered_verbs():
    assert U._ev_text("created", {}, _n) == "Дело создано"
    assert U._ev_text("items_added", {"n": 1, "titles": ["Страховка без согласия"]}, _n) \
        == "Добавлен материал: «Страховка без согласия»"
    assert U._ev_text("items_added", {"n": 3, "titles": ["а", "б", "в"]}, _n) \
        == "Добавлено материалов: 3 — «а», «б»…"
    assert U._ev_text("member_added", {"member": "e-1", "role": "editor"}, _n) \
        == "Новый участник: Елена В. — может добавлять"
    assert U._ev_text("member_removed", {"member": "v-2"}, _n) == "Исключение из дела: Ольга Х."
    assert U._ev_text("owner", {"member": "e-1"}, _n) == "Передача владения: теперь ведёт Елена В."
    assert U._ev_text("status", {"from": "collect", "to": "done"}, _n) \
        == "Статус: Сбор материалов → Завершено"
    assert U._ev_text("archived", {"on": True}, _n) == "Дело перенесено в архив"
    assert U._ev_text("archived", {"on": False}, _n) == "Дело возвращено из архива"
    assert U._ev_text("analysis", {"n": 1}, _n) == "Новый разбор ИИ — по 1 материалу"
    assert U._ev_text("analysis", {"n": 7}, _n) == "Новый разбор ИИ — по 7 материалам"
    long = U._ev_text("renamed", {"from": "a" * 200, "to": "b"}, _n)
    assert "…»" in long and len(long) < 120


def test_short_names():
    assert U.puname("Елена Волкова") == "Елена В."
    assert U.puname("Елена") == "Елена"
    assert U.puname(None) == ""


def test_statuses_and_message_limit():
    assert list(U.CASE_STATUS) == ["collect", "work", "done"]
    assert U.CASE_MSG_MAX == 4000


def test_status_change_rejects_unknown_and_non_owner(monkeypatch):
    monkeypatch.setattr(U, "_case_flags", lambda c, u: {"role": "editor", "archived": False,
                                                        "status": "collect", "title": "К"})
    try:
        U.set_case_status(1, "e-1", "work")
        raise AssertionError("должно быть отказано")
    except U.CaseError as e:
        assert "владелец" in str(e)
    monkeypatch.setattr(U, "_case_flags", lambda c, u: {"role": "owner", "archived": True,
                                                        "status": "collect", "title": "К"})
    try:
        U.set_case_status(1, "o", "work")
        raise AssertionError("в архиве статус не меняется")
    except U.CaseError as e:
        assert "архив" in str(e)


def test_message_rejects_archive_and_empty(monkeypatch):
    monkeypatch.setattr(U, "_case_flags", lambda c, u: {"role": "viewer", "archived": True,
                                                        "status": "work", "title": "К"})
    try:
        U.add_case_msg(1, "v-2", "привет")
        raise AssertionError("в архиве не пишут")
    except U.CaseError as e:
        assert "архив" in str(e)
    monkeypatch.setattr(U, "_case_flags", lambda c, u: {"role": "viewer", "archived": False,
                                                        "status": "work", "title": "К"})
    for bad in ("", "   ", "x" * 4001):
        try:
            U.add_case_msg(1, "v-2", bad)
            raise AssertionError("пустое и слишком длинное не принимаем")
        except U.CaseError:
            pass
