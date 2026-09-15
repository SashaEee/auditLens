-- Тип находки кандидата исследования: все находки агента (лазейка, «не лазейка»,
-- мошенническая схема) сохраняются в каталог; тип переносится в loophole_record
-- при импорте. NULL — исторические кандидаты до миграции: тип считается по
-- бинарному is_loophole (совместимость с прежним поведением).
ALTER TABLE loophole_research_candidate
    ADD COLUMN IF NOT EXISTS classification TEXT
    CHECK (classification IN ('vulnerability', 'fraud_scheme', 'not_confirmed'));

-- Восстановление типа исторических кандидатов по смыслу is_loophole.
UPDATE loophole_research_candidate
SET classification = CASE WHEN is_loophole = TRUE THEN 'vulnerability' ELSE 'not_confirmed' END
WHERE classification IS NULL;
