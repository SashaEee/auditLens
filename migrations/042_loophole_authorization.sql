-- 042: server-side авторизация модуля «Лазейки» (story 1.1).
--
-- Trusted identity приходит только от nginx (X-Authentik-*); membership и
-- роль ccks_expert — авторитетные данные ЭТИХ таблиц, перечитываемые на
-- каждом защищённом запросе: отзыв действует на следующий запрос, без
-- перевыпуска токена. Идемпотентно, диалект Greenplum 6 (без PRIMARY KEY /
-- UNIQUE) — как 012_loophole.sql и последующие миграции модуля.
-- Лимит активных экспертов ЦК КС (не более 5) контролируется на уровне
-- приложения при назначении роли (история 1.5); здесь — только схема.

-- ── Principal (subject из Authentik) ────────────────────────────────────────
-- Зарезервирована под админ-операции истории 1.5 (выдача/отзыв membership и
-- ролей, сводный аудит): код истории 1.1 эту таблицу НЕ читает — проверки
-- идут напрямую по loophole_workspace_membership / loophole_role_assignment.
CREATE TABLE IF NOT EXISTS loophole_principal (
    principal_id  BIGSERIAL,
    username      TEXT NOT NULL,         -- X-Authentik-Username — стабильный ключ
    display_name  TEXT,
    status        TEXT DEFAULT 'active', -- active | disabled
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lp_username ON loophole_principal(username);

-- ── Membership в workspace модуля ───────────────────────────────────────────
-- Активная строка = действующий член модуля: видит «Общую базу» и создание
-- AI-исследования. workspace_id NULL — членство на уровне модуля; выдача
-- и отзыв — административная операция истории 1.5.
CREATE TABLE IF NOT EXISTS loophole_workspace_membership (
    membership_id BIGSERIAL,
    username      TEXT NOT NULL,
    workspace_id  BIGINT,
    status        TEXT DEFAULT 'active', -- active | revoked
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lwm_user ON loophole_workspace_membership(username, status);

-- ── Назначение роли ЦК КС ───────────────────────────────────────────────────
-- role = 'ccks_expert' открывает очередь верификации. Отзыв — status =
-- 'revoked'; проверка перечитывает таблицу на каждом запросе очереди.
CREATE TABLE IF NOT EXISTS loophole_role_assignment (
    assignment_id BIGSERIAL,
    username      TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('ccks_expert', 'module_admin')),
    status        TEXT DEFAULT 'active', -- active | revoked
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lra_user_role ON loophole_role_assignment(username, role, status);

-- ── Обезличенный аудит авторизации ──────────────────────────────────────────
-- Только факты allow/deny без payload: ни текстов запросов, ни данных кейсов.
CREATE TABLE IF NOT EXISTS loophole_auth_audit (
    audit_id    BIGSERIAL,
    username    TEXT NOT NULL,
    action      TEXT NOT NULL,           -- 'membership_check' | 'queue_access'
    decision    TEXT NOT NULL,           -- 'allow' | 'deny'
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_laa_user ON loophole_auth_audit(username, created_at);
