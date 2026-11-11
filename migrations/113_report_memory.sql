-- report_memory — память исследования: проверенные факты отчёта ИИ-помощника, сведения об
-- их источниках и то, как был понят вопрос. Следующий вопрос в том же разговоре начинает с
-- них и добирает только недостающее (research/gptr/memory.py).
-- Без внешнего ключа на report: создание не блокирует существующие таблицы; строка без
-- отчёта просто не находится соединением. Только CREATE ... IF NOT EXISTS — повтор безопасен.
CREATE TABLE IF NOT EXISTS report_memory (
    report_id  BIGINT PRIMARY KEY,
    session_id BIGINT,
    username   TEXT NOT NULL,
    question   TEXT NOT NULL DEFAULT '',
    plan       JSONB NOT NULL DEFAULT '{}'::jsonb,
    facts      JSONB NOT NULL DEFAULT '[]'::jsonb,
    sources    JSONB NOT NULL DEFAULT '{}'::jsonb,
    n_facts    INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS report_memory_session_idx
    ON report_memory (session_id, created_at DESC);
