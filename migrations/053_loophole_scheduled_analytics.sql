-- Внутренние версионированные контракты именованных аналитических задач (Story 4.4).
-- Raw SQL здесь намеренно отсутствует: он живёт только в серверном реестре.
CREATE TABLE IF NOT EXISTS loophole_scheduled_query (
    scheduled_query_id BIGSERIAL,
    query_id TEXT NOT NULL,
    query_version INTEGER NOT NULL,
    workspace_id BIGINT NOT NULL,
    owner_username TEXT NOT NULL,
    recipient_username TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lsaq_due
    ON loophole_scheduled_query (enabled, next_run_at);

-- Результат доступен только во внутреннем workspace и его явному ACL.
CREATE TABLE IF NOT EXISTS loophole_scheduled_result (
    scheduled_result_id BIGSERIAL,
    scheduled_query_id BIGINT NOT NULL,
    workspace_id BIGINT NOT NULL,
    owner_username TEXT NOT NULL,
    recipient_username TEXT NOT NULL,
    result_json JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lsar_workspace_expiry
    ON loophole_scheduled_result (workspace_id, expires_at);
