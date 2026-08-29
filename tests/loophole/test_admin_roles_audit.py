"""Тест администрирования роли ЦК КС и сводного аудита (story 1.5).

Спека: docs/loophole/bmad/implementation-artifacts/
spec-1-5-администрирование-роли-цк-кс-и-сводного-аудита.md

Покрывает критерии приёмки frozen-интента:
- назначение/отзыв роли ЦК КС только с capability module_admin (server-side
  проверка на каждом админ-endpoint), изменение аудируемо;
- одновременно активны не более пяти экспертов ЦК КС;
- админ-поверхность ограничена управлением ролями, статусом Telegram-целей
  и сводным обезличенным аудитом (без payload и рабочих данных);
- прямой URL без административного права → fail-closed 403 без данных в ответе.

Без сети и реальной БД: in-memory SQLite (паттерн test_authorization.py),
реальный путь trusted principal через заголовки X-Authentik-*. Фронт без
сборки — текстовые проверки (паттерн test_adaptive_context_routes.py).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bank_audit import db as db_mod
from bank_audit.loophole import authorization
from bank_audit.loophole.web import get_session, router

from .conftest import SCHEMA_SQL

SRC = Path(__file__).resolve().parents[2] / "src" / "bank_audit" / "loophole" / "static"
LOOPHOLE_JSX = SRC / "loophole.jsx"
JSX = LOOPHOLE_JSX.read_text(encoding="utf-8")
MIGRATION_043 = (
    Path(__file__).resolve().parents[2] / "migrations" / "043_loophole_ccks_expert_limit.sql"
)

# Маркер защищённых данных: при deny/в сводном аудите его быть не должно.
_SECRET_EXPERT = "secret-expert-ivanov"


@pytest.fixture
def app_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.connection.executescript(SCHEMA_SQL)
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    yield s
    s.close()


@contextmanager
def _engine_session(SessionLocal):
    """Семантика prod `db.session()`: commit при успехе, rollback на исключении."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@pytest.fixture
def client(app_session, monkeypatch):
    """TestClient БЕЗ override авторизации: реальный путь trusted principal.

    log_auth_event пишет аудит через отдельную db.session() — направляем её
    в тестовую БД (prod-семантика: свой commit, независимый от request-сессии).
    """
    engine = app_session.get_bind()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_mod, "session", lambda: _engine_session(SessionLocal))

    def override_session():
        yield app_session

    app = FastAPI()
    app.include_router(router, prefix="/api/loophole")
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as c:
        yield c


# ── Сидирование membership / ролей ──────────────────────────────────────────
def _grant_membership(session, username: str, status: str = "active") -> None:
    session.execute(
        text("INSERT INTO loophole_workspace_membership (username, status) VALUES (:u, :st)"),
        {"u": username, "st": status},
    )


def _grant_role(
    session,
    username: str,
    role: str = "ccks_expert",
    status: str = "active",
) -> None:
    session.execute(
        text("INSERT INTO loophole_role_assignment (username, role, status) VALUES (:u, :r, :st)"),
        {"u": username, "r": role, "st": status},
    )


def _grant_admin(session, username: str) -> None:
    _grant_membership(session, username)
    _grant_role(session, username, role="module_admin")


def _seed_telegram_parser(session) -> None:
    session.execute(
        text(
            "INSERT INTO loophole_parser "
            "(workspace_id, name, code_path, status, source_keys, last_run_at) "
            "VALUES (1, 'tg-parser', 'p.py', 'ok', "
            "'[\"t.me/bank_news\", \"example.com/x\"]', '2026-08-01T10:00:00')"
        )
    )


def _auth(username: str) -> dict:
    return {"X-Authentik-Username": username, "X-Authentik-Name": username}


def _audit_rows(session, username: str) -> list[tuple]:
    rows = session.execute(
        text("SELECT action, decision FROM loophole_auth_audit WHERE username = :u"),
        {"u": username},
    ).all()
    return [(row[0], row[1]) for row in rows]


# ── Контексты: админ-поверхность видна только module_admin ──────────────────
def test_contexts_admin_gets_admin_surface(client, app_session):
    _grant_admin(app_session, "boss")
    r = client.get("/api/loophole/contexts", headers=_auth("boss"))
    assert r.status_code == 200
    contexts = r.json()["contexts"]
    ids = {c["id"] for c in contexts}
    assert "admin" in ids
    admin = next(c for c in contexts if c["id"] == "admin")
    assert admin["title"] == "Администрирование"


