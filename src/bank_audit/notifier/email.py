"""Корпоративная email-рассылка для алертов.

Адаптировано под внутренний SMTP (Сбер, smtp.omega.sbrf.ru:2525, STARTTLS).
Никаких внешних мессенджеров — банковский compliance запрещает.

Переменные окружения (.env):
  SMTP_HOST        — хост SMTP (default: smtp.omega.sbrf.ru)
  SMTP_PORT        — порт   (default: 2525)
  SMTP_USER        — логин корпоративной учётки
  SMTP_PWD         — пароль (или хранить через keychain — см. get_pwd_callback)
  SMTP_FROM        — отправитель (FROM-заголовок, обязателен в этой реализации)
  ALERTS_TO        — получатель(и) алертов через запятую
  ALERTS_CC        — копия (опционально)

Все методы безопасны: при ошибке SMTP логгируется warning, исключение
наружу не пробрасывается — алерты не должны валить пайплайн.
"""
from __future__ import annotations
import os, smtplib, logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Iterable, Callable

log = logging.getLogger(__name__)


class EmailNotifier:
    """Тонкая обёртка над smtplib для алертов аудита.

    Конфигурация — из env (см. модульный docstring) или явных аргументов.
    """

    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_user: str | None = None,
        smtp_pwd:  str | None = None,
        from_email: str | None = None,
        default_to: str | None = None,
        default_cc: str | None = None,
    ):
        self.smtp_host  = smtp_host or os.getenv("SMTP_HOST", "smtp.omega.sbrf.ru")
        self.smtp_port  = int(smtp_port or os.getenv("SMTP_PORT", "2525"))
        self.smtp_user  = smtp_user or os.getenv("SMTP_USER", "")
        self.smtp_pwd   = smtp_pwd  or os.getenv("SMTP_PWD",  "")
        self.from_email = from_email or os.getenv("SMTP_FROM", self.smtp_user)
        self.default_to = default_to or os.getenv("ALERTS_TO", "")
        self.default_cc = default_cc or os.getenv("ALERTS_CC", "")

    # ── проверка ──────────────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        """True если есть всё необходимое для отправки."""
        return bool(self.smtp_user and self.smtp_pwd and self.from_email and self.default_to)

    def test_login(self) -> tuple[bool, str | None]:
        """Проверка соединения и логина. Возвращает (ok, error_msg|None)."""
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as s:
                s.starttls()
                s.login(self.smtp_user, self.smtp_pwd)
            return True, None
        except Exception as e:
            return False, str(e)

    # ── отправка ─────────────────────────────────────────────────────────────

    def send(
        self,
        subject: str,
        body: str,
        to: str | Iterable[str] | None = None,
        cc: str | Iterable[str] | None = None,
        is_html: bool = False,
    ) -> bool:
        """Отправляет письмо. Возвращает True при успехе.

        to / cc — строка через запятую или iterable. Если None — берётся default.
        """
        if not self.is_configured():
            log.warning("EmailNotifier: SMTP не сконфигурирован — пропуск отправки")
            return False

        to_str = _join(to or self.default_to)
        cc_str = _join(cc or self.default_cc)
        if not to_str:
            log.warning("EmailNotifier: пустой получатель — пропуск")
            return False

        msg = MIMEMultipart()
        msg["From"]    = self.from_email
        msg["To"]      = to_str
        msg["Subject"] = subject
        if cc_str:
            msg["Cc"] = cc_str
        msg.attach(MIMEText(body, "html" if is_html else "plain", "utf-8"))

        recipients = [r.strip() for r in to_str.split(",") if r.strip()]
        if cc_str:
            recipients += [r.strip() for r in cc_str.split(",") if r.strip()]

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as s:
                s.starttls()
                s.login(self.smtp_user, self.smtp_pwd)
                s.sendmail(self.from_email, recipients, msg.as_string())
            log.info("EmailNotifier: отправлено '%s' → %s", subject, to_str)
            return True
        except Exception as e:
            log.warning("EmailNotifier: ошибка отправки '%s': %s", subject, e)
            return False


def _join(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return ", ".join(str(x) for x in v)
