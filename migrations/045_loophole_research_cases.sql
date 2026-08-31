-- Изолированные результаты AI-исследования; не публикуются в loophole_record.
-- Greenplum 6: без PRIMARY KEY / UNIQUE / FOREIGN KEY, целостность источника
-- и текущего research_id обеспечивает серверный сервис ResearchCaseService.
CREATE TABLE IF NOT EXISTS loophole_research (
    research_id BIGSERIAL,
    workspace_id BIGINT NOT NULL,
    run_id TEXT NOT NULL,
    query_text TEXT NOT NULL,
    search_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS loophole_research_source (
    source_id BIGSERIAL,
    research_id BIGINT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    extracted_text TEXT,
    status TEXT NOT NULL DEFAULT 'fetched',
    limitation_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS loophole_research_candidate (
    candidate_id BIGSERIAL,
    research_id BIGINT NOT NULL,
    source_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    evidence TEXT NOT NULL,
    category TEXT,
    description TEXT NOT NULL,
    severity TEXT NOT NULL,
    is_loophole BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_loophole_research_workspace ON loophole_research(workspace_id);
CREATE INDEX IF NOT EXISTS idx_loophole_research_source_research ON loophole_research_source(research_id);
CREATE INDEX IF NOT EXISTS idx_loophole_research_candidate_research ON loophole_research_candidate(research_id);