def test_contexts_member_without_admin_role_has_no_admin(client, app_session):
    _grant_membership(app_session, "analyst")
    r = client.get("/api/loophole/contexts", headers=_auth("analyst"))
    assert {c["id"] for c in r.json()["contexts"]} == {"catalog", "ai_research"}


def test_contexts_expert_has_queue_but_not_admin(client, app_session):
    _grant_membership(app_session, "expert")
    _grant_role(app_session, "expert")
    r = client.get("/api/loophole/contexts", headers=_auth("expert"))
    ids = {c["id"] for c in r.json()["contexts"]}
    assert "queue" in ids
    assert "admin" not in ids


# ── Управление ролями: server-side capability на каждом endpoint ────────────
def test_admin_roles_requires_admin_capability(client, app_session):
    """Прямой URL без module_admin → 403, данные ролей не возвращаются."""
    _grant_membership(app_session, "analyst")
    _grant_role(app_session, _SECRET_EXPERT)
    r = client.get("/api/loophole/admin/roles", headers=_auth("analyst"))
    assert r.status_code == 403
    assert "roles" not in r.json()
    assert _SECRET_EXPERT not in r.text
    assert ("admin_roles_read", "deny") in _audit_rows(app_session, "analyst")


def test_admin_roles_require_membership(client, app_session):
    """Роль module_admin без membership не открывает поверхность (router guard)."""
    _grant_role(app_session, "ghost", role="module_admin")
    r = client.get("/api/loophole/admin/roles", headers=_auth("ghost"))
    assert r.status_code == 403


def test_admin_roles_lists_ccks_assignments(client, app_session):
    _grant_admin(app_session, "boss")
    _grant_role(app_session, "expert-1")
    _grant_role(app_session, "expert-2")
    _grant_role(app_session, "expert-old", status="revoked")
    r = client.get("/api/loophole/admin/roles", headers=_auth("boss"))
    assert r.status_code == 200
    data = r.json()
    assert data["active_experts"] == 2
    assert data["max_experts"] == 5
    roles = data["roles"]
    # Управляется только роль ЦК КС: назначения module_admin не смешиваются.
    assert {row["role"] for row in roles} == {"ccks_expert"}
    by_user = {row["username"]: row["status"] for row in roles}
    assert by_user["expert-1"] == "active"
    assert by_user["expert-old"] == "revoked"


# ── Назначение роли ──────────────────────────────────────────────────────────
def test_grant_role_assigns_queue_access(client, app_session):
    _grant_admin(app_session, "boss")
    _grant_membership(app_session, "newbie")
    r = client.post(
        "/api/loophole/admin/roles/grant",
        json={"username": "newbie"},
        headers=_auth("boss"),
    )
    assert r.status_code == 200
    assert r.json()["active_experts"] == 1
    # Назначение действует на следующий запрос: очередь и контексты открыты.
    r_ctx = client.get("/api/loophole/contexts", headers=_auth("newbie"))
    assert "queue" in {c["id"] for c in r_ctx.json()["contexts"]}
    r_q = client.get("/api/loophole/queue", headers=_auth("newbie"))
    assert r_q.status_code == 200
    # Изменение роли аудируемо (обезличенно: actor + действие + решение).
    assert ("role_grant", "allow") in _audit_rows(app_session, "boss")


def test_grant_role_limit_five_active_experts(client, app_session):
    """Одновременно активны не более пяти экспертов ЦК КС."""
    _grant_admin(app_session, "boss")
    for i in range(5):
        _grant_role(app_session, f"expert-{i}")
    r = client.post(
        "/api/loophole/admin/roles/grant",
        json={"username": "expert-6"},
        headers=_auth("boss"),
    )
    assert r.status_code == 409
    count = app_session.execute(
        text(
            "SELECT COUNT(*) FROM loophole_role_assignment "
            "WHERE role = 'ccks_expert' AND status = 'active'"
        )
    ).scalar_one()
    assert count == 5
    assert ("role_grant", "deny") in _audit_rows(app_session, "boss")


def test_migration_043_treats_null_status_as_inactive():
    """NULL status не проходит ветку создания active-эксперта и не занимает лимит."""
    sql = MIGRATION_043.read_text(encoding="utf-8")
    assert "CREATE TRIGGER" not in sql
    assert "status = 'active'" in Path(__file__).resolve().parents[2].joinpath(
        "src/bank_audit/loophole/authorization.py"
    ).read_text(encoding="utf-8")


def test_migration_043_fails_closed_on_existing_expert_limit_violation():
    """Накатка не легализует уже нарушенный лимит: preflight-abort
    выполняется до установки функции и trigger."""
    sql = MIGRATION_043.read_text(encoding="utf-8")
    assert "DO $$" in sql
    assert "COUNT(*) > 5" in sql
    assert "RAISE EXCEPTION" in sql
    assert "CREATE TRIGGER" not in sql


