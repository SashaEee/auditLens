-- 015: явные оценки 👍/👎 (двухконтурная обратная связь)
--   kind news/for_you/check → обучение персональных рекомендаций (тема/источник);
--   kind ai_answer          → качество инструмента (разбор дизлайков командой).
-- Идемпотентно: безопасно накатывать повторно.

CREATE TABLE IF NOT EXISTS item_feedback (
    id         bigserial PRIMARY KEY,
    username   text      NOT NULL,
    kind       text      NOT NULL,          -- news | for_you | check | ai_answer
    item_key   text      NOT NULL,          -- url новости / ключ сообщения ИИ
    verdict    smallint  NOT NULL,          -- +1 лайк / -1 дизлайк
    topics     jsonb,                       -- слаги тем на момент оценки (обучение)
    payload    jsonb,                       -- снапшот (title/source | question/reasons/comment)
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (username, kind, item_key)       -- повторный клик = снятие/смена оценки
);

CREATE INDEX IF NOT EXISTS idx_item_feedback_user
    ON item_feedback (username, kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_feedback_kind
    ON item_feedback (kind, verdict, created_at DESC);
