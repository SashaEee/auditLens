-- Канонический server-side результат AI-исследования для безопасного экспорта.
CREATE TABLE IF NOT EXISTS loophole_research_report (
    report_id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL,
    run_id TEXT NOT NULL,
    query_text TEXT NOT NULL,
    result_text TEXT NOT NULL,
    evidence_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_loophole_research_report_workspace
    ON loophole_research_report (workspace_id, report_id DESC);
