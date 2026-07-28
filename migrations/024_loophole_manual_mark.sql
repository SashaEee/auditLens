-- Migration 014: ручная маркировка записей — связь примера KB с записью.
-- record_id связывает loophole_kb_example с loophole_record: нужен для дедупа
-- (повторная маркировка не создаёт дубль) и отката (снятие метки удаляет пример).
-- Идемпотентно, диалект Greenplum 6 (без PRIMARY KEY / UNIQUE).

ALTER TABLE loophole_kb_example ADD COLUMN IF NOT EXISTS record_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_lkbe_record ON loophole_kb_example(record_id);
