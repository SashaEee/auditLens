"""Quality alerts → email.

Логика: за интервал N минут смотрим в quality_flag новые записи с severity
in (error, warn). Если есть — формируем письмо со сводкой и шлём через
EmailNotifier. Состояние отправок храним в workspace/alerts_state.json
чтобы не дублировать одни и те же flag_id.

Запуск:
  • в фоне из FastAPI (asyncio loop, см. web.app:lifespan)
  • вручную: python -m bank_audit.notifier.alerts
"""
from __future__ import annotations
import asyncio, json, logging, os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from .. import db
from ..config import Settings
from .email import EmailNotifier

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = int(os.getenv("ALERTS_INTERVAL_S", "1800"))   # 30 мин
DEFAULT_LOOKBACK_M = int(os.getenv("ALERTS_LOOKBACK_M", "60"))    # за час

STATE_FILE_NAME = "alerts_state.json"


def _state_path(settings: Settings) -> Path:
    return settings.workspace_dir / STATE_FILE_NAME


def _load_sent(settings: Settings) -> set[int]:
    p = _state_path(settings)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return set(int(x) for x in data.get("sent_flag_ids", []))
        except Exception:
            pass
    return set()


def _save_sent(settings: Settings, ids: set[int]) -> None:
    p = _state_path(settings)
    # Не храним больше 5000 — TTL естественный, старые флаги не повторятся
    keep = sorted(ids)[-5000:]
    p.write_text(json.dumps({"sent_flag_ids": keep}, ensure_ascii=False))


def _format_email_body(flags: list[dict]) -> tuple[str, str]:
    """Возвращает (subject, body_plain). Только plain — корпоратив часто
    режет HTML."""
    by_sev: dict[str, list[dict]] = {}
    for f in flags:
        by_sev.setdefault(f["severity"], []).append(f)

    n_err  = len(by_sev.get("error", []))
    n_warn = len(by_sev.get("warn", []))
    subject = f"[bank_audit] data quality: {n_err} error, {n_warn} warn"

    lines = [
        f"Сводка по флагам качества за последний интервал:",
        f"  errors: {n_err}",
        f"  warns:  {n_warn}",
        f"  всего:  {len(flags)}",
        "",
    ]
    for sev in ("error", "warn"):
        items = by_sev.get(sev, [])
        if not items:
            continue
        lines.append(f"=== {sev.upper()} ({len(items)}) ===")
        for f in items[:50]:  # первые 50 каждого уровня
            detail = f.get("detail")
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except Exception:
                    pass
            detail_str = json.dumps(detail, ensure_ascii=False)[:200] if detail else ""
            lines.append(
                f"  • {f['code']:<28} {f['entity_type']}#{f['entity_id']}  {detail_str}"
            )
        if len(items) > 50:
            lines.append(f"  ... и ещё {len(items) - 50}")
        lines.append("")

    lines += [
        "—",
        "Это автоматическое уведомление. Подробности — в UI bank_audit_platform → раздел Quality.",
    ]
    return subject, "\n".join(lines)


def collect_new_flags(lookback_minutes: int, sent_ids: set[int]) -> list[dict]:
    with db.session() as s:
        rows = s.execute(text("""
            SELECT flag_id, entity_type, entity_id, severity, code,
                   detail::text AS detail, created_at
              FROM quality_flag
             WHERE created_at > now() - make_interval(mins => :m)
               AND severity IN ('error', 'warn')
             ORDER BY severity DESC, created_at DESC
        """), {"m": lookback_minutes}).mappings().all()
    new_rows = [dict(r) for r in rows if int(r["flag_id"]) not in sent_ids]
    return new_rows


def run_once(settings: Settings, notifier: EmailNotifier) -> dict:
    """Один прогон: проверить флаги, отправить если есть, обновить state.
    Возвращает диагностический dict для логов / endpoint'а.
    """
    sent = _load_sent(settings)
    flags = collect_new_flags(DEFAULT_LOOKBACK_M, sent)
    if not flags:
        return {"ok": True, "sent": 0, "skipped": "no new flags"}

    if not notifier.is_configured():
        log.info("alerts.run_once: %s flags but SMTP not configured — skip", len(flags))
        return {"ok": False, "sent": 0, "skipped": "smtp_not_configured", "flags": len(flags)}

    subject, body = _format_email_body(flags)
    delivered = notifier.send(subject=subject, body=body)
    if delivered:
        new_ids = sent | {int(f["flag_id"]) for f in flags}
        _save_sent(settings, new_ids)
        return {"ok": True, "sent": len(flags), "subject": subject}
    return {"ok": False, "sent": 0, "error": "smtp_send_failed", "flags": len(flags)}


async def alerts_background_loop():
    """Фоновый цикл для FastAPI lifespan. Раз в DEFAULT_INTERVAL_S секунд
    выполняет run_once. Безопасен: любые исключения логируются, цикл
    не падает."""
    settings = Settings.load()
    notifier = EmailNotifier()
    log.info("alerts loop started: interval=%ss, lookback=%sm, configured=%s",
             DEFAULT_INTERVAL_S, DEFAULT_LOOKBACK_M, notifier.is_configured())
    # Небольшая задержка на старте, чтобы не дублировать прогон при рестартах
    await asyncio.sleep(60)
    while True:
        try:
            res = run_once(settings, notifier)
            log.info("alerts tick: %s", res)
        except Exception as e:
            log.warning("alerts tick failed: %s", e)
        await asyncio.sleep(DEFAULT_INTERVAL_S)


# CLI: ручной прогон одного раза
if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.load()
    n = EmailNotifier()
    print(json.dumps(run_once(s, n), ensure_ascii=False, indent=2))
