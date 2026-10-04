"""«Пульс»: кого считать в метриках, обрезка профиля, названия лент.

SQL самих метрик проверяется на живой базе (тесты здесь без БД).
"""
from __future__ import annotations

from bank_audit.web import telemetry as T
from bank_audit.web.profile_ai import clip_sentence
from bank_audit.web.sources_catalog import news_source_label


def test_excluded_viewer_by_default(monkeypatch):
    # владелец помечен служебным: себя по умолчанию не считает, «со мной» — считает
    monkeypatch.setattr(T, "hidden_users", lambda: ["owner", "svc-a"])
    assert T.excluded("owner") == ["owner", "svc-a"]
    assert T.excluded("owner", with_me=True) == ["svc-a"]
    assert T.with_me_default("owner") is False
    # коллега с доступом к «Пульсу» — обычный пользователь: по умолчанию в счёт
    assert T.excluded("colleague") == ["owner", "svc-a"]
    assert T.excluded("colleague", with_me=False) == ["colleague", "owner", "svc-a"]
    assert T.with_me_default("colleague") is True


def test_pulse_access_is_separate_from_owner_rights(monkeypatch):
    monkeypatch.setenv("ADMIN_USERS", "owner")
    monkeypatch.setenv("PULSE_USERS", " a , b ")
    assert T.is_admin("owner") and T.can_pulse("owner")
    assert T.can_pulse("a") and T.can_pulse("b") and not T.is_admin("a")
    assert not T.can_pulse("c") and not T.can_pulse(None)


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


def test_event_age_is_clamped():
    assert T._age_ms({"age_ms": 1500}) == 1500
    assert T._age_ms({"age_ms": -5}) == 0
    assert T._age_ms({"age_ms": "x"}) == 0
    assert T._age_ms({}) == 0
    assert T._age_ms({"age_ms": 10 ** 12}) == T._MAX_AGE_MS


def test_track_batch_stamps_age_and_moves_last_seen(monkeypatch):
    calls = []

    class S:
        def execute(self, sql, params=None):
            calls.append((str(sql), params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(T.db, "session", lambda: S())
    n = T.track_batch("u1", [{"kind": "page_view", "page": "reviews", "age_ms": 2000},
                             {"kind": "page_leave", "page": "ai", "dur_ms": 5000, "age_ms": 100},
                             {"kind": "api_request"}])
    assert n == 2                                    # api_request с фронта не принимаем
    ins = calls[0]
    assert "created_at" in ins[0] and "millisecond" in ins[0]
    assert [r["age"] for r in ins[1]] == [2000, 100]
    assert any("last_seen_at" in c[0] for c in calls[1:])