def test_migration_043_suppresses_concurrent_duplicate_active_assignment():
    """Повторное конкурентное назначение одного эксперта не создаёт вторую
    активную строку: DB-trigger видит username под тем же advisory lock."""
    MIGRATION_043.read_text(encoding="utf-8")
    source = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("src/bank_audit/loophole/authorization.py")
        .read_text(encoding="utf-8")
    )
    assert "pg_advisory_xact_lock" in source
    assert "has_active_role(target" in source


def test_migration_043_serializes_ccks_expert_limit():
    """Лимит пяти экспертов enforced в БД: конкурентные транзакции
    сериализуются advisory lock до проверки и вставки/реактивации роли."""
    assert MIGRATION_043.exists(), "миграция DB-защиты лимита экспертов отсутствует"
    sql = MIGRATION_043.read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in Path(__file__).resolve().parents[2].joinpath(
        "src/bank_audit/loophole/authorization.py"
    ).read_text(encoding="utf-8")

    assert "CREATE TRIGGER" not in sql


def test_db_trigger_limit_returns_409_and_audits_deny(client, app_session, monkeypatch):
    """Отклонение DB-trigger переводится в 409 без раскрытия DB-деталей,
    а в redacted аудите остаётся только actor, действие и deny."""
    _grant_admin(app_session, "boss")
    audits = []

    class ScalarResult:
        def scalar_one_or_none(self):
            return None

        def scalar_one(self):
            return 4

    class TriggerCheckViolation(Exception):
        sqlstate = "23514"

    class TriggerLimitSession:
        rolled_back = False

        def execute(self, statement, params=None):
            query = str(statement)
            if "SELECT 1 FROM loophole_role_assignment" in query:
                return ScalarResult()
            if "SELECT COUNT(*)" in query:
                return ScalarResult()
            raise IntegrityError(
                "INSERT", params, TriggerCheckViolation("database check violation")
            )

        def rollback(self):
            self.rolled_back = True

    trigger_session = TriggerLimitSession()
    real_grant = authorization.grant_ccks_expert
    monkeypatch.setattr(
        authorization,
        "log_auth_event",
        lambda actor, action, decision: audits.append((actor, action, decision)),
    )
    monkeypatch.setattr(
        authorization,
        "grant_ccks_expert",
        lambda actor, target, *, session: real_grant(actor, target, session=trigger_session),
    )

    response = client.post(
        "/api/loophole/admin/roles/grant",
        json={"username": "expert-5"},
        headers=_auth("boss"),
    )

    assert response.status_code == 409
    assert trigger_session.rolled_back
    assert ("boss", "role_grant", "deny") in audits


def test_successful_grant_audit_uses_role_mutation_session(app_session, monkeypatch):
    """Успешная выдача роли и её audit не расходятся по транзакциям."""

    def unexpected_audit_session():
        raise AssertionError("успешный аудит не должен открывать отдельную сессию")

    monkeypatch.setattr(db_mod, "session", unexpected_audit_session)

    authorization.grant_ccks_expert("boss", "newbie", session=app_session)

    assert (
        app_session.execute(
            text(
                "SELECT COUNT(*) FROM loophole_role_assignment "
                "WHERE username = 'newbie' AND role = 'ccks_expert' AND status = 'active'"
            )
        ).scalar_one()
        == 1
    )
    assert ("role_grant", "allow") in _audit_rows(app_session, "boss")


def test_grant_role_rejects_blank_username_and_trims_valid_value(client, app_session):
    _grant_admin(app_session, "boss")

    blank = client.post(
        "/api/loophole/admin/roles/grant",
        json={"username": "   "},
        headers=_auth("boss"),
    )
    assert blank.status_code == 422

    granted = client.post(
        "/api/loophole/admin/roles/grant",
        json={"username": "  newbie  "},
        headers=_auth("boss"),
    )
    assert granted.status_code == 200
    assert granted.json()["username"] == "newbie"
    assert (
        app_session.execute(
            text(
                "SELECT COUNT(*) FROM loophole_role_assignment "
                "WHERE username = 'newbie' AND role = 'ccks_expert' AND status = 'active'"
            )
        ).scalar_one()
        == 1
    )


