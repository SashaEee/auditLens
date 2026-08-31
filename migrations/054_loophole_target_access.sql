-- Управление workspace-доступом и lifecycle Telegram-цели (Story 6.2).
-- Canonical root имеет NULL canonical_target_id; redirect хранит ID root и
-- отвергается TargetAccessService до любой управляющей mutation.
ALTER TABLE loophole_telegram_target
    ADD COLUMN IF NOT EXISTS canonical_target_id BIGINT;
ALTER TABLE loophole_telegram_target
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE loophole_telegram_target
    ADD COLUMN IF NOT EXISTS generation BIGINT NOT NULL DEFAULT 1;
ALTER TABLE loophole_telegram_target
    ADD COLUMN IF NOT EXISTS fence_token BIGINT NOT NULL DEFAULT 1;
-- Поле зарезервировано для durable checkpoint worker-а Story 6.3. Lifecycle
-- не изменяет и не удаляет его, поэтому reactivation выбирает initial или
-- incremental путь по существующему значению.
ALTER TABLE loophole_telegram_target
    ADD COLUMN IF NOT EXISTS checkpoint_json JSONB;

CREATE INDEX IF NOT EXISTS idx_ltt_canonical_target
    ON loophole_telegram_target (canonical_target_id);

CREATE TABLE IF NOT EXISTS loophole_telegram_workspace_subscription (
    subscription_id BIGSERIAL,
    workspace_id BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    grant_version BIGINT NOT NULL DEFAULT 1,
    intent_sequence BIGINT NOT NULL DEFAULT 1,
    granted_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lttws_workspace_target
    ON loophole_telegram_workspace_subscription (workspace_id, target_id);

-- Аудит содержит субъекта, момент (created_at) и результат; payload и адрес
-- Telegram сюда не копируются.
CREATE TABLE IF NOT EXISTS loophole_telegram_target_audit (
    audit_id BIGSERIAL,
    target_id BIGINT NOT NULL,
    workspace_id BIGINT,
    actor_username TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ltta_target_created
    ON loophole_telegram_target_audit (target_id, created_at);

-- Сигнал фиксирует старый generation/fence перед переходом active(g) в
-- inactive(g+1). Worker со старым lease может только остановиться.
CREATE TABLE IF NOT EXISTS loophole_telegram_terminal_signal (
    terminal_signal_id BIGSERIAL,
    target_id BIGINT NOT NULL,
    generation BIGINT NOT NULL,
    fence_token BIGINT NOT NULL,
    code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ltts_target_generation
    ON loophole_telegram_terminal_signal (target_id, generation);
