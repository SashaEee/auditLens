-- Migration 015: общий каталог парсеров — расписания, аудит владельцев,
-- история запусков, дедуп записей по URL и полному тексту страницы.
-- Идемпотентно, диалект Greenplum 6 (без первичных ключей и уникальных ограничений).

-- ── Расширение loophole_parser ─────────────────────────────────────────────
ALTER TABLE loophole_parser ADD COLUMN IF NOT EXISTS created_by TEXT;
ALTER TABLE loophole_parser ADD COLUMN IF NOT EXISTS last_edited_by TEXT;
ALTER TABLE loophole_parser ADD COLUMN IF NOT EXISTS cron_expr TEXT;          -- NULL = расписание не настроено
ALTER TABLE loophole_parser ADD COLUMN IF NOT EXISTS auto_enabled BOOLEAN DEFAULT false;
ALTER TABLE loophole_parser ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMPTZ;
ALTER TABLE loophole_parser ADD COLUMN IF NOT EXISTS source_keys JSONB;       -- нормализованные ключи дедупа targets
ALTER TABLE loophole_parser ADD COLUMN IF NOT EXISTS heal_attempts INT DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_lp_auto_due ON loophole_parser(auto_enabled, next_run_at);

-- ── История запусков парсеров ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_parser_run (
    run_id        BIGSERIAL,
    parser_id     BIGINT,
    run_trigger   TEXT,                 -- 'manual' | 'cron' | 'heal'
    status        TEXT,                 -- 'running' | 'success' | 'empty' | 'error'
    started_at    TIMESTAMPTZ DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    items_found   INT DEFAULT 0,
    items_new     INT DEFAULT 0,
    items_dup     INT DEFAULT 0,
    error_text    TEXT,
    log_tail      TEXT,                 -- последние ~8 КБ лога
    heal_report   TEXT                  -- отчёт nanobot (run_trigger='heal')
);
CREATE INDEX IF NOT EXISTS idx_lpr_parser ON loophole_parser_run(parser_id);
CREATE INDEX IF NOT EXISTS idx_lpr_status ON loophole_parser_run(status);
CREATE INDEX IF NOT EXISTS idx_lpr_started ON loophole_parser_run(started_at);

-- ── Дедуп записей: полный URL + полный текст страницы ──────────────────────
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS parser_id BIGINT;
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS text_sha256 TEXT;
CREATE INDEX IF NOT EXISTS idx_lr_parser ON loophole_record(parser_id);
CREATE INDEX IF NOT EXISTS idx_lr_url ON loophole_record(url);
CREATE INDEX IF NOT EXISTS idx_lr_text_sha ON loophole_record(text_sha256);