def test_successful_revoke_and_admin_audit_use_request_session(client, app_session, monkeypatch):
    _grant_admin(app_session, "boss")
    _grant_role(app_session, "expert")

    def unexpected_audit_session():
        raise AssertionError("успешный аудит не должен открывать отдельную сессию")

    monkeypatch.setattr(db_mod, "session", unexpected_audit_session)
    assert authorization.revoke_ccks_expert("boss", "expert", session=app_session)
    assert ("role_revoke", "allow") in _audit_rows(app_session, "boss")

    response = client.get("/api/loophole/admin/audit", headers=_auth("boss"))
    assert response.status_code == 200
    assert ("admin_audit_read", "allow") in _audit_rows(app_session, "boss")


def test_grant_role_idempotent(client, app_session):
    """Повторное назначение не плодит активных строк (Greenplum без UNIQUE)."""
    _grant_admin(app_session, "boss")
    _grant_membership(app_session, "newbie")
    for _ in range(2):
        r = client.post(
            "/api/loophole/admin/roles/grant",
            json={"username": "newbie"},
            headers=_auth("boss"),
        )
        assert r.status_code == 200
    count = app_session.execute(
        text(
            "SELECT COUNT(*) FROM loophole_role_assignment "
            "WHERE username = 'newbie' AND role = 'ccks_expert' AND status = 'active'"
        )
    ).scalar_one()
    assert count == 1


def test_grant_role_requires_admin(client, app_session):
    _grant_membership(app_session, "analyst")
    _grant_membership(app_session, "newbie")
    r = client.post(
        "/api/loophole/admin/roles/grant",
        json={"username": "newbie"},
        headers=_auth("analyst"),
    )
    assert r.status_code == 403
    count = app_session.execute(text("SELECT COUNT(*) FROM loophole_role_assignment")).scalar_one()
    assert count == 0


# ── Отзыв роли ───────────────────────────────────────────────────────────────
def test_revoke_role_cuts_queue_next_request(client, app_session):
    _grant_admin(app_session, "boss")
    _grant_membership(app_session, "expert")
    _grant_role(app_session, "expert")
    assert client.get("/api/loophole/queue", headers=_auth("expert")).status_code == 200
    r = client.post(
        "/api/loophole/admin/roles/revoke",
        json={"username": "expert"},
        headers=_auth("boss"),
    )
    assert r.status_code == 200
    # Отзыв действует на следующий запрос (роль перечитывается из БД).
    assert client.get("/api/loophole/queue", headers=_auth("expert")).status_code == 403
    assert ("role_revoke", "allow") in _audit_rows(app_session, "boss")


def test_revoke_role_without_active_assignment_404(client, app_session):
    _grant_admin(app_session, "boss")
    _grant_membership(app_session, "newbie")
    r = client.post(
        "/api/loophole/admin/roles/revoke",
        json={"username": "newbie"},
        headers=_auth("boss"),
    )
    assert r.status_code == 404


def test_revoke_role_requires_admin(client, app_session):
    _grant_membership(app_session, "analyst")
    _grant_role(app_session, "expert")
    r = client.post(
        "/api/loophole/admin/roles/revoke",
        json={"username": "expert"},
        headers=_auth("analyst"),
    )
    assert r.status_code == 403
    # Отзыв не состоялся: назначение осталось активным.
    assert (
        app_session.execute(
            text(
                "SELECT COUNT(*) FROM loophole_role_assignment "
                "WHERE username = 'expert' AND status = 'active'"
            )
        ).scalar_one()
        == 1
    )


# ── Сводный обезличенный аудит ───────────────────────────────────────────────
def test_admin_audit_summary_is_redacted(client, app_session):
    """Сводный аудит — агрегаты action/decision/count без username и payload."""
    _grant_admin(app_session, "boss")
    app_session.execute(
        text(
            "INSERT INTO loophole_auth_audit (username, action, decision) "
            "VALUES (:u, 'membership_check', 'deny')"
        ),
        {"u": _SECRET_EXPERT},
    )
    r = client.get("/api/loophole/admin/audit", headers=_auth("boss"))
    assert r.status_code == 200
    # Ни username инициаторов, ни имён экспертов в ответе нет.
    assert _SECRET_EXPERT not in r.text
    assert "boss" not in r.text
    events = r.json()["events"]
    assert events, "сводный аудит пуст"
    for row in events:
        assert set(row) <= {"action", "decision", "count", "last_at"}
    actions = {(row["action"], row["decision"]) for row in events}
    assert ("membership_check", "deny") in actions


def test_admin_audit_read_is_audited(client, app_session):
    _grant_admin(app_session, "boss")
    client.get("/api/loophole/admin/audit", headers=_auth("boss"))
    assert ("admin_audit_read", "allow") in _audit_rows(app_session, "boss")


