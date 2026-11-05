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

import hashlib
import json
import logging
import os
import smtplib
import ssl
import time
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
    images = [(cid, data, "png") for cid, data in inline_images(mail["html"])]
    images += [(c, d, s) for c, d, s in (mail.get("images") or []) if f"cid:{c}" in mail["html"]]
    if images:
        html_part = m.get_payload()[1]
        for cid, data, sub in images:
            html_part.add_related(data, maintype="image", subtype=sub, cid=f"<{cid}>", disposition="inline")
    return m


# ── закреплённый сертификат почтового сервера ─────────────────────────────────
# 03.11.2026 у сертификата почтового сервера истёк срок (не сработало автопродление), и с тех пор
# ни одно письмо не уходило: проверка TLS падала. Пока администратор сервера его не продлит,
# владелец разрешил (05.11) принимать ровно этот сертификат по отпечатку SHA-256. Порядок:
# сначала всегда обычная проверка; закреплённый отпечаток — только если она не прошла, и сверяется
# ДО отправки пароля. Подменить такой сертификат без закрытого ключа сервера нельзя. Отпечаток —
# в базе (app_setting, ключ smtp_tls_pin: {"host", "sha256"}), а не в коде и не в переменных.
_PIN: dict = {"at": -1e9, "v": None}


def _pinned(host: str) -> str | None:
    now = time.monotonic()
    if now - _PIN["at"] > 60:
        v = None
        try:
            from sqlalchemy import text
            from .. import db
            with db.session() as s:
                v = s.execute(text("SELECT value FROM app_setting WHERE key = 'smtp_tls_pin'")).scalar()
        except Exception:  # noqa: BLE001 — нет таблицы или базы: закрепления нет
            v = None
        _PIN.update(at=now, v=v)
    v = _PIN["v"]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    if not isinstance(v, dict) or str(v.get("host") or "").lower() != (host or "").lower():
        return None
    pin = str(v.get("sha256") or "").replace(":", "").strip().lower()
    return pin if len(pin) == 64 else None


def _check_pin(sock, pin: str) -> None:
    der = sock.getpeercert(binary_form=True) if sock is not None else None
    if not der or hashlib.sha256(der).hexdigest() != pin:
        raise MailError("сертификат почтового сервера не проходит проверку и не совпал с закреплённым")


def _smtp(host: str, port: int, msg: EmailMessage, ctx: ssl.SSLContext, pin: str | None = None):
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
            if pin:
                _check_pin(s.sock, pin)
            s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
            return s.send_message(msg)
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls(context=ctx)
        if pin:
            _check_pin(s.sock, pin)
        s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
        return s.send_message(msg)


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
    try:
        try:
            refused = _smtp(host, port, msg, ssl.create_default_context())
        except ssl.SSLCertVerificationError:
            pin = _pinned(host)
            if not pin:
                raise
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE          # проверка — сверкой отпечатка в _check_pin
            refused = _smtp(host, port, msg, ctx, pin)
            log.info("[mail] сертификат %s не проходит проверку — принят по закреплённому отпечатку", host)
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("почтовый сервер не принял пароль приложения") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"почтовый сервер недоступен: {type(e).__name__}") from e
    if refused:
        raise MailError("почтовый сервер отказался доставлять на этот адрес")
    log.info("[mail] → %s: %s", to, mail["subject"][:80])
    return msg["Message-ID"]
