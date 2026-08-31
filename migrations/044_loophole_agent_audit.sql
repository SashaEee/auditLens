-- Redacted аудит запусков управляемого loophole-агента.
-- Сырые prompt/result, аргументы tools и секреты в таблицу не записываются.
CREATE TABLE IF NOT EXISTS agent_audit_log (
    audit_id        BIGSERIAL,
    run_id          TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    workspace_id    BIGINT,
    query_redacted  TEXT NOT NULL,
    tools_used      JSONB NOT NULL,
    duration_ms     INTEGER NOT NULL,
    result_redacted TEXT NOT NULL,
    status          TEXT NOT NULL,
    error_code      TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_run ON agent_audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_audit_user ON agent_audit_log(user_id, created_at);

-- Greenplum 6 применяет таблицу и индексы выше, но не поддерживает
-- пользовательские triggers. Append-only триггер создаётся только в PostgreSQL.
DO $migration_044$
BEGIN
    IF version() NOT ILIKE '%Greenplum%' THEN
        EXECUTE $function_044$
            CREATE OR REPLACE FUNCTION loophole_agent_audit_append_only()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $body_044$
            BEGIN
                RAISE EXCEPTION 'agent_audit_log is append-only';
            END;
            $body_044$;
        $function_044$;

        EXECUTE 'DROP TRIGGER IF EXISTS trg_agent_audit_append_only ON agent_audit_log';
        EXECUTE $trigger_044$
            CREATE TRIGGER trg_agent_audit_append_only
                BEFORE UPDATE OR DELETE ON agent_audit_log
                FOR EACH ROW EXECUTE FUNCTION loophole_agent_audit_append_only();
        $trigger_044$;
    END IF;
END;
$migration_044$;

REVOKE UPDATE ON TABLE agent_audit_log FROM PUBLIC;
REVOKE DELETE ON TABLE agent_audit_log FROM PUBLIC;
REVOKE TRUNCATE ON TABLE agent_audit_log FROM PUBLIC;
