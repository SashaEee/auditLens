-- Файлы, которые ИИ-помощник (агент Hermes) отдаёт пользователю: Excel, CSV, Word, PDF…
--
-- Агент собирает файл у себя в контейнере и отправляет его командой al-share на
-- POST /agent-files (тот же ключ и только локально, как MCP). В ответ он вставляет
-- метку [[FILE:<file_id>]]; обёртка стрима привязывает файл к спросившему
-- (username, session_id, claimed_at), и в чате появляется карточка «Скачать».
-- Непривязанные файлы живут сутки — их чистит следующая загрузка.
-- Только ... IF NOT EXISTS: повтор безопасен.
CREATE TABLE IF NOT EXISTS agent_file (
    file_id    TEXT PRIMARY KEY,            -- 32 hex, случайный
    name       TEXT NOT NULL,               -- имя для пользователя
    mime       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    data       BYTEA NOT NULL,
    username   TEXT,                        -- кому выдан; NULL — ещё не в ответе
    session_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_agent_file_user ON agent_file (username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_file_unclaimed ON agent_file (created_at) WHERE username IS NULL;
