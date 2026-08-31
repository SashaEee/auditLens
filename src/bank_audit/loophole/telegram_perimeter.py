"""Проверка доказательств защищённого perimeter Telegram worker-а.

Этот модуль не подменяет внешние проверки декларацией из репозитория. Миграция
и manifest подтверждают намерение, а PASS возможен только по отдельному
staging-артефакту с результатом каждого обязательного контроля.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REQUIRED_STAGING_EVIDENCE: tuple[str, ...] = (
    "principal_allow_deny",
    "oidc_denials",
    "lease_fencing",
    "pii_sanitation",
    "cleanup",
    "secret_rotation",
    "firewall",
    "alert_ownership",
)
"""Обязательная матрица release-evidence для production-like staging."""


def verify_telegram_worker_perimeter(evidence_path: str | None = None) -> dict[str, Any]:
    """Проверяет внешний staging-артефакт и никогда не считает его необязательным.

    Путь задаётся явно либо через ``AUDITLENS_TELEGRAM_WORKER_STAGING_EVIDENCE``.
    Отсутствующий или недоступный артефакт означает ``UNVERIFIED``: локальные
    unit-тесты не могут доказать фактические DCL, OIDC, firewall или rotation.
    """
    configured_path = evidence_path or os.getenv("AUDITLENS_TELEGRAM_WORKER_STAGING_EVIDENCE")
    if not configured_path:
        return _unverified("Не задан внешний staging evidence Telegram worker-а")

    try:
        payload = json.loads(Path(configured_path).read_text(encoding="utf-8"))
    except OSError as exc:
        return _unverified(f"Внешний staging evidence недоступен: {type(exc).__name__}")
    except json.JSONDecodeError:
        return _failed("Внешний staging evidence содержит некорректный JSON")

    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), dict):
        return _failed("Внешний staging evidence не содержит объекта checks")
    checks = payload["checks"]
    missing = [name for name in REQUIRED_STAGING_EVIDENCE if name not in checks]
    if missing:
        return _failed("Missing staging checks: " + ", ".join(missing))
    failed = [name for name in REQUIRED_STAGING_EVIDENCE if checks[name] != "VERIFIED"]
    if failed:
        return _failed("Staging checks не подтверждены: " + ", ".join(failed))
    return {
        "status": "VERIFIED",
        "reason": "Внешний staging evidence подтвердил все обязательные perimeter checks",
        "required_checks": list(REQUIRED_STAGING_EVIDENCE),
    }


def _unverified(reason: str) -> dict[str, Any]:
    return {"status": "UNVERIFIED", "reason": reason, "required_checks": list(REQUIRED_STAGING_EVIDENCE)}


def _failed(reason: str) -> dict[str, Any]:
    return {"status": "FAILED", "reason": reason, "required_checks": list(REQUIRED_STAGING_EVIDENCE)}
