-- Отдельный traceable model verdict кандидата исследования (Story 2.3).
-- Greenplum 6: только ADD COLUMN IF NOT EXISTS, без PK/UNIQUE/FK.
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS model_is_loophole BOOLEAN;
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS model_confidence DOUBLE PRECISION;
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS model_reason TEXT;
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS model_classified_at TIMESTAMPTZ;
-- Эти terminal-поля заполняют Stories 3.1 и 3.2; classifier их не изменяет.
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS manual_verdict BOOLEAN;
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS ccks_decision TEXT;
CREATE INDEX IF NOT EXISTS idx_lrc_research_model
    ON loophole_research_candidate(research_id, model_classified_at);
