-- Submitted snapshot для очереди ЦК КС (Story 2.5).
-- Greenplum 6: без PK/UNIQUE/FK; idempotency обеспечивается сервисом по
-- candidate_id + draft_version + submitted status.
ALTER TABLE loophole_research_source
    ADD COLUMN IF NOT EXISTS access_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE loophole_research_source
    ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS draft_version INTEGER NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS loophole_verification_snapshot (
    snapshot_id BIGSERIAL,
    candidate_id BIGINT NOT NULL,
    research_id BIGINT NOT NULL,
    workspace_id BIGINT NOT NULL,
    draft_version INTEGER NOT NULL,
    case_snapshot JSONB NOT NULL,
    evidence_snapshot JSONB NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted'
);
CREATE INDEX IF NOT EXISTS idx_lvs_candidate_version
    ON loophole_verification_snapshot(candidate_id, draft_version, status);
CREATE INDEX IF NOT EXISTS idx_lvs_workspace_status
    ON loophole_verification_snapshot(workspace_id, status, submitted_at);
