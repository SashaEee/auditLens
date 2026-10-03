"""Отправка писем AuditLens через SMTP служебного почтового ящика.

Настройки — в .env: SMTP_HOST, SMTP_PORT (465 — SSL, 587 — STARTTLS),
SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_FROM_NAME (по умолчанию «AuditLens»).

Защита от случайной рассылки: пока MAIL_ENABLED не равен 1, письма уходят
только на адреса из MAIL_TEST_TO (через запятую) — сейчас это почта владельца
для проверки шаблонов. Адреса сотрудников появятся, когда система входа
начнёт передавать почту приложению; включать рассылку — отдельным решением.

Заголовки: Auto-Submitted и X-Auto-Response-Suppress — чтобы Outlook не
отвечал на уведомления автоответами («я в отпуске») и не устраивал петли;
References по делу — почтовые программы складывают письма одного дела в цепочку.

Картинки (логотип) лежат внутри письма: HTML ссылается на них через cid:,
части собираются в multipart/related — внешние ссылки на картинки
корпоративная почта режет, а вложенные показывает сразу.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    """Письмо не ушло — текст для человека."""


def _env(k: str, d: str = "") -> str:
    return (os.getenv(k) or d).strip()


def configured() -> bool:
    return bool(_env("SMTP_HOST") and _env("SMTP_USER") and _env("SMTP_PASSWORD"))


def test_recipients() -> list[str]:
    return [x.strip().lower() for x in _env("MAIL_TEST_TO").split(",") if x.strip()]


def allowed(to: str) -> bool:
    """Пока рассылка не включена — только тестовые адреса."""
    if _env("MAIL_ENABLED") == "1":
        return True
    return (to or "").strip().lower() in test_recipients()


def build(to: str, mail: dict, *, bulk: bool = False) -> EmailMessage:
    """Письмо из шаблона (subject/html/text/thread) — без отправки."""
    sender = _env("SMTP_FROM") or _env("SMTP_USER")
    domain = sender.split("@")[-1] if "@" in sender else "auditlens.local"
    m = EmailMessage()
    m["From"] = formataddr((_env("SMTP_FROM_NAME", "AuditLens"), sender))
    m["To"] = to
    m["Subject"] = mail["subject"]
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=domain)
    m["Auto-Submitted"] = "auto-generated"
    m["X-Auto-Response-Suppress"] = "All"
    if bulk:
        m["Precedence"] = "bulk"
    if mail.get("thread"):
        m["References"] = f"<auditlens-{mail['thread']}@{domain}>"
    m.set_content(mail["text"])
    m.add_alternative(mail["html"], subtype="html")
    from .mail_templates import inline_images
    images = inline_images(mail["html"])
    if images:
        html_part = m.get_payload()[1]
        for cid, data in images:
            html_part.add_related(data, maintype="image", subtype="png", cid=f"<{cid}>", disposition="inline")
    return m


def send(to: str, mail: dict, *, bulk: bool = False) -> str:
    """Отправить письмо. Возвращает Message-ID; при отказе — MailError."""
    if not configured():
        raise MailError("почта не настроена: нет SMTP_* в .env")
    if not allowed(to):
        raise MailError("рассылка ещё не включена: письма уходят только на тестовые адреса (MAIL_TEST_TO)")
    msg = build(to, mail, bulk=bulk)
    host, port = _env("SMTP_HOST"), int(_env("SMTP_PORT", "465") or 465)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
                refused = s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
                refused = s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("почтовый сервер не принял пароль приложения") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"почтовый сервер недоступен: {type(e).__name__}") from e
    if refused:
        raise MailError("почтовый сервер отказался доставлять на этот адрес")
    log.info("[mail] → %s: %s", to, mail["subject"][:80])
    return msg["Message-ID"]
