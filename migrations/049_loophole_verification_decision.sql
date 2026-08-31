-- Единственное append-only решение ЦК КС для immutable submitted snapshot.
CREATE TABLE IF NOT EXISTS loophole_verification_decision (
    decision_id BIGSERIAL,
    snapshot_id BIGINT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('vulnerability', 'fraud_scheme', 'not_confirmed')),
    comment TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lvd_snapshot ON loophole_verification_decision(snapshot_id);

-- PostgreSQL блокирует изменение append-only решения; Greenplum 6 не умеет
-- пользовательские trigger, поэтому production GP опирается на service-only write path.
DO $migration_049$
BEGIN
    IF version() NOT ILIKE '%Greenplum%' THEN
        EXECUTE $function_049$
            CREATE OR REPLACE FUNCTION loophole_verification_decision_append_only()
            RETURNS TRIGGER LANGUAGE plpgsql AS $body_049$
            BEGIN
                RAISE EXCEPTION 'loophole_verification_decision is append-only';
            END;
            $body_049$;
        $function_049$;
        EXECUTE 'DROP TRIGGER IF EXISTS trg_lvd_append_only ON loophole_verification_decision';
        EXECUTE $trigger_049$
            CREATE TRIGGER trg_lvd_append_only
            BEFORE UPDATE OR DELETE ON loophole_verification_decision
            FOR EACH ROW EXECUTE FUNCTION loophole_verification_decision_append_only();
        $trigger_049$;
    END IF;
END;
$migration_049$;
