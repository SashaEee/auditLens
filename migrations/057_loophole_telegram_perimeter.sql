-- Защищённый DB perimeter Telegram worker-а (Story 6.5).
-- Runtime-principals создаются платформой до миграции. Их отсутствие должно
-- остановить controlled deployment, а не превратить DCL в незаметный no-op.
-- Локальный Docker setup работает от роли-владельца и bootstrap'ит NOLOGIN
-- principals сам; managed-платформа может создать их заранее с нужными
-- атрибутами. Без этих ролей нижележащие REVOKE/GRANT некорректны.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'auditlens_app') THEN
        CREATE ROLE auditlens_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'loophole_readonly') THEN
        CREATE ROLE loophole_readonly NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'telegram_worker') THEN
        CREATE ROLE telegram_worker NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingestion_reaper') THEN
        CREATE ROLE ingestion_reaper NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'audit_retention') THEN
        CREATE ROLE audit_retention NOLOGIN;
    END IF;
END;
$$;

CREATE OR REPLACE VIEW loophole_telegram_active_target_v1 AS
SELECT target_id, normalized_address, target_kind, checkpoint_json, generation, fence_token
FROM loophole_telegram_target
WHERE canonical_target_id IS NULL AND lifecycle_status = 'active';

-- Reaper не получает payload: только возраст и идентификаторы попытки.
CREATE OR REPLACE VIEW loophole_telegram_expired_attempt_age_v1 AS
SELECT attempt_id, target_id, sync_mode, started_at, lease_until,
       CURRENT_TIMESTAMP - lease_until AS overdue_for
FROM loophole_telegram_worker_attempt
WHERE status = 'running';

-- Запись worker-а возможна только через SECURITY DEFINER function с точной
-- проверкой global, target и lifecycle fencing. Функция принимает только уже
-- санитизированную проекцию; raw body и replacement map в контракте отсутствуют.
CREATE OR REPLACE FUNCTION loophole_worker_write_sanitized_ingress(
    p_target_id BIGINT,
    p_global_fence_token BIGINT,
    p_target_fence_token BIGINT,
    p_lifecycle_fence_token BIGINT,
    p_source_identity TEXT,
    p_source_version TEXT,
    p_object_kind TEXT,
    p_sequence_no BIGINT,
    p_sanitized_text TEXT,
    p_metadata_json JSONB,
    p_ingestion_run_id BIGINT,
    p_checkpoint_after JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    wrote_row BOOLEAN := FALSE;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM loophole_telegram_worker_global_lease global_lease
        JOIN loophole_telegram_worker_target_lease target_lease
          ON target_lease.target_id = p_target_id
        JOIN loophole_telegram_target target
          ON target.target_id = p_target_id
        WHERE global_lease.lease_name = 'telegram-worker'
          AND global_lease.fence_token = p_global_fence_token
          AND global_lease.lease_until > CURRENT_TIMESTAMP
          AND target_lease.global_fence_token = p_global_fence_token
          AND target_lease.target_fence_token = p_target_fence_token
          AND target_lease.lifecycle_fence_token = p_lifecycle_fence_token
          AND target_lease.lease_until > CURRENT_TIMESTAMP
          AND target.canonical_target_id IS NULL
          AND target.lifecycle_status = 'active'
          AND target.fence_token = p_lifecycle_fence_token
    ) THEN
        RAISE EXCEPTION 'fencing token Telegram worker-а устарел';
    END IF;

    INSERT INTO loophole_telegram_ingress (
        target_id, source_identity, source_version, object_kind, sequence_no,
        sanitized_text, metadata_json, ingestion_run_id
    ) VALUES (
        p_target_id, p_source_identity, p_source_version, p_object_kind, p_sequence_no,
        p_sanitized_text, p_metadata_json, p_ingestion_run_id
    ) ON CONFLICT (target_id, source_identity, source_version) DO NOTHING;
    wrote_row := FOUND;

    IF wrote_row THEN
        UPDATE loophole_telegram_target
        SET checkpoint_json = p_checkpoint_after
        WHERE target_id = p_target_id
          AND fence_token = p_lifecycle_fence_token;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'checkpoint Telegram worker-а устарел';
        END IF;
    END IF;
    RETURN wrote_row;
