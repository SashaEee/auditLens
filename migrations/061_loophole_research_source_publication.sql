-- Дата публикации относится к первоисточнику и может отсутствовать.
-- Дату сбора нельзя использовать как её подмену.
ALTER TABLE loophole_research_source
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
