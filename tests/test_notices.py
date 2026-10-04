"""Колокольчик: заголовки без рода, склейка, кому не слать.

SQL самих уведомлений проверяется на живой базе (тесты здесь без БД).
"""
from __future__ import annotations

from bank_audit.web import notices as N


class _S:
    """Поддельная сессия: пишет SQL, на SELECT склейки отвечает prev."""

    def __init__(self, prev=None):
        self.calls, self.prev = [], prev

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        prev = self.prev

        class R:
            def mappings(self_inner):
                return self_inner

            def first(self_inner):
                return prev

        return R()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_titles_are_events_without_gender():
    assert N.title_of("case_items", {"case": "Кредиты"}, 1) == "В деле «Кредиты» новый материал"
    assert N.title_of("case_items", {"case": "Кредиты"}, 3) == "В деле «Кредиты» 3 новых материала"
    assert N.title_of("case_items", {"case": "Кредиты"}, 12) == "В деле «Кредиты» 12 новых материалов"
    assert N.title_of("case_items", {"case": "Кредиты"}, 21) == "В деле «Кредиты» 21 новый материал"
    assert N.title_of("case_added", {"case": "К"}) == "Вас добавили в дело «К»"
    assert N.title_of("case_role", {"case": "К", "role_label": "только смотрит"}) \
        == "Ваша роль в деле «К»: только смотрит"
    assert N.title_of("report_shared", {"report": "Ставки  по\nвкладам"}) \
        == "С вами поделились отчётом «Ставки по вкладам»"
    assert N.title_of("ticket", {"no": 7, "reply": True}) == "Команда AuditLens ответила на обращение № 7"
    assert N.title_of("ticket", {"no": 7, "status_label": "Сделано"}) == "Обращение № 7: Сделано"
    long = N.title_of("case_added", {"case": "д" * 200})
    assert long.endswith("…»") and len(long) < 130


def test_every_kind_has_a_group():
    for kind, group in N.KIND_GROUP.items():
        assert group in N.GROUPS, kind


def test_actor_and_muted_get_nothing(monkeypatch):
    s = _S()
    monkeypatch.setattr(N.db, "session", lambda: s)
    monkeypatch.setattr(N, "_muted", lambda users, g: {"muted"} if g == "items" else set())
    n = N.notify(["actor", "a", "muted", None, "a"], "case_items", actor="actor",
                 link="case:1", ref={"case": "К"}, n=2)
    assert n == 1                                   # только «a»: автор и выключивший — нет
    ins = [c for c in s.calls if "INSERT INTO app_notice" in c[0]]
    assert len(ins) == 1 and ins[0][1]["u"] == "a" and ins[0][1]["c"] == 2
    assert ins[0][1]["t"] == "В деле «К» 2 новых материала"


def test_unread_items_merge_into_one_line(monkeypatch):
    s = _S(prev={"notice_id": 5, "count": 3, "ref": {"case": "К"}})
    monkeypatch.setattr(N.db, "session", lambda: s)
    monkeypatch.setattr(N, "_muted", lambda users, g: set())
    N.notify(["a"], "case_items", actor="b", link="case:1", ref={"case": "К"}, n=2)
    upd = [c for c in s.calls if "UPDATE app_notice" in c[0]]
    assert upd and upd[0][1]["c"] == 5 and upd[0][1]["t"] == "В деле «К» 5 новых материалов"
    assert not [c for c in s.calls if "INSERT INTO app_notice" in c[0]]


def test_ticket_reply_survives_later_status_change(monkeypatch):
    s = _S(prev={"notice_id": 9, "count": 1, "ref": {"no": 7, "reply": True}})
    monkeypatch.setattr(N.db, "session", lambda: s)
    monkeypatch.setattr(N, "_muted", lambda users, g: set())
    N.notify(["a"], "ticket", actor="team", link="inbox:7",
             ref={"no": 7, "status_label": "Сделано"})
    upd = [c for c in s.calls if "UPDATE app_notice" in c[0]][0]
    assert upd[1]["c"] == 1 and upd[1]["t"] == "Команда AuditLens ответила на обращение № 7"


