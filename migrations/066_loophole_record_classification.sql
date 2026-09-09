-- Тип находки хранится отдельно от статуса публикации и решения верификации.
-- is_loophole сохраняет совместимый смысл: положительная находка любого типа.
ALTER TABLE loophole_record
    ADD COLUMN IF NOT EXISTS classification TEXT
    CHECK (classification IN ('vulnerability', 'fraud_scheme', 'not_confirmed'));

-- Восстанавливаем тип уже опубликованных схем из зафиксированного решения ЦК КС.
-- Ручная более поздняя маркировка имеет приоритет над исходной публикацией.
UPDATE loophole_record AS record
SET classification = decision.decision
FROM loophole_publication_mapping AS publication
JOIN loophole_verification_decision AS decision
    ON decision.decision_id = publication.decision_id
WHERE publication.record_id = record.record_id
    AND record.classification IS NULL
    AND record.is_loophole = TRUE
    AND COALESCE(record.verdict_model, '') <> 'manual'
    AND decision.decision IN ('vulnerability', 'fraud_scheme');

UPDATE loophole_record
SET classification = CASE
    WHEN is_loophole = TRUE THEN 'vulnerability'
    WHEN is_loophole = FALSE THEN 'not_confirmed'
END
WHERE classification IS NULL AND is_loophole IS NOT NULL;
