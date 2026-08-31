-- Дата публикации первоисточника хранится отдельно от даты сбора.
-- NULL означает, что первоисточник не сообщил надёжную дату публикации.
ALTER TABLE loophole_record
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
