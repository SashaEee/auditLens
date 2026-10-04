"""Почта пользователей и рассылка писем-уведомлений (миграции 087–088).

Пока система входа не передаёт почту, адрес указывает сам человек — в
колокольчике («Что присылать» → «На почту») — и подтверждает кодом из письма.
Без подтверждения на адрес не уходит ничего, кроме самого кода.

Корпоративный адрес — почта Sigma (домены из MAIL_CORP_DOMAINS через запятую) —
получает письма целиком. На любой другой — без подробностей (mail_templates.redact):
что произошло и ссылка, без названий дел и отчётов, имён коллег и цитат. Почта
Omega (MAIL_BLOCKED_DOMAINS) внешних писем физически не принимает — такой адрес
отклоняем сразу, иначе код подтверждения просто не дойдёт.

Рассылка — фоновый цикл mail_background_loop, тик раз в 5 минут:
• сразу о личном (упомянули, ответили, добавили в дело, поделились отчётом,
  ответ на обращение). Событию даём 10 минут: прочитали в AuditLens — письма не
  будет. Одним письмом и не чаще раза в 15 минут;
• утренняя сводка — в рабочие дни после 08:00 МСК: всё непрочитанное за неделю,
  о чём ещё не писали. Раз в день (уникальный индекс в журнале).
Каждое событие уходит на почту один раз (app_notice.emailed_at — «захват» до
отправки, при сбое отпускаем), каждое письмо — в журнале app_mail_log.

Кому: подтвердившим адрес самим — всегда (это их согласие); адресам из системы
входа — только при MAIL_ENABLED=1. MAIL_PAUSED=1 останавливает всё.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .. import db
from . import mail_templates as T
from . import mailer, notices
from .auth import clean_email

log = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

CODE_TTL = timedelta(minutes=30)
CODE_TRIES = 5
RESEND_AFTER = timedelta(seconds=60)
CODES_PER_HOUR = 5                  # кодов одному человеку за час
CODES_PER_ADDR_DAY = 5              # кодов на один адрес за сутки — от всех вместе
INSTANT_EVERY = timedelta(minutes=int(os.getenv("MAIL_INSTANT_EVERY_MIN", "15")))
INSTANT_DELAY = timedelta(minutes=int(os.getenv("MAIL_INSTANT_DELAY_MIN", "10")))
INSTANT_WINDOW = timedelta(days=1)  # «сразу» о вчерашнем уже не сразу — это дело сводки
DIGEST_WINDOW = timedelta(days=7)   # старый хвост непрочитанного в сводку не тащим
DIGEST_HOUR = int(os.getenv("MAIL_DIGEST_HOUR_MSK", "8"))
TICK_S = int(os.getenv("MAIL_TICK_S", "300"))


class MailUserError(Exception):
    """Отказ, понятный человеку: status — HTTP-код для ответа."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(sql: str, p: dict | None = None) -> list[dict]:
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), p or {}).mappings().all()]


def _one(sql: str, p: dict | None = None) -> dict | None:
    r = _rows(sql, p)
    return r[0] if r else None


def _scalar(sql: str, p: dict | None = None):
    with db.session() as s:
        return s.execute(text(sql), p or {}).scalar()


def _exec(sql: str, p: dict | None = None) -> int:
    with db.session() as s:
        return s.execute(text(sql), p or {}).rowcount


# ── адреса ───────────────────────────────────────────────────────────────────

def corp_domains() -> list[str]:
    return [d.strip().lower().lstrip("@") for d in os.getenv("MAIL_CORP_DOMAINS", "").split(",")
            if d.strip()]


def blocked_domains() -> list[str]:
    return [d.strip().lower().lstrip("@") for d in os.getenv("MAIL_BLOCKED_DOMAINS", "").split(",")
            if d.strip()]


def _in(email: str | None, domains: list[str]) -> bool:
    if not email or "@" not in email:
        return False
    dom = email.rsplit("@", 1)[1].lower()
    return any(dom == d or dom.endswith("." + d) for d in domains)


def is_corporate(email: str | None) -> bool:
    return _in(email, corp_domains())


def is_blocked(email: str | None) -> bool:
    """Почта Omega: внешние письма туда физически не доходят."""
    return _in(email, blocked_domains())


def _sso_enabled() -> bool:
    return (os.getenv("MAIL_ENABLED") or "").strip() == "1"


def _sendable(source: str | None) -> bool:
    return source == "user" or (source == "sso" and _sso_enabled())


