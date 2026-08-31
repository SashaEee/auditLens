-- Независимая история Telegram ingress и устойчивые checkpoints (Story 6.3).
-- В таблицах нет raw body, replacement map или данных файлов: accepted-текст уже
-- санитизирован worker-сервисом, а сомнительные объекты имеют только metadata.
CREATE TABLE IF NOT EXISTS loophole_telegram_ingestion_run (
    ingestion_run_id BIGSERIAL,
    target_id BIGINT NOT NULL,
    sync_mode TEXT NOT NULL,
    checkpoint_before_json JSONB,
    checkpoint_after_json JSONB,
    accepted_count BIGINT NOT NULL DEFAULT 0,
    quarantined_count BIGINT NOT NULL DEFAULT 0,
    duplicate_count BIGINT NOT NULL DEFAULT 0,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lttir_target_completed
    ON loophole_telegram_ingestion_run (target_id, completed_at);

CREATE TABLE IF NOT EXISTS loophole_telegram_ingress (
    ingress_id BIGSERIAL,
    target_id BIGINT NOT NULL,
    source_identity TEXT NOT NULL,
    source_version TEXT NOT NULL,
    object_kind TEXT NOT NULL,
    sequence_no BIGINT,
    sanitized_text TEXT,
    metadata_json JSONB NOT NULL,
    ingestion_run_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ltti_target_identity_version
    ON loophole_telegram_ingress (target_id, source_identity, source_version);
CREATE INDEX IF NOT EXISTS idx_ltti_target_sequence
    ON loophole_telegram_ingress (target_id, sequence_no);

-- Quarantine хранит только allowlist metadata и код причины; исходный текст и
-- файлы не имеют колонок и поэтому не могут попасть в БД, LLM или audit.
CREATE TABLE IF NOT EXISTS loophole_telegram_ingress_quarantine (
    quarantine_id BIGSERIAL,
    target_id BIGINT NOT NULL,
    source_identity TEXT NOT NULL,
    source_version TEXT NOT NULL,
    object_kind TEXT NOT NULL,
    sequence_no BIGINT,
    metadata_json JSONB NOT NULL,
    reason_code TEXT NOT NULL,
    ingestion_run_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lttq_target_identity_version
    ON loophole_telegram_ingress_quarantine (target_id, source_identity, source_version);
