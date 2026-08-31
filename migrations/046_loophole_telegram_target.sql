-- Идемпотентный реестр Telegram-целей (Story 6.1).
CREATE TABLE IF NOT EXISTS loophole_telegram_target (
    target_id BIGSERIAL,
    normalized_address TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Уникальный индекс необходим для race-safe idempotent register(): при
-- конкурентной вставке TargetRegistryService перехватывает IntegrityError и
-- возвращает уже существующую цель.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ltt_normalized_address
    ON loophole_telegram_target(normalized_address);
