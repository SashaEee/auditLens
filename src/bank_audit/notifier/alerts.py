"""Эксплуатационные алерты владельцу — письмом.

Раньше модуль слал новые флаги качества через свой SMTP-клиент, который
читал SMTP_PWD (на проде — SMTP_PASSWORD) и делал STARTTLS на порт 465 (там
SSL): не ушло ни одного письма, а простой ночного сбора 25–30.09 нашли
ручным аудитом (аудит 03.10, ПЛТ-01). Теперь:

• отправка — через web/mailer.py (тот же ящик и настройки, что у остальной
  почты; 465 — SSL, 587 — STARTTLS);
• получатели — ALERTS_TO (через запятую), иначе тестовые адреса владельца
  MAIL_TEST_TO;
• содержание — сбои эксплуатации, а не флаги данных (их ~1 000 в сутки):
  ночной сбор не обновлял данные, предохранитель протухания сработал,
  площадка отзывов встала, банк пропал из корпуса;
• одно письмо в сутки на событие (таблица ops_alert_sent, migrations/094).

Запуск: в фоне из FastAPI (web.app:lifespan) и вручную
    python -m bank_audit.notifier.alerts
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = int(os.getenv("ALERTS_INTERVAL_S", "1800"))   # 30 мин
INGEST_STALE_H = float(os.getenv("ALERTS_INGEST_STALE_H", "26"))   # сбор раз в сутки + запас


def recipients() -> list[str]:
    from ..web import mailer
    own = [x.strip().lower() for x in (os.getenv("ALERTS_TO") or "").split(",") if x.strip()]
    return own or mailer.test_recipients()


def ops_events() -> list[dict]:
    """Текущие сбои: [{key, title, detail}]. key стабилен, пока сбой длится."""
    out: list[dict] = []
    with db.session() as s:
        age = s.execute(text("""
            SELECT EXTRACT(epoch FROM now() - max(finished_at)) / 3600.0
              FROM extraction_run WHERE status = 'ok' AND items_seen > 0""")).scalar()
        if age is not None and float(age) > INGEST_STALE_H:
            out.append({"key": "ingest_stale",
                        "title": f"Сбор тарифов не обновлял данные {round(float(age))} ч",
                        "detail": "Последний успешный прогон сборщика с данными — "
                                  f"{round(float(age))} ч назад. Витрина «Рынок» и позиция "
                                  "Сбера стареют. Проверьте «Источники»."})
        for cat, st, ac in s.execute(text("""
                SELECT detail->>'category', max((detail->>'stale')::int), max((detail->>'active')::int)
                  FROM quality_flag
                 WHERE code = 'EXPIRE_BLOCKED' AND created_at > now() - interval '26 hours'
                 GROUP BY 1""")).all():
            out.append({"key": f"expire_blocked:{cat}",
                        "title": f"Категория {cat}: сборщик не видит {st} из {ac} предложений",
                        "detail": "Сработал предохранитель протухания: массовая пропажа похожа на "
                                  "сбой сборщика. Через 14 дней без данных предложения погаснут."})
    try:
        from ..rag import reviews_dash as rd
        h = rd.source_health() or {}
        for src in h.get("sources") or []:
            if src.get("status") == "встал":
                out.append({"key": f"reviews_down:{src['source']}",
                            "title": f"Площадка отзывов «{src.get('label') or src['source']}» встала",
                            "detail": f"За неделю 0 отзывов при норме {src.get('norm')}. "
                                      f"Последний отзыв — {src.get('last_item') or '—'}, "
                                      f"последний прогон — {src.get('last_run_status') or '—'}."})
        for g in h.get("gone_banks") or []:
            if g.get("known"):
                continue
            out.append({"key": f"bank_gone:{g['bank']}",
                        "title": f"Банк «{g['bank']}» пропал из корпуса отзывов",
                        "detail": f"Нет жалоб {g.get('silent_days')} дн. при ожидании "
                                  f"{g.get('expected')}. Возможно, переименован: "
                                  f"{', '.join(g.get('rename_candidates') or []) or 'кандидатов нет'}."})
    except Exception as e:  # noqa: BLE001 — отзывы не мешают остальным алертам
        log.warning("alerts: здоровье площадок недоступно: %s", e)
    return out


def _sent_today(keys: list[str]) -> set[str]:
    with db.session() as s:
        return {r[0] for r in s.execute(text("""
            SELECT key FROM ops_alert_sent WHERE key = ANY(:k)
               AND day = (now() AT TIME ZONE 'Europe/Moscow')::date"""), {"k": keys}).all()}


def _mark_sent(keys: list[str]) -> None:
    with db.session() as s:
        s.execute(text("""
            INSERT INTO ops_alert_sent (key, day)
            VALUES (:k, (now() AT TIME ZONE 'Europe/Moscow')::date)
            ON CONFLICT DO NOTHING"""), [{"k": k} for k in keys])


def _mail(events: list[dict]) -> dict:
    head = events[0]["title"] + (f" и ещё {len(events) - 1}" if len(events) > 1 else "")
    text_ = "\n\n".join(f"• {e['title']}\n  {e['detail']}" for e in events)
    items = "".join(f"<li><b>{_html.escape(e['title'])}</b><br>{_html.escape(e['detail'])}</li>"
                    for e in events)
    return {"subject": f"AuditLens: {head}",
            "text": "Что сломалось в эксплуатации AuditLens:\n\n" + text_,
            "html": f"<p>Что сломалось в эксплуатации AuditLens:</p><ul>{items}</ul>",
            "thread": "ops-alerts"}


def run_once(*_a, force: bool = False) -> dict:
    """Один прогон: собрать сбои, отправить новые за сутки. force — без учёта
    «уже отправляли сегодня» (кнопка «Запустить прогон»)."""
    from ..web import mailer
    events = ops_events()
    if not events:
        return {"ok": True, "sent": 0, "skipped": "сбоев нет"}
    try:
        fresh = events if force else [e for e in events
                                      if e["key"] not in _sent_today([x["key"] for x in events])]
    except Exception as e:  # noqa: BLE001 — без журнала отправок не шлём (иначе письмо каждые 30 мин)
        log.warning("alerts: журнал отправок недоступен: %s", e)
        return {"ok": False, "sent": 0, "error": "нет таблицы ops_alert_sent"}
    if not fresh:
        return {"ok": True, "sent": 0, "skipped": "уже отправляли сегодня", "events": len(events)}
    to = recipients()
    if not (mailer.configured() and to):
        return {"ok": False, "sent": 0, "skipped": "почта или получатели не настроены",
                "events": len(fresh)}
    mail, sent = _mail(fresh), 0
    for addr in to:
        try:
            mailer.send(addr, mail)
            sent += 1
        except mailer.MailError as e:
            log.warning("alerts: %s → %s", addr, e)
    if sent:
        _mark_sent([e["key"] for e in fresh])
    return {"ok": bool(sent), "sent": sent, "events": [e["title"] for e in fresh]}


async def alerts_background_loop():
    """Фоновый цикл для FastAPI lifespan: раз в DEFAULT_INTERVAL_S секунд."""
    await asyncio.sleep(120)       # не дублировать прогон при рестартах
    while True:
        try:
            res = await asyncio.to_thread(run_once)
            if res.get("sent") or not res.get("ok"):
                log.info("alerts tick: %s", res)
        except Exception as e:  # noqa: BLE001
            log.warning("alerts tick failed: %s", e)
        await asyncio.sleep(DEFAULT_INTERVAL_S)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_once(), ensure_ascii=False, indent=2))
