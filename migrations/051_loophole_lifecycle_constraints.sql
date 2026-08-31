-- DB-side idempotency lifecycle Story 3.3.
-- В отличие от ранних индексов производительности эти уникальные ключи —
-- обязательная последняя линия защиты от concurrent duplicate операций.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lvs_candidate_draft
    ON loophole_verification_snapshot(candidate_id, draft_version);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lvd_snapshot
    ON loophole_verification_decision(snapshot_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lpm_decision
    ON loophole_publication_mapping(decision_id);
