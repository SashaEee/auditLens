-- «Суть» записи для вкладки «Уязвимости»: пересказ механизма находки моделью.
-- Составляется ТОЛЬКО для уязвимостей и мошеннических схем (99% базы —
-- «не подтверждено», вызов модели для них не нужен): лениво при первом открытии
-- записи и разовым проходом по существующим находкам. Колонки допускают NULL —
-- у записей без находки суть не составляется.
-- Greenplum 6 / PostgreSQL: только ADD COLUMN IF NOT EXISTS, повтор безопасен.
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS summary_model TEXT;
ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS summarized_at TIMESTAMPTZ;
