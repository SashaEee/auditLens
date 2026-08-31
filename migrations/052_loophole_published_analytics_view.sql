-- Allowlisted read-only view аналитики опубликованного каталога (Story 4.3).
CREATE OR REPLACE VIEW loophole_published_catalog_v1 AS
SELECT record_id, title, url, bank_slug, keyword, trust_score, is_loophole,
       verdict_confidence, verdict_reason, status, collected_at, classified_at
FROM loophole_record
WHERE status = 'published' AND is_loophole = TRUE;

-- Локальный setup создаёт роль сам. В managed PostgreSQL роль может создавать
-- только платформа: тогда миграция не должна падать до отдельного DCL-шагa.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'loophole_readonly') THEN
        CREATE ROLE loophole_readonly NOLOGIN;
    END IF;
    GRANT SELECT ON loophole_published_catalog_v1 TO loophole_readonly;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'Роль loophole_readonly должна быть создана и получить GRANT платформой';
END;
$$;
