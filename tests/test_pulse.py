"""«Пульс»: кого считать в метриках, обрезка профиля, названия лент.

SQL самих метрик проверяется на живой базе (тесты здесь без БД).
"""
from __future__ import annotations

from bank_audit.web import telemetry as T
from bank_audit.web.profile_ai import clip_sentence
from bank_audit.web.sources_catalog import news_source_label


def test_excluded_owner_by_default(monkeypatch):
    monkeypatch.setattr(T, "hidden_users", lambda: ["svc-a", "svc-b"])
    assert T.excluded("owner") == ["owner", "svc-a", "svc-b"]
    # «со мной» — владелец считается, служебные — нет
    assert T.excluded("owner", with_me=True) == ["svc-a", "svc-b"]


def test_excluded_empty_list_gets_sentinel():
    # пустой массив PG не выводит по типу: в запрос уходит заглушка
    assert T._ex([]) == [T._NOBODY]
    assert T._ex(None) == [T._NOBODY]
    assert T._ex(["b", "a", "a"]) == ["a", "b"]


def test_people_filter_keeps_null_usernames_out_of_exclusion():
    # COALESCE: события без пользователя не выпадают из «<> ALL(...)» как NULL
    assert T._ppl("f.username") == "COALESCE(f.username, '') <> ALL(:ex)"


def test_period_is_calendar_days_msk():
    assert "date_trunc('day'" in T._SINCE and "(:days - 1)" in T._SINCE
    assert "Europe/Moscow" in T._TODAY


def test_human_activity_skips_pulse_polling():
    assert "/api/admin/%" in T._HUMAN and "NOT LIKE" in T._HUMAN


def test_clip_sentence():
    assert clip_sentence("Работает с банками. Задачи носят аналитико-расслед") \
        == "Работает с банками."
    assert clip_sentence("Задачи носят аналитико-расслед") == "Задачи носят…"
    assert clip_sentence("Готово.") == "Готово."
    assert clip_sentence("") == ""


def test_news_source_label():
    assert news_source_label("tg_banksta") == "Банкста"
    assert news_source_label("vedomosti_fin") == "Ведомости — финансы"
    assert news_source_label("unknown_feed") == "unknown_feed"
    assert news_source_label(None) == ""
