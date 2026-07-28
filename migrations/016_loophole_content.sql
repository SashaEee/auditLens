-- Migration 016: полный контент источников в loophole_record.
-- content_status: full | truncated | empty | fetch_failed | legacy (существующие строки —
-- очередь backfill). Идемпотентно, диалект Greenplum 6 (без PRIMARY KEY / UNIQUE).

ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS content_status TEXT DEFAULT 'legacy';
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS raw_text_len INTEGER;
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS raw_text_truncated BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_lr_content_status ON loophole_record(content_status);
