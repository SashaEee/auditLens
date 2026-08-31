-- Явная переносимая запись: исследовательский источник остаётся provenance,
-- а `loophole_record` попадает в общий каталог только как preliminary.
CREATE TABLE IF NOT EXISTS loophole_preliminary_import (
    import_id BIGSERIAL,
    research_id BIGINT NOT NULL,
    source_id BIGINT NOT NULL,
    workspace_id BIGINT NOT NULL,
    record_id BIGINT NOT NULL,
    imported_by TEXT NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Greenplum 6 не поддерживает нужный UNIQUE-constraint; уникальный индекс
-- сохраняет идемпотентность на PostgreSQL, а service повторно проверяет source_id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_loophole_preliminary_import_source
    ON loophole_preliminary_import (source_id);
CREATE INDEX IF NOT EXISTS idx_loophole_preliminary_import_record
    ON loophole_preliminary_import (record_id);
CREATE INDEX IF NOT EXISTS idx_loophole_preliminary_import_workspace
    ON loophole_preliminary_import (workspace_id, imported_at DESC);
