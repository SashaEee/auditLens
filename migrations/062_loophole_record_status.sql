-- Классификация хранится в verdict_* и classified_at; статус описывает публикацию.
-- Старые new/classified и прочие неизвестные значения не означают подтверждение.
UPDATE loophole_record
SET status = 'preliminary'
WHERE status IS NULL OR status NOT IN ('published', 'preliminary');

ALTER TABLE loophole_record ALTER COLUMN status SET DEFAULT 'preliminary';
ALTER TABLE loophole_record ALTER COLUMN status SET NOT NULL;

-- Повторное применение не меняет данные и сохраняет тот же контракт.
ALTER TABLE loophole_record DROP CONSTRAINT IF EXISTS ck_loophole_record_status;
ALTER TABLE loophole_record ADD CONSTRAINT ck_loophole_record_status
    CHECK (status IN ('published', 'preliminary'));
