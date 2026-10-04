-- Разбор дела помнит СОСТАВ, по которому написан (аудит 03.10, ДЕЛ-04).
-- Раньше хранилось только число материалов: убрали один и добавили другой —
-- разбор считался свежим, а его ссылки [N] (номер по порядку приобщения)
-- после удаления указывали на чужие материалы, в том числе в выгрузке.
ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS analysis_item_ids BIGINT[];
ALTER TABLE audit_case_analysis ADD COLUMN IF NOT EXISTS item_ids BIGINT[];