def test_admin_audit_requires_admin(client, app_session):
    _grant_membership(app_session, "analyst")
    r = client.get("/api/loophole/admin/audit", headers=_auth("analyst"))
    assert r.status_code == 403
    assert "events" not in r.json()


# ── Статус Telegram-целей ────────────────────────────────────────────────────
def test_telegram_targets_status(client, app_session):
    _grant_admin(app_session, "boss")
    _seed_telegram_parser(app_session)
    r = client.get("/api/loophole/admin/telegram-targets", headers=_auth("boss"))
    assert r.status_code == 200
    targets = r.json()["targets"]
    assert len(targets) == 1
    t = targets[0]
    assert t["target"] == "t.me/bank_news"
    assert t["status"] == "ok"
    assert t["last_run_at"]
    # Обычные web-источники и технические поля не смешиваются в поверхность.
    assert "example.com" not in r.text
    assert "code_path" not in t
    assert "config" not in t


def test_telegram_targets_requires_admin(client, app_session):
    _grant_membership(app_session, "analyst")
    _seed_telegram_parser(app_session)
    r = client.get("/api/loophole/admin/telegram-targets", headers=_auth("analyst"))
    assert r.status_code == 403
    assert "t.me/bank_news" not in r.text


# ── Фронт: админ-экран (текстовые проверки, фронт без сборки) ───────────────
def _norm(s: str) -> str:
    """Схлопывает весь whitespace — сравнение не зависит от форматирования."""
    return re.sub(r"\s+", "", s)


def test_admin_route_and_title():
    """Маршрут admin — отдельный рабочий экран с русским заголовком."""
    jsx = _jsx = JSX
    assert 'view === "admin"' in jsx
    assert "Администрирование" in jsx
    # Админ-экран — собственная ветка разметки, не смешанная с каталогом.
    assert _norm('{view==="admin"&&(') in _norm(jsx)


def test_admin_fail_closed_screen_clears_data():
    """401/403 админ-endpoint'ов: ранее загруженные данные очищаются,
    показывается fail-closed экран без деталей отказа."""
    _norm(JSX)
    assert "Нет доступа к администрированию" in JSX
    m = re.search(r"const loadAdmin = useCallback\(async \(\) => \{(.*?)\}, \[\]", JSX, re.DOTALL)
    assert m, "не найдено тело loadAdmin"
    body = m.group(1)
    denied = re.search(r"401.{0,900}?setAdminDenied\(true\)", body, re.DOTALL)
    assert denied, "loadAdmin не обрабатывает 401/403 как fail-closed"
    branch = denied.group(0)
    assert "setAdminRoles(null)" in branch
    assert "setAdminTargets(null)" in branch
    assert "setAdminAudit(null)" in branch


def test_admin_revoke_uses_modal_confirmation():
    """Отзыв роли — через доступную модалку с последствием, не window.confirm."""
    jsx = _norm(JSX)
    assert _norm("const[revokeConfirm,setRevokeConfirm]=useState(null);") in jsx
    assert _norm("useFocusLayer(!!revokeConfirm,") in jsx
    assert 'aria-labelledby="lp-revoke-title"' in JSX
    assert 'id="lp-revoke-title"' in JSX
    # Прямого вызова отзыва без подтверждения нет: кнопка в таблице открывает
    # модалку, revokeRole вызывается только из неё.
    assert "setRevokeConfirm(a.username)" in jsx
    assert "Отозвать" in JSX


def test_admin_expert_limit_displayed():
    """Лимит активных экспертов виден администратору (N из max_experts)."""
    assert "max_experts" in JSX
    assert "active_experts" in JSX


def test_admin_sections_and_states():
    """Три раздела админ-поверхности; ошибка загрузки — поверхность
    с «Повторить», а не toast и не пустое состояние."""
    assert "Роль ЦК КС" in JSX
    assert "Статус Telegram-целей" in JSX
    assert "Сводный аудит" in JSX
    m = re.search(r"adminError\s*\?\s*\((.{0,600})", JSX, re.DOTALL)
    assert m, "нет ветки поверхности ошибки админ-экрана"
    assert "Повторить" in m.group(1)


def test_admin_audit_shows_only_aggregates():
    """Разметка сводного аудита рендерит только агрегаты — без username."""
    m = re.search(r"adminAudit\.map\((.{0,700})", JSX, re.DOTALL)
    assert m, "не найдена разметка таблицы сводного аудита"
    block = m.group(1)
    assert "e.action" in block
    assert "e.decision" in block
    assert "e.count" in block
    assert "username" not in block
