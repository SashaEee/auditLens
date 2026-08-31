-- Идемпотентная публикация подтверждённого решения (Story 3.2).
CREATE TABLE IF NOT EXISTS loophole_publication_mapping (
    publication_id BIGSERIAL,
    decision_id BIGINT NOT NULL,
    command_key TEXT NOT NULL,
    record_id BIGINT,
    status TEXT NOT NULL DEFAULT 'publishing',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lpm_command_key ON loophole_publication_mapping(command_key);
CREATE INDEX IF NOT EXISTS idx_lpm_decision ON loophole_publication_mapping(decision_id);
