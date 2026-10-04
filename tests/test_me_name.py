"""Имя пользователя в профиле не затирается логином и попадает в письма."""
import os

# как в tests/test_static_bust_main.py: app при импорте делает db.init()
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.web import app as A  # noqa: E402
from bank_audit.web.auth import CurrentUser  # noqa: E402


def _stub(monkeypatch, stored: dict | None):
    seen = {}

    def touch(username, display_name=None, **kw):
        seen["name"] = display_name
        return stored
    monkeypatch.setattr(A.userdata, "touch_user", touch)
    monkeypatch.setattr(A.userdata, "get_user", lambda u: stored)
    for f in ("top_interests", "recommend_topics", "personalization_score"):
        monkeypatch.setattr(A.userdata, f, lambda u: None)
    monkeypatch.setattr(A.telemetry, "is_admin", lambda u: False)
    monkeypatch.setattr(A.telemetry, "can_pulse", lambda u: False)
    return seen


def test_request_without_name_header_keeps_stored_name(monkeypatch):
    """Без заголовка с именем user.name равен логину — им нельзя затирать имя в профиле."""
    seen = _stub(monkeypatch, {"display_name": "Анна Смирнова"})
    out = A.get_me(user=CurrentUser(username="ivanov-2127124", name="ivanov-2127124", authenticated=True))
    assert seen["name"] is None and out["name"] == "Анна Смирнова"
    seen = _stub(monkeypatch, {"display_name": "Анна Смирнова"})
    A.get_me(user=CurrentUser(username="ivanov-2127124", name="Анна Смирнова", authenticated=True))
    assert seen["name"] == "Анна Смирнова"


def test_mail_name_comes_from_profile_not_from_request(monkeypatch):
    _stub(monkeypatch, {"display_name": "Анна Смирнова"})
    login = CurrentUser(username="ivanov-2127124", name="ivanov-2127124", authenticated=True)
    assert A._mail_name(login) == "Анна Смирнова"
    _stub(monkeypatch, None)
    assert A._mail_name(login) == ""


def test_pulse_mail_addresses_only_for_owner(monkeypatch):
    """Кто подключил почту — видят все, у кого есть «Пульс»; сами адреса — только владелец."""
    def fake_metrics(days, ex):
        return {"mail": {"people": [{"username": "u1", "name": "Анна Смирнова", "email": "a@example.org"}]}}
    monkeypatch.setattr(A.telemetry, "metrics", fake_metrics)
    monkeypatch.setattr(A.telemetry, "can_pulse", lambda u: True)
    monkeypatch.setattr(A.telemetry, "hidden_users", lambda: [])
    monkeypatch.setattr(A.telemetry, "excluded", lambda u, with_me=None: [])
    viewer = CurrentUser(username="viewer", name="Павел Орлов", authenticated=True)
    monkeypatch.setattr(A.telemetry, "is_admin", lambda u: False)
    assert "email" not in A.admin_metrics(user=viewer)["mail"]["people"][0]
    monkeypatch.setattr(A.telemetry, "is_admin", lambda u: True)
    assert A.admin_metrics(user=viewer)["mail"]["people"][0]["email"] == "a@example.org"
