-- Заморозка исходного комментария классификатора loophole_record.
-- Ручной вердикт (POST /records/verdict → repository.update_verdict)
-- перезаписывает verdict_reason комментарием аудитора; эта колонка хранит
-- первый классификаторский текст (заполняется через COALESCE в том же UPDATE,
-- идемпотентно и без backfill). Комментарий участника ЦК живёт в
-- loophole_verification_decision и здесь не дублируется.
ALTER TABLE loophole_record
    ADD COLUMN IF NOT EXISTS classifier_verdict_reason TEXT;
