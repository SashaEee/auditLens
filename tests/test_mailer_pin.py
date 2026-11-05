"""Закреплённый сертификат почтового сервера (mailer._pinned/_check_pin): принимается только
если обычная проверка не прошла и отпечаток совпал; пароль — после сверки."""
from __future__ import annotations

import hashlib
import ssl

import pytest

from bank_audit.web import mailer

DER = b"0\x82fake-certificate-der"
PIN = hashlib.sha256(DER).hexdigest()


class _Sock:
    def getpeercert(self, binary_form=False):
        return DER


@pytest.fixture
def smtp(monkeypatch):
    calls = {"ctx": [], "login": 0, "sent": 0, "fail_verify": True}

    class FakeSSL:
        def __init__(self, host, port, timeout=None, context=None):
            calls["ctx"].append(context)
            if context.verify_mode != ssl.CERT_NONE and calls["fail_verify"]:
                raise ssl.SSLCertVerificationError("certificate has expired")
            self.sock = _Sock()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            calls["login"] += 1

        def send_message(self, msg):
            calls["sent"] += 1
            return {}

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSSL)
    for k, v in {"SMTP_HOST": "mail.example.ru", "SMTP_PORT": "465", "SMTP_USER": "bot@example.ru",
                 "SMTP_PASSWORD": "x", "MAIL_TEST_TO": "to@example.ru"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MAIL_SINK_DIR", raising=False)
    monkeypatch.setattr(mailer, "_PIN", {"at": 1e18, "v": None})
    return calls


MAIL = {"subject": "s", "text": "t", "html": "<p>h</p>"}


def test_valid_certificate_never_uses_pin(smtp):
    smtp["fail_verify"] = False
    mailer.send("to@example.ru", MAIL)
    assert smtp["sent"] == 1 and len(smtp["ctx"]) == 1 and smtp["ctx"][0].verify_mode == ssl.CERT_REQUIRED


def test_expired_certificate_without_pin_fails(smtp):
    with pytest.raises(mailer.MailError):
        mailer.send("to@example.ru", MAIL)
    assert smtp["login"] == 0


def test_expired_certificate_accepted_only_by_matching_pin(smtp):
    mailer._PIN["v"] = {"host": "mail.example.ru", "sha256": PIN}
    mailer.send("to@example.ru", MAIL)
    assert smtp["sent"] == 1 and smtp["ctx"][-1].verify_mode == ssl.CERT_NONE


def test_wrong_pin_or_host_sends_no_password(smtp):
    mailer._PIN["v"] = {"host": "mail.example.ru", "sha256": "0" * 64}
    with pytest.raises(mailer.MailError, match="не совпал"):
        mailer.send("to@example.ru", MAIL)
    assert smtp["login"] == 0 and smtp["sent"] == 0
    mailer._PIN["v"] = {"host": "other.example.ru", "sha256": PIN}
    with pytest.raises(mailer.MailError):
        mailer.send("to@example.ru", MAIL)
    assert smtp["login"] == 0