def test_notify_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(N, "_muted", lambda users, g: set())
    monkeypatch.setattr(N.db, "session", boom)
    assert N.notify(["a"], "case_added", actor="b", ref={"case": "К"}) == 0


def test_mark_read_needs_a_target():
    assert N.mark_read("a") == 0                    # без ids/all/link — ничего не трогаем


def test_settings_reflect_prefs():
    st = {g["key"]: g["on"] for g in N.settings({"notify_off": ["items"]})}
    assert st == {"mention": True, "talk": True, "items": False, "access": True, "inbox": True}
    assert all(g["on"] for g in N.settings(None))


def test_discussion_titles():
    assert N.title_of("case_msg", {"case": "К"}, 1) == "Новое сообщение в деле «К»"
    assert N.title_of("case_msg", {"case": "К"}, 3) == "В деле «К» 3 новых сообщения"
    assert N.title_of("case_msg", {"case": "К"}, 5) == "В деле «К» 5 новых сообщений"
    assert N.title_of("case_mention", {"case": "К"}) == "Вас упомянули в деле «К»"
    assert N.title_of("case_reply", {"case": "К"}) == "Ответ на ваше сообщение в деле «К»"
    assert N.title_of("case_reply", {"case": "К", "on_item": True}) \
        == "Комментарий к вашему материалу в деле «К»"
    assert N.title_of("case_status", {"case": "К", "status_label": "В работе"}) \
        == "Статус дела «К»: В работе"
    assert N.title_of("case_status", {"case": "К", "archived": True}) == "Дело «К» перенесено в архив"
    assert N.title_of("case_status", {"case": "К", "archived": False}) == "Дело «К» возвращено из архива"
    assert N.title_of("case_analysis", {"case": "К"}) == "В деле «К» новый разбор ИИ"


def test_mute_never_silences_mentions_or_access():
    assert "case_mention" not in N.CASE_MUTABLE and "case_reply" not in N.CASE_MUTABLE
    assert "case_added" not in N.CASE_MUTABLE and "case_removed" not in N.CASE_MUTABLE
    assert {"case_items", "case_msg", "case_status", "case_analysis"} <= N.CASE_MUTABLE


def test_messages_merge_across_authors(monkeypatch):
    # «3 новых сообщения» копятся от разных коллег; материалы — только от одного
    s = _S(prev={"notice_id": 4, "count": 2, "ref": {"case": "К"}})
    monkeypatch.setattr(N.db, "session", lambda: s)
    monkeypatch.setattr(N, "_muted", lambda users, g: set())
    N.notify(["a"], "case_msg", actor="c", link="case:1:talk", ref={"case": "К", "snippet": "x"})
    sel = [c for c in s.calls if "SELECT notice_id" in c[0]][0]
    assert sel[1]["same"] is False
    upd = [c for c in s.calls if "UPDATE app_notice" in c[0]][0]
    assert upd[1]["c"] == 3 and upd[1]["t"] == "В деле «К» 3 новых сообщения"


def test_mark_read_prefix_matches_nested_links(monkeypatch):
    s = _S()

    class R:
        rowcount = 2
    s.execute = lambda sql, params=None: (s.calls.append((str(sql), params)), R())[1]
    monkeypatch.setattr(N.db, "session", lambda: s)
    assert N.mark_read("a", link="case:12:talk", prefix=True) == 2
    sql, p = s.calls[0]
    assert "LIKE :lp" in sql and p["lp"] == "case:12:talk:%" and p["l"] == "case:12:talk"


def test_talk_targets_one_notice_per_person_most_personal_wins():
    t = N.talk_targets("me", ["me", "a", "b", "c", "d"], ["a", "me"], reply_author="a",
                       item_author="b")
    assert t == [("case_mention", ["a"], {}), ("case_reply", ["b"], {"on_item": True}),
                 ("case_msg", ["c", "d"], {})]
    # ответ себе и свой материал — без уведомлений себе
    assert N.talk_targets("me", ["me", "a"], [], reply_author="me", item_author="me") \
        == [("case_msg", ["a"], {})]
    assert N.talk_targets("me", ["me"], []) == []