END;
$function$;

-- System actor закрывает только истёкшие попытки. Его возврат — aggregate,
-- поэтому reaper не читает ingress, journal или payload.
CREATE OR REPLACE FUNCTION loophole_terminalize_expired_attempt(p_limit INTEGER DEFAULT 100)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    terminalized_count INTEGER := 0;
BEGIN
    IF p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'p_limit должен быть в диапазоне 1..1000';
    END IF;
    WITH expired AS (
        SELECT attempt_id
        FROM loophole_telegram_worker_attempt
        WHERE status = 'running' AND lease_until <= CURRENT_TIMESTAMP
        ORDER BY attempt_id
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    ), terminalized AS (
        UPDATE loophole_telegram_worker_attempt attempt
        SET status = 'reaped', finished_at = CURRENT_TIMESTAMP
        FROM expired
        WHERE attempt.attempt_id = expired.attempt_id
        RETURNING attempt.attempt_id, attempt.target_id, attempt.accepted_count,
                  attempt.quarantined_count, attempt.duplicate_count, attempt.checkpoint_after_json
    )
    INSERT INTO loophole_telegram_worker_outbox (attempt_id, target_id, event_type, payload_json)
    SELECT attempt_id, target_id, 'attempt_reaped', jsonb_build_object(
        'accepted_count', accepted_count,
        'quarantined_count', quarantined_count,
        'duplicate_count', duplicate_count,
        'checkpoint_after', checkpoint_after_json,
        'reason', 'lease_expired'
    )
    FROM terminalized
    ON CONFLICT (attempt_id) DO NOTHING;
    GET DIAGNOSTICS terminalized_count = ROW_COUNT;
    RETURN terminalized_count;
END;
$function$;

-- Retention получает только bounded aggregate functions и не читает payload.
CREATE OR REPLACE FUNCTION loophole_purge_agent_audit_before(
    p_cutoff TIMESTAMPTZ,
    p_limit INTEGER DEFAULT 1000
) RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    deleted_count INTEGER := 0;
BEGIN
    IF p_cutoff > CURRENT_TIMESTAMP - INTERVAL '14 days' THEN
        RAISE EXCEPTION 'retention cutoff agent_audit_log должен быть не новее 14 дней';
    END IF;
    IF p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'p_limit должен быть в диапазоне 1..1000';
    END IF;
    DELETE FROM agent_audit_log
    WHERE audit_id IN (
        SELECT audit_id FROM agent_audit_log
        WHERE created_at < p_cutoff
        ORDER BY audit_id
        LIMIT p_limit
    );
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$function$;

CREATE OR REPLACE FUNCTION loophole_purge_ingestion_journal_before(
    p_cutoff TIMESTAMPTZ,
    p_limit INTEGER DEFAULT 1000
) RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    deleted_count INTEGER := 0;
BEGIN
    IF p_cutoff > CURRENT_TIMESTAMP - INTERVAL '90 days' THEN
        RAISE EXCEPTION 'retention cutoff worker journal должен быть не новее 90 дней';
    END IF;
    IF p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'p_limit должен быть в диапазоне 1..1000';
    END IF;
    DELETE FROM loophole_telegram_worker_journal
    WHERE journal_id IN (
        SELECT journal_id FROM loophole_telegram_worker_journal
        WHERE created_at < p_cutoff
        ORDER BY journal_id
        LIMIT p_limit
    );
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$function$;

-- Trigger из migration 044 остаётся append-only для application principals,
-- но разрешает DELETE только во время controlled retention вызова.
CREATE OR REPLACE FUNCTION loophole_agent_audit_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' AND session_user = 'audit_retention' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'agent_audit_log is append-only';
END;
$function$;