def address(username: str) -> dict | None:
    """Подключённый адрес: {email, source} — source 'user' (подтвердил сам) или 'sso'."""
    r = _one("SELECT email, email_source FROM app_user WHERE username = :u", {"u": username})
    if not r or not r.get("email"):
        return None
    return {"email": r["email"], "source": r.get("email_source") or "sso"}


def state(username: str) -> dict:
    """Что показать в настройках колокольчика."""
    cur = address(username)
    p = _one("SELECT email, expires_at, tries, sent_at FROM app_email_verify WHERE username = :u",
             {"u": username})
    now, pending = _now(), None
    if p and p["expires_at"] > now:
        pending = {"email": p["email"], "corporate": is_corporate(p["email"]),
                   "expires_in": int((p["expires_at"] - now).total_seconds()),
                   "resend_in": max(0, int((p["sent_at"] + RESEND_AFTER - now).total_seconds())),
                   "tries_left": CODE_TRIES - int(p["tries"])}
    return {"email": cur["email"] if cur else None, "source": cur["source"] if cur else None,
            "corporate": bool(cur) and is_corporate(cur["email"]),
            "active": bool(cur) and _sendable(cur["source"]),
            "pending": pending, "corp_domains": corp_domains(), "blocked_domains": blocked_domains()}


def _hash(username: str, email: str, code: str) -> str:
    return hashlib.sha256(f"{username}\n{email}\n{code}".encode()).hexdigest()


