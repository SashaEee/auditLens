-- Устойчивое владение Telegram worker-ом и безопасная операционная история
-- (Story 6.4). Таблицы не хранят адрес цели, ingress-текст, raw body или map PII.

CREATE TABLE IF NOT EXISTS loophole_telegram_worker_global_lease (
    lease_name TEXT NOT NULL,
    owner_id TEXT,
    fence_token BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lttwgl_lease_name
    ON loophole_telegram_worker_global_lease (lease_name);

-- Начальная singleton-строка делает захват lease конкурентно-атомарным UPDATE.
INSERT INTO loophole_telegram_worker_global_lease
    (lease_name, owner_id, fence_token, lease_until)
SELECT 'telegram-worker', NULL, 0, CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM loophole_telegram_worker_global_lease
    WHERE lease_name = 'telegram-worker'
);

CREATE TABLE IF NOT EXISTS loophole_telegram_worker_target_lease (
    target_id BIGINT NOT NULL,
    owner_id TEXT,
    global_fence_token BIGINT NOT NULL DEFAULT 0,
    target_fence_token BIGINT NOT NULL DEFAULT 0,
    lifecycle_fence_token BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lttwtl_target
    ON loophole_telegram_worker_target_lease (target_id);
CREATE INDEX IF NOT EXISTS idx_lttwtl_expiry
    ON loophole_telegram_worker_target_lease (lease_until);

CREATE TABLE IF NOT EXISTS loophole_telegram_worker_attempt (
    attempt_id BIGSERIAL,
    target_id BIGINT NOT NULL,
    owner_id TEXT NOT NULL,
    global_fence_token BIGINT NOT NULL,
    target_fence_token BIGINT NOT NULL,
    lifecycle_fence_token BIGINT NOT NULL,
    sync_mode TEXT NOT NULL,
    checkpoint_before_json JSONB,
    checkpoint_after_json JSONB,
    accepted_count BIGINT NOT NULL DEFAULT 0,
    quarantined_count BIGINT NOT NULL DEFAULT 0,
    duplicate_count BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    lease_until TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lttwa_running_expiry
    ON loophole_telegram_worker_attempt (status, lease_until);
CREATE INDEX IF NOT EXISTS idx_lttwa_target_started
    ON loophole_telegram_worker_attempt (target_id, started_at);

-- Единственный summary для terminalized attempt обеспечивается unique attempt_id.
CREATE TABLE IF NOT EXISTS loophole_telegram_worker_outbox (
    outbox_id BIGSERIAL,
    attempt_id BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lttwo_attempt
    ON loophole_telegram_worker_outbox (attempt_id);

-- Structured journal: режим, checkpoint, счётчики, длительность и code ошибки.
-- Здесь намеренно отсутствуют колонки содержимого Telegram-объектов.
CREATE TABLE IF NOT EXISTS loophole_telegram_worker_journal (
    journal_id BIGSERIAL,
    attempt_id BIGINT,
    target_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    sync_mode TEXT,
    checkpoint_before_json JSONB,
    checkpoint_after_json JSONB,
    accepted_count BIGINT NOT NULL DEFAULT 0,
    quarantined_count BIGINT NOT NULL DEFAULT 0,
    duplicate_count BIGINT NOT NULL DEFAULT 0,
    duration_ms BIGINT,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lttwj_target_created
    ON loophole_telegram_worker_journal (target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lttwj_event_created
    ON loophole_telegram_worker_journal (event_type, created_at);
