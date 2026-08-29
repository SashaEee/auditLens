"""Server-side авторизация модуля «Лазейки» (story 1.1, администрирование — 1.5).

Граница доверия: identity приходит только от trusted nginx (заголовки
X-Authentik-*, см. web/auth.py) и устанавливает лишь ЛИЧНОСТЬ principal.
Membership и роли (ccks_expert, module_admin) — авторитетные данные БД
(миграция 042) и перечитываются на КАЖДОМ защищённом запросе: отзыв роли
действует на следующий запрос, без перевыпуска токена. Роль/workspace/
capability из клиентских заголовков не принимаются никогда.

module_admin (story 1.5) — прикладная роль, не DB-superuser: даёт только
управление назначениями ЦК КС (не более пяти активных), статус Telegram-целей
и сводный обезличенный аудит. Изменения ролей аудируются так же обезличенно:
actor + действие + решение, без целевого username и payload.

Отказы фиксируются в обезличенном аудите (loophole_auth_audit): только
username + действие + решение, без payload запроса и данных кейсов.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from .. import db
from ..web.auth import CurrentUser
from . import db_schema as schema

log = logging.getLogger(__name__)

# Единственная прикладная роль истории 1.1: эксперт ЦК КС (очередь верификации).
ROLE_CCKS_EXPERT = "ccks_expert"
# Прикладная роль администратора модуля (story 1.5): управление назначениями
# ЦК КС, статус Telegram-целей и сводный аудит. Не DB-superuser.
ROLE_MODULE_ADMIN = "module_admin"
# Одновременно активных экспертов ЦК КС — не более пяти (миграция 042:
# лимит контролируется на уровне приложения при назначении роли).
MAX_ACTIVE_CCKS_EXPERTS = 5
_CCKS_LOCK_KEY = 1_500_043


class ExpertLimitError(Exception):
    """Превышен лимит одновременно активных экспертов ЦК КС (не более 5)."""


# Рабочие контексты модуля. Заголовки русские — отдаются в UI как есть.
_CONTEXT_CATALOG = {"id": "catalog", "title": "Общая база"}
_CONTEXT_AI_RESEARCH = {"id": "ai_research", "title": "Новое AI-исследование"}
_CONTEXT_QUEUE = {"id": "queue", "title": "Очередь верификации"}
_CONTEXT_ADMIN = {"id": "admin", "title": "Администрирование"}


def is_active_member(username: str, *, session) -> bool:
    """Активная membership-строка = действующий член модуля."""
    return (
        session.execute(
            text(
                f"SELECT 1 FROM {schema.T_MEMBERSHIP} "
                "WHERE username = :u AND status = 'active' LIMIT 1"
            ),
            {"u": username},
        ).scalar_one_or_none()
        is not None
    )


def has_active_role(username: str, role: str, *, session) -> bool:
    """Активное назначение роли (отзыв = status='revoked')."""
    return (
        session.execute(
            text(
                f"SELECT 1 FROM {schema.T_ROLE_ASSIGNMENT} "
                "WHERE username = :u AND role = :r AND status = 'active' LIMIT 1"
            ),
            {"u": username, "r": role},
        ).scalar_one_or_none()
        is not None
    )


def log_auth_event(username: str, action: str, decision: str, *, session=None) -> None:
    """Обезличенный аудит авторизации.

    Успешная бизнес-операция передаёт request-сессию: изменение роли и audit
    фиксируются или откатываются вместе. Отказ передаётся без сессии и пишется
    best-effort отдельно, поскольку request-сессия будет откатана HTTPException.
    """
    statement = text(
        f"INSERT INTO {schema.T_AUTH_AUDIT} (username, action, decision) VALUES (:u, :a, :d)"
    )
    params = {"u": username, "a": action, "d": decision}
    if session is not None:
        session.execute(statement, params)
        return
    try:
        with db.session() as audit_session:
            audit_session.execute(statement, params)
    except SQLAlchemyError as e:
        log.warning("[authorization] не удалось записать аудит %s/%s: %s", action, decision, e)


def require_member(user: CurrentUser, *, session) -> CurrentUser:
    """Граница членства: 401 без trusted principal, 403 без active membership.

    Вызывается ДО чтения любых данных модуля (router-level dependency).
    """
    if not user.authenticated:
        # Заголовков Authentik нет: прямой доступ в обход nginx или локалка.
        raise HTTPException(
            status_code=401,
            detail="Требуется аутентификация через корпоративный SSO",
        )
    if not is_active_member(user.username, session=session):
        log_auth_event(user.username, "membership_check", "deny")
        raise HTTPException(
            status_code=403,
            detail="Нет доступа к модулю «Лазейки»",
        )
    return user


def require_role(
    username: str,
    role: str,
    *,
    action: str,
    session,
    detail: str = "Нет доступа к очереди верификации",
) -> None:
    """Граница роли: 403 без активного назначения. Данные не возвращаются."""
    if not has_active_role(username, role, session=session):
        log_auth_event(username, action, "deny")
        raise HTTPException(status_code=403, detail=detail)


def available_contexts(username: str, *, session) -> list[dict]:
    """Рабочие контексты, доступные члену модуля: каталог и AI-исследование —
    всегда, очередь верификации — только активному эксперту ЦК КС,
    администрирование — только активному module_admin."""
    contexts = [dict(_CONTEXT_CATALOG), dict(_CONTEXT_AI_RESEARCH)]
    if has_active_role(username, ROLE_CCKS_EXPERT, session=session):
        contexts.append(dict(_CONTEXT_QUEUE))
    if has_active_role(username, ROLE_MODULE_ADMIN, session=session):
        contexts.append(dict(_CONTEXT_ADMIN))
    return contexts


# ── Администрирование роли ЦК КС и сводного аудита (story 1.5) ──────────────
def count_active_ccks_experts(*, session) -> int:
    """Число одновременно активных экспертов ЦК КС."""
    return session.execute(
        text(
            f"SELECT COUNT(*) FROM {schema.T_ROLE_ASSIGNMENT} WHERE role = :r AND status = 'active'"
        ),
        {"r": ROLE_CCKS_EXPERT},
    ).scalar_one()


def list_ccks_assignments(*, session) -> list[dict]:
    """Назначения роли ЦК КС (активные первыми) для админ-экрана.

    Только username/статус/даты назначения — управление ролью не требует
    рабочих данных; назначения module_admin сюда не смешиваются.
    """
    rows = (
        session.execute(
            text(
                f"SELECT username, role, status, created_at, updated_at, revoked_at "
                f"FROM {schema.T_ROLE_ASSIGNMENT} WHERE role = :r "
                "ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, username"
            ),
            {"r": ROLE_CCKS_EXPERT},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def grant_ccks_expert(actor: str, target: str, *, session) -> None:
    if (
        getattr(getattr(session, "bind", None), "dialect", None)
        and session.bind.dialect.name != "sqlite"
    ):
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CCKS_LOCK_KEY})
    """Назначение роли ЦК КС. Идемпотентно (повтор — no-op); при превышении
    лимита активных экспертов — ExpertLimitError. Аудит обезличенный:
    actor + role_grant + allow/deny, целевой username в журнал не пишется."""
    if has_active_role(target, ROLE_CCKS_EXPERT, session=session):
        log_auth_event(actor, "role_grant", "allow", session=session)
        return
    if count_active_ccks_experts(session=session) >= MAX_ACTIVE_CCKS_EXPERTS:
        log_auth_event(actor, "role_grant", "deny")
        raise ExpertLimitError(
            f"Лимит активных экспертов ЦК КС — не более {MAX_ACTIVE_CCKS_EXPERTS}"
        )
    # Реактивация отозванного назначения, иначе — новая строка. DB-trigger
    # миграции 043 сериализует конкурентные выдачи и может отклонить шестую.
    try:
        res = session.execute(
            text(
                f"UPDATE {schema.T_ROLE_ASSIGNMENT} SET status = 'active', "
                "updated_at = CURRENT_TIMESTAMP, revoked_at = NULL "
                "WHERE username = :u AND role = :r AND status = 'revoked'"
            ),
            {"u": target, "r": ROLE_CCKS_EXPERT},
        )
        if res.rowcount == 0:
            session.execute(
                text(
                    f"INSERT INTO {schema.T_ROLE_ASSIGNMENT} (username, role, status) "
                    "VALUES (:u, :r, 'active')"
                ),
                {"u": target, "r": ROLE_CCKS_EXPERT},
            )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", getattr(exc.orig, "pgcode", None)) != "23514":
            raise
        session.rollback()
        log_auth_event(actor, "role_grant", "deny")
        raise ExpertLimitError(
            f"Лимит активных экспертов ЦК КС — не более {MAX_ACTIVE_CCKS_EXPERTS}"
        ) from exc
    log_auth_event(actor, "role_grant", "allow", session=session)


def revoke_ccks_expert(actor: str, target: str, *, session) -> bool:
    """Отзыв роли ЦК КС. True — активное назначение отозвано; False — его
    не было (404 на транспорте). Аудит пишется только о состоявшемся отзыве."""
    res = session.execute(
        text(
            f"UPDATE {schema.T_ROLE_ASSIGNMENT} SET status = 'revoked', "
            "updated_at = CURRENT_TIMESTAMP, revoked_at = CURRENT_TIMESTAMP "
            "WHERE username = :u AND role = :r AND status = 'active'"
        ),
        {"u": target, "r": ROLE_CCKS_EXPERT},
    )
    if res.rowcount == 0:
        return False
    log_auth_event(actor, "role_revoke", "allow", session=session)
    return True


def audit_summary(*, session, limit: int = 100) -> list[dict]:
    """Сводный обезличенный аудит: агрегаты action/decision/count + время
    последнего события. Username инициаторов и payload не возвращаются."""
    rows = (
        session.execute(
            text(
                f"SELECT action, decision, COUNT(*) AS count, "
                f"MAX(created_at) AS last_at FROM {schema.T_AUTH_AUDIT} "
                "GROUP BY action, decision ORDER BY last_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