def start(username: str, raw_email: str, name: str = "") -> dict:
    """Указали адрес → письмо с кодом. Лимиты — чтобы через AuditLens нельзя было
    засыпать письмами чужой ящик."""
    email = clean_email(raw_email)
    if not email or len(email) > 254:
        raise MailUserError(400, "Похоже, в адресе опечатка — проверьте его")
    if is_blocked(email):
        raise MailUserError(400, "Это почта Omega — письма извне туда не доходят. "
                                 "Укажите адрес Sigma или личную почту")
    cur = address(username)
    if cur and cur["email"] == email and cur["source"] == "user":
        raise MailUserError(400, "Этот адрес уже подключён")
    if not mailer.configured():
        raise MailUserError(503, "Отправка писем в AuditLens пока не настроена")
    now = _now()
    p = _one("SELECT sent_at FROM app_email_verify WHERE username = :u", {"u": username})
    if p and now - p["sent_at"] < RESEND_AFTER:
        sec = int((p["sent_at"] + RESEND_AFTER - now).total_seconds()) + 1
        raise MailUserError(429, f"Новый код можно запросить через {sec} с")
    if int(_scalar("""SELECT count(*) FROM app_mail_log WHERE username = :u AND kind = 'verify' AND ok
                       AND sent_at > now() - interval '1 hour'""", {"u": username}) or 0) >= CODES_PER_HOUR:
        raise MailUserError(429, "За последний час отправлено уже пять кодов — попробуйте позже")
    if int(_scalar("""SELECT count(*) FROM app_mail_log WHERE to_addr = :e AND kind = 'verify' AND ok
                       AND sent_at > now() - interval '1 day'""", {"e": email}) or 0) >= CODES_PER_ADDR_DAY:
        raise MailUserError(429, "На этот адрес сегодня уже отправляли коды — попробуйте завтра")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    mail = T.render_verify(code, email, name, int(CODE_TTL.total_seconds() // 60))
    try:
        deliver(username, "verify", email, mail, consented=True)
    except mailer.MailError as e:
        raise MailUserError(502, "Письмо с кодом не ушло — попробуйте ещё раз через минуту") from e
    _exec("""INSERT INTO app_email_verify (username, email, code_hash, expires_at, tries, sent_at)
             VALUES (:u, :e, :h, :x, 0, now())
             ON CONFLICT (username) DO UPDATE
                SET email = EXCLUDED.email, code_hash = EXCLUDED.code_hash,
                    expires_at = EXCLUDED.expires_at, tries = 0, sent_at = now()""",
          {"u": username, "e": email, "h": _hash(username, email, code), "x": now + CODE_TTL})
    return state(username)


def confirm(username: str, raw_code: str, name: str = "") -> dict:
    """Код из письма → адрес подключён; первое письмо на него — что и когда придёт."""
    code = re.sub(r"\D", "", str(raw_code or ""))
    p = _one("SELECT email, code_hash, expires_at, tries FROM app_email_verify WHERE username = :u",
             {"u": username})
    if not p:
        raise MailUserError(400, "Сначала укажите адрес и запросите код")
    if p["expires_at"] <= _now():
        cancel(username)
        raise MailUserError(410, "Код устарел — запросите новый")
    if len(code) != 6 or not hmac.compare_digest(_hash(username, p["email"], code), p["code_hash"]):
        tries = int(p["tries"]) + 1
        if tries >= CODE_TRIES:
            cancel(username)
            raise MailUserError(429, "Код не подошёл пять раз — запросите новый")
        _exec("UPDATE app_email_verify SET tries = :t WHERE username = :u", {"t": tries, "u": username})
        left = CODE_TRIES - tries
        raise MailUserError(400, f"Код не подошёл — осталось {left} "
                                 f"{T._plural(left, 'попытка', 'попытки', 'попыток')}")
    _exec("""INSERT INTO app_user (username, email, email_at, email_source) VALUES (:u, :e, now(), 'user')
             ON CONFLICT (username) DO UPDATE
                SET email = EXCLUDED.email, email_at = now(), email_source = 'user'""",
          {"u": username, "e": p["email"]})
    cancel(username)
    try:
        deliver(username, "welcome", p["email"],
                T.render_welcome(name, private=not is_corporate(p["email"])), consented=True)
    except Exception:  # noqa: BLE001 — адрес уже подключён, приветствие не обязательно
        log.warning("[mail] приветствие %s не ушло", username, exc_info=True)
    return state(username)


def cancel(username: str) -> None:
    """Забыть неподтверждённый адрес и его код."""
    _exec("DELETE FROM app_email_verify WHERE username = :u", {"u": username})


def remove(username: str) -> dict:
    """Отключить почту. 'off' — чтобы адрес из системы входа не вернулся сам."""
    _exec("UPDATE app_user SET email = NULL, email_at = now(), email_source = 'off' WHERE username = :u",
          {"u": username})
    cancel(username)
    return state(username)


# ── отправка и журнал ────────────────────────────────────────────────────────

def _log(username: str | None, kind: str, to: str, n: int, ok: bool, error: str | None) -> None:
    try:
        _exec("""INSERT INTO app_mail_log (username, kind, to_addr, n_items, ok, error)
                 VALUES (:u, :k, :t, :n, :ok, :er)""",
              {"u": username, "k": kind, "t": to, "n": n, "ok": ok, "er": (error or "")[:300] or None})
    except Exception:  # noqa: BLE001 — журнал не повод не отправить
        log.debug("[mail] журнал не записался", exc_info=True)


def deliver(username: str | None, kind: str, to: str, mail: dict, *, consented: bool = False,
            bulk: bool = False, n: int = 0, log_row: bool = True) -> str:
    """Отправить и записать в журнал. Ошибку (MailError) пробрасывает."""
    try:
        mid = mailer.send(to, mail, bulk=bulk, consented=consented)
    except mailer.MailError as e:
        if log_row:
            _log(username, kind, to, n, False, str(e))
        raise
    if log_row:
        _log(username, kind, to, n, True, None)
    return mid


def recipients() -> list[dict]:
    """Кому сейчас можно писать: подтвердили сами; из системы входа — при MAIL_ENABLED=1."""
    return _rows("""SELECT username, email, COALESCE(email_source, 'sso') AS source, prefs, display_name
                      FROM app_user
                     WHERE email IS NOT NULL
                       AND (email_source = 'user' OR (:sso AND COALESCE(email_source, 'sso') = 'sso'))
                     ORDER BY username""", {"sso": _sso_enabled()})


def _claim(ids: list[int]) -> set[int]:
    """Пометить «отправлено» до отправки: два процесса не пошлют одно и то же."""
    if not ids:
        return set()
    with db.session() as s:
        got = s.execute(text("""UPDATE app_notice SET emailed_at = now()
                                 WHERE notice_id = ANY(:i) AND emailed_at IS NULL AND read_at IS NULL
                                 RETURNING notice_id"""), {"i": [int(i) for i in ids]}).scalars().all()
    return {int(i) for i in got}


def _unclaim(ids) -> None:
    if ids:
        _exec("UPDATE app_notice SET emailed_at = NULL WHERE notice_id = ANY(:i)", {"i": [int(i) for i in ids]})


def _ts(v) -> datetime:
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _instant(r: dict, now: datetime) -> int:
    """Личное — одним письмом, не чаще раза в 15 минут, событию даём 10 минут на прочтение."""
    last = _scalar("""SELECT max(sent_at) FROM app_mail_log
                       WHERE username = :u AND kind = 'instant' AND ok""", {"u": r["username"]})
    if last and now - _ts(last) < INSTANT_EVERY:
        return 0
    pend = [n for n in notices.pending_mail(r["username"], kinds=T.PERSONAL)
            if now - INSTANT_WINDOW <= _ts(n["updated_at"]) <= now - INSTANT_DELAY]
    got = _claim([n["id"] for n in pend])
    pend = [n for n in pend if n["id"] in got]
    if not pend:
        return 0
    mail = T.render_batch(pend, now, private=not is_corporate(r["email"]))
    try:
        deliver(r["username"], "instant", r["email"], mail, consented=r["source"] == "user", n=len(pend))
    except Exception:
        _unclaim(got)
        raise
    return 1


def _workday(d: date) -> bool:
    """Будни без праздников — тот же DIGEST_HOLIDAYS, что у «Новостных обзоров»."""
    hol = {x.strip() for x in os.getenv("DIGEST_HOLIDAYS", "").split(",") if x.strip()}
    return d.weekday() < 5 and d.isoformat() not in hol


def _digest(r: dict, now: datetime) -> int:
    """Утренняя сводка — раз в день: место в журнале занимаем до отправки."""
    day = now.astimezone(MSK).date()
    slot = _scalar("""INSERT INTO app_mail_log (username, kind, to_addr, day, ok, error)
                      VALUES (:u, 'digest', :e, :d, false, 'собирается')
                      ON CONFLICT (username, day) WHERE kind = 'digest' DO NOTHING
                      RETURNING id""", {"u": r["username"], "e": r["email"], "d": day})
    if not slot:
        return 0
    pend = [n for n in notices.pending_mail(r["username"], limit=60)
            if _ts(n["updated_at"]) >= now - DIGEST_WINDOW]
    got = _claim([n["id"] for n in pend])
    pend = [n for n in pend if n["id"] in got]
    if not pend:
        _exec("UPDATE app_mail_log SET ok = true, error = 'нечего отправлять' WHERE id = :id", {"id": slot})
        return 0
    mail = T.render_digest(pend, now, r.get("display_name") or "", private=not is_corporate(r["email"]))
    try:
        deliver(r["username"], "digest", r["email"], mail, consented=r["source"] == "user", bulk=True,
                log_row=False)
    except Exception:
        _unclaim(got)
        _exec("DELETE FROM app_mail_log WHERE id = :id", {"id": slot})     # следующий тик попробует снова
        raise
    _exec("UPDATE app_mail_log SET ok = true, error = NULL, n_items = :n, sent_at = now() WHERE id = :id",
          {"n": len(pend), "id": slot})
    return 1


def tick(now: datetime | None = None) -> dict:
    """Один проход рассылки по всем, кому можно писать."""
    out = {"users": 0, "instant": 0, "digest": 0, "errors": 0}
    if not mailer.configured() or mailer.paused():
        return {**out, "skipped": True}
    now = now or _now()
    msk = now.astimezone(MSK)
    digest_due = _workday(msk.date()) and msk.hour >= DIGEST_HOUR
    for r in recipients():
        out["users"] += 1
        pm = (r.get("prefs") or {}).get("mail") or {}
        for kind, on, fn in (("instant", pm.get("instant", True), _instant),
                             ("digest", digest_due and pm.get("digest", True), _digest)):
            if not on:
                continue
            try:
                out[kind] += fn(r, now)
            except Exception as e:  # noqa: BLE001 — один адрес не держит остальных
                out["errors"] += 1
                log.warning("[mail] %s → %s: %s", kind, r["username"], e)
    return out


async def mail_background_loop() -> None:
    await asyncio.sleep(int(os.getenv("MAIL_START_DELAY_S", "90")))
    log.info("[mail] рассылка: тик раз в %d с, сводка в будни после %02d:00 МСК", TICK_S, DIGEST_HOUR)
    while True:
        try:
            r = await asyncio.to_thread(tick)
            if r.get("instant") or r.get("digest") or r.get("errors"):
                log.info("[mail] рассылка: %s", r)
        except Exception as e:  # noqa: BLE001
            log.warning("[mail] тик рассылки: %s", e)
        await asyncio.sleep(TICK_S)


def stats() -> dict:
    """Для галереи писем: сколько адресов подключено и сколько писем за сутки."""
    try:
        users = _rows("SELECT email FROM app_user WHERE email IS NOT NULL AND email_source = 'user'")
        sent = _one("""SELECT count(*) FILTER (WHERE ok) AS ok, count(*) FILTER (WHERE NOT ok) AS bad
                         FROM app_mail_log WHERE kind <> 'test' AND sent_at > now() - interval '1 day'""") or {}
    except Exception:  # noqa: BLE001 — до миграции 088
        return {}
    corp = sum(1 for u in users if is_corporate(u["email"]))
    return {"users": len(users), "corporate": corp, "private": len(users) - corp,
            "sent_day": int(sent.get("ok") or 0), "failed_day": int(sent.get("bad") or 0)}