-- Следующие функции образуют единственный mutation API runtime worker-а.
CREATE OR REPLACE FUNCTION loophole_worker_acquire_global_lease(p_owner_id TEXT, p_lease_seconds INTEGER)
RETURNS TABLE(fence_token BIGINT) LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    UPDATE loophole_telegram_worker_global_lease
       SET owner_id = p_owner_id, fence_token = fence_token + 1,
           lease_until = CURRENT_TIMESTAMP + make_interval(secs => p_lease_seconds)
     WHERE lease_name = 'telegram-worker'
       AND (lease_until <= CURRENT_TIMESTAMP OR owner_id = p_owner_id)
    RETURNING fence_token;
$function$;

CREATE OR REPLACE FUNCTION loophole_worker_acquire_target_lease(
    p_target_id BIGINT, p_owner_id TEXT, p_global_fence_token BIGINT, p_lease_seconds INTEGER
) RETURNS TABLE(target_fence_token BIGINT, lifecycle_fence_token BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    INSERT INTO loophole_telegram_worker_target_lease (
        target_id, owner_id, global_fence_token, target_fence_token, lifecycle_fence_token, lease_until
    ) SELECT p_target_id, NULL, 0, 0, 0, CURRENT_TIMESTAMP
      WHERE EXISTS (SELECT 1 FROM loophole_telegram_target WHERE target_id = p_target_id)
        AND NOT EXISTS (SELECT 1 FROM loophole_telegram_worker_target_lease WHERE target_id = p_target_id);
    RETURN QUERY
    UPDATE loophole_telegram_worker_target_lease lease
       SET owner_id = p_owner_id, global_fence_token = p_global_fence_token,
           target_fence_token = target_fence_token + 1,
           lifecycle_fence_token = target.fence_token,
           lease_until = CURRENT_TIMESTAMP + make_interval(secs => p_lease_seconds)
      FROM loophole_telegram_target target, loophole_telegram_worker_global_lease global_lease
     WHERE lease.target_id = p_target_id AND target.target_id = p_target_id
       AND global_lease.lease_name = 'telegram-worker' AND global_lease.owner_id = p_owner_id
       AND global_lease.fence_token = p_global_fence_token AND global_lease.lease_until > CURRENT_TIMESTAMP
       AND target.canonical_target_id IS NULL AND target.lifecycle_status = 'active'
       AND (lease.lease_until <= CURRENT_TIMESTAMP OR lease.owner_id = p_owner_id)
    RETURNING lease.target_fence_token, lease.lifecycle_fence_token;
END;
$function$;

CREATE OR REPLACE FUNCTION loophole_worker_start_attempt(
    p_target_id BIGINT, p_owner_id TEXT, p_global_fence_token BIGINT,
    p_target_fence_token BIGINT, p_lifecycle_fence_token BIGINT
) RETURNS TABLE(attempt_id BIGINT, sync_mode TEXT, checkpoint_before JSONB)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE check_before JSONB;
BEGIN
    SELECT checkpoint_json INTO check_before FROM loophole_telegram_target WHERE target_id = p_target_id;
    RETURN QUERY
    INSERT INTO loophole_telegram_worker_attempt (
        target_id, owner_id, global_fence_token, target_fence_token, lifecycle_fence_token,
        sync_mode, checkpoint_before_json, status, lease_until
    ) SELECT p_target_id, p_owner_id, p_global_fence_token, p_target_fence_token,
             p_lifecycle_fence_token, CASE WHEN check_before IS NULL THEN 'initial' ELSE 'incremental' END,
             check_before, 'running', lease.lease_until
      FROM loophole_telegram_worker_target_lease lease
     WHERE lease.target_id = p_target_id AND lease.owner_id = p_owner_id
       AND lease.global_fence_token = p_global_fence_token AND lease.target_fence_token = p_target_fence_token
       AND lease.lifecycle_fence_token = p_lifecycle_fence_token AND lease.lease_until > CURRENT_TIMESTAMP
       AND EXISTS (SELECT 1 FROM loophole_telegram_worker_global_lease global_lease
                   WHERE global_lease.lease_name = 'telegram-worker'
                     AND global_lease.owner_id = p_owner_id
                     AND global_lease.fence_token = p_global_fence_token
                     AND global_lease.lease_until > CURRENT_TIMESTAMP)
    RETURNING loophole_telegram_worker_attempt.attempt_id, loophole_telegram_worker_attempt.sync_mode,
              loophole_telegram_worker_attempt.checkpoint_before_json;
    INSERT INTO loophole_telegram_worker_journal (
        attempt_id, target_id, event_type, sync_mode, checkpoint_before_json, duration_ms
    ) SELECT attempt_id, p_target_id, 'attempt_started', sync_mode, checkpoint_before_json, 0
      FROM loophole_telegram_worker_attempt
     WHERE attempt_id = (SELECT max(attempt_id) FROM loophole_telegram_worker_attempt
                         WHERE target_id = p_target_id AND owner_id = p_owner_id);
END;
$function$;

CREATE OR REPLACE FUNCTION loophole_worker_ingest_batch(
    p_target_id BIGINT, p_owner_id TEXT, p_global_fence_token BIGINT,
    p_target_fence_token BIGINT, p_lifecycle_fence_token BIGINT, p_attempt_id BIGINT, p_items JSONB
) RETURNS TABLE(
    checkpoint_before JSONB, checkpoint_after JSONB, accepted_count BIGINT,
    quarantined_count BIGINT, duplicate_count BIGINT
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    item JSONB;
    batch_run_id BIGINT;
    current_checkpoint JSONB;
    next_checkpoint JSONB;
    inserted_count BIGINT := 0;
    quarantined BIGINT := 0;
    duplicates BIGINT := 0;
    sequence_value BIGINT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM loophole_telegram_worker_global_lease global_lease
        JOIN loophole_telegram_worker_target_lease lease ON lease.target_id = p_target_id
        JOIN loophole_telegram_target target ON target.target_id = p_target_id
        JOIN loophole_telegram_worker_attempt attempt ON attempt.attempt_id = p_attempt_id
        WHERE global_lease.lease_name = 'telegram-worker' AND global_lease.owner_id = p_owner_id
          AND global_lease.fence_token = p_global_fence_token AND global_lease.lease_until > CURRENT_TIMESTAMP
          AND lease.owner_id = p_owner_id AND lease.global_fence_token = p_global_fence_token
          AND lease.target_fence_token = p_target_fence_token AND lease.lifecycle_fence_token = p_lifecycle_fence_token
          AND lease.lease_until > CURRENT_TIMESTAMP AND target.lifecycle_status = 'active'
          AND target.canonical_target_id IS NULL AND target.fence_token = p_lifecycle_fence_token
          AND attempt.target_id = p_target_id AND attempt.status = 'running'
    ) THEN
        RAISE EXCEPTION 'fencing token Telegram worker-а устарел';
    END IF;
    SELECT checkpoint_json INTO current_checkpoint FROM loophole_telegram_target WHERE target_id = p_target_id;
    INSERT INTO loophole_telegram_ingestion_run (
        target_id, sync_mode, checkpoint_before_json, accepted_count, quarantined_count, duplicate_count
    ) SELECT p_target_id, sync_mode, current_checkpoint, 0, 0, 0
      FROM loophole_telegram_worker_attempt WHERE attempt_id = p_attempt_id
    RETURNING ingestion_run_id INTO batch_run_id;
    next_checkpoint := current_checkpoint;
    FOR item IN SELECT value FROM jsonb_array_elements(p_items)
    LOOP
        sequence_value := NULLIF(item->>'sequence', '')::BIGINT;
        IF sequence_value IS NOT NULL AND (
            next_checkpoint IS NULL OR sequence_value > COALESCE((next_checkpoint->>'sequence')::BIGINT, -1)
        ) THEN
            next_checkpoint := jsonb_build_object('sequence', sequence_value);
        END IF;
        IF item->>'quarantine_reason' IS NOT NULL THEN
            INSERT INTO loophole_telegram_ingress_quarantine (
                target_id, source_identity, source_version, object_kind, sequence_no,
                metadata_json, reason_code, ingestion_run_id
            ) VALUES (
                p_target_id, item->>'identity', item->>'version', item->>'object_kind', sequence_value,
                COALESCE(item->'metadata', '{}'::jsonb), item->>'quarantine_reason', batch_run_id
            ) ON CONFLICT (target_id, source_identity, source_version) DO NOTHING;
            IF FOUND THEN quarantined := quarantined + 1; ELSE duplicates := duplicates + 1; END IF;
        ELSE
            INSERT INTO loophole_telegram_ingress (
                target_id, source_identity, source_version, object_kind, sequence_no,
                sanitized_text, metadata_json, ingestion_run_id
            ) VALUES (
                p_target_id, item->>'identity', item->>'version', item->>'object_kind', sequence_value,
                item->>'sanitized_text', COALESCE(item->'metadata', '{}'::jsonb), batch_run_id
            ) ON CONFLICT (target_id, source_identity, source_version) DO NOTHING;
            IF FOUND THEN inserted_count := inserted_count + 1; ELSE duplicates := duplicates + 1; END IF;
        END IF;
    END LOOP;
    UPDATE loophole_telegram_ingestion_run
       SET checkpoint_after_json = next_checkpoint, accepted_count = inserted_count,
           quarantined_count = quarantined, duplicate_count = duplicates
     WHERE ingestion_run_id = batch_run_id;
    UPDATE loophole_telegram_target SET checkpoint_json = next_checkpoint
     WHERE target_id = p_target_id AND fence_token = p_lifecycle_fence_token;
    UPDATE loophole_telegram_worker_attempt
       SET checkpoint_after_json = next_checkpoint, accepted_count = accepted_count + inserted_count,
           quarantined_count = quarantined_count + quarantined, duplicate_count = duplicate_count + duplicates
     WHERE attempt_id = p_attempt_id AND status = 'running';
    INSERT INTO loophole_telegram_worker_journal (
        attempt_id, target_id, event_type, checkpoint_before_json, checkpoint_after_json,
        accepted_count, quarantined_count, duplicate_count, duration_ms
    ) VALUES (p_attempt_id, p_target_id, 'batch_finished', current_checkpoint, next_checkpoint,
              inserted_count, quarantined, duplicates, 0);
    RETURN QUERY SELECT current_checkpoint, next_checkpoint, inserted_count, quarantined, duplicates;
END;
$function$;

CREATE OR REPLACE FUNCTION loophole_worker_complete_attempt(
    p_target_id BIGINT, p_owner_id TEXT, p_global_fence_token BIGINT,
    p_target_fence_token BIGINT, p_lifecycle_fence_token BIGINT, p_attempt_id BIGINT
) RETURNS TABLE(completed BOOLEAN) LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    UPDATE loophole_telegram_worker_attempt attempt SET status = 'completed', finished_at = CURRENT_TIMESTAMP
     WHERE attempt.attempt_id = p_attempt_id AND attempt.target_id = p_target_id AND attempt.status = 'running'
       AND EXISTS (SELECT 1 FROM loophole_telegram_worker_target_lease lease
                   WHERE lease.target_id = p_target_id AND lease.owner_id = p_owner_id
                     AND lease.global_fence_token = p_global_fence_token
                     AND lease.target_fence_token = p_target_fence_token
                     AND lease.lifecycle_fence_token = p_lifecycle_fence_token
                     AND lease.lease_until > CURRENT_TIMESTAMP)
    RETURNING TRUE;
$function$;

CREATE OR REPLACE VIEW loophole_telegram_worker_slo_v1 AS
SELECT target_id FROM loophole_telegram_active_target_v1 target
WHERE NOT EXISTS (SELECT 1 FROM loophole_telegram_worker_journal journal
                  WHERE journal.target_id = target.target_id AND journal.event_type = 'attempt_started'
                    AND journal.created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours');

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM telegram_worker;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM telegram_worker;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM telegram_worker;
REVOKE ALL ON TABLE agent_audit_log FROM telegram_worker;
REVOKE ALL ON TABLE loophole_record FROM telegram_worker;
REVOKE ALL ON TABLE loophole_research FROM telegram_worker;
REVOKE ALL ON TABLE loophole_verification_snapshot FROM telegram_worker;
REVOKE ALL ON TABLE loophole_verification_decision FROM telegram_worker;
REVOKE ALL ON TABLE loophole_publication_mapping FROM telegram_worker;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM loophole_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ingestion_reaper;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM audit_retention;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ingestion_reaper;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM audit_retention;
REVOKE ALL ON FUNCTION loophole_worker_write_sanitized_ingress(
    BIGINT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, BIGINT, TEXT, JSONB, BIGINT, JSONB
) FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_terminalize_expired_attempt(INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_purge_agent_audit_before(TIMESTAMPTZ, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_purge_ingestion_journal_before(TIMESTAMPTZ, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_worker_acquire_global_lease(TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_worker_acquire_target_lease(BIGINT, TEXT, BIGINT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_worker_start_attempt(BIGINT, TEXT, BIGINT, BIGINT, BIGINT) FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_worker_ingest_batch(BIGINT, TEXT, BIGINT, BIGINT, BIGINT, BIGINT, JSONB)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION loophole_worker_complete_attempt(BIGINT, TEXT, BIGINT, BIGINT, BIGINT, BIGINT)
    FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO auditlens_app, loophole_readonly, telegram_worker,
    ingestion_reaper, audit_retention;
-- App-role остаётся владельцем доменных repositories, но retention/DCL ему не выдаются.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE loophole_record, loophole_workspace,
    loophole_result, loophole_chat_message, loophole_agent_task, loophole_action_log,
    loophole_research, loophole_research_source, loophole_research_candidate,
    loophole_verification_snapshot, loophole_verification_decision,
    loophole_publication_mapping, loophole_telegram_target,
    loophole_telegram_workspace_subscription, loophole_telegram_target_audit,
    loophole_telegram_terminal_signal, loophole_scheduled_query,
    loophole_scheduled_result TO auditlens_app;
GRANT INSERT ON TABLE agent_audit_log TO auditlens_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO auditlens_app;

GRANT SELECT ON loophole_published_catalog_v1 TO loophole_readonly;
GRANT SELECT ON loophole_telegram_active_target_v1 TO telegram_worker;
GRANT EXECUTE ON FUNCTION loophole_worker_write_sanitized_ingress(
    BIGINT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, BIGINT, TEXT, JSONB, BIGINT, JSONB
) TO telegram_worker;
GRANT EXECUTE ON FUNCTION loophole_worker_acquire_global_lease(TEXT, INTEGER) TO telegram_worker;
GRANT EXECUTE ON FUNCTION loophole_worker_acquire_target_lease(BIGINT, TEXT, BIGINT, INTEGER)
    TO telegram_worker;
GRANT EXECUTE ON FUNCTION loophole_worker_start_attempt(BIGINT, TEXT, BIGINT, BIGINT, BIGINT)
    TO telegram_worker;
GRANT EXECUTE ON FUNCTION loophole_worker_ingest_batch(
    BIGINT, TEXT, BIGINT, BIGINT, BIGINT, BIGINT, JSONB
) TO telegram_worker;
GRANT EXECUTE ON FUNCTION loophole_worker_complete_attempt(BIGINT, TEXT, BIGINT, BIGINT, BIGINT, BIGINT)
    TO telegram_worker;
GRANT SELECT ON loophole_telegram_worker_slo_v1 TO telegram_worker;
GRANT SELECT ON loophole_telegram_expired_attempt_age_v1 TO ingestion_reaper;
GRANT EXECUTE ON FUNCTION loophole_terminalize_expired_attempt(INTEGER) TO ingestion_reaper;
GRANT EXECUTE ON FUNCTION loophole_purge_agent_audit_before(TIMESTAMPTZ, INTEGER) TO audit_retention;
GRANT EXECUTE ON FUNCTION loophole_purge_ingestion_journal_before(TIMESTAMPTZ, INTEGER)
    TO audit_retention;
