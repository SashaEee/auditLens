"""Отправка писем AuditLens через SMTP служебного почтового ящика.

Настройки — в .env: SMTP_HOST, SMTP_PORT (465 — SSL, 587 — STARTTLS),
SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_FROM_NAME (по умолчанию «AuditLens»).

Кому можно слать:
• адрес, который человек указал и подтверждает сам (consented=True) — это его
  согласие, такие письма идут всегда;
• адреса из системы входа — только когда включат MAIL_ENABLED=1;
• MAIL_TEST_TO (через запятую) — тестовые адреса владельца, всегда.
MAIL_PAUSED=1 останавливает всё, кроме тестовых адресов.

Для разработки: MAIL_SINK_DIR — письма не уходят по SMTP, а ложатся файлами .eml
в эту папку (их открывает любая почтовая программа).

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
    return bool(_env("MAIL_SINK_DIR") or (_env("SMTP_HOST") and _env("SMTP_USER") and _env("SMTP_PASSWORD")))


def test_recipients() -> list[str]:
    return [x.strip().lower() for x in _env("MAIL_TEST_TO").split(",") if x.strip()]


def paused() -> bool:
    return _env("MAIL_PAUSED") == "1"


def allowed(to: str, *, consented: bool = False) -> bool:
    """Тестовые — всегда; указанные самим человеком — если не на паузе; остальные —
    только при MAIL_ENABLED=1."""
    if (to or "").strip().lower() in test_recipients():
        return True
    if paused():
        return False
    return consented or _env("MAIL_ENABLED") == "1"


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


def send(to: str, mail: dict, *, bulk: bool = False, consented: bool = False) -> str:
    """Отправить письмо. Возвращает Message-ID; при отказе — MailError.
    consented — адрес человек указал сам (подтверждён или подтверждается)."""
    if not configured():
        raise MailError("почта не настроена: нет SMTP_* в .env")
    if not allowed(to, consented=consented):
        if paused():
            raise MailError("рассылка на паузе (MAIL_PAUSED=1)")
        raise MailError("рассылка ещё не включена: письма уходят только на тестовые адреса (MAIL_TEST_TO)")
    msg = build(to, mail, bulk=bulk)
    if _env("MAIL_SINK_DIR"):
        from pathlib import Path
        sink = Path(_env("MAIL_SINK_DIR"))
        sink.mkdir(parents=True, exist_ok=True)
        stamp = formatdate(localtime=True).replace(",", "").replace(":", "-").replace(" ", "_")
        (sink / f"{stamp}_{to.replace('@', '_at_')}_{make_msgid()[1:9]}.eml").write_bytes(bytes(msg))
        log.info("[mail] (в папку) → %s: %s", to, mail["subject"][:80])
        return msg["Message-ID"]
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
