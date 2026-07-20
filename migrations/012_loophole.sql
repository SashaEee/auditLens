-- Migration 010: модуль loophole — поиск и LLM-анализ лазеек/уязвимостей
-- в продуктах банка. Идемпотентно, диалект Greenplum 6 (без PRIMARY KEY / UNIQUE).
-- Уникальность — через индексы + app-level dedup (sha256) и INSERT ... ON CONFLICT
-- DO NOTHING по индексу там, где это уместно.

-- ── Ключевые слова ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_keyword (
    keyword_id    BIGSERIAL,
    keyword       TEXT NOT NULL,
    category      TEXT,              -- 'seed' | 'refined' | 'manual'
    source        TEXT,              -- 'cbr' | 'forum' | 'auto'
    weight        NUMERIC(3,2) DEFAULT 1.0,
    created_at    TIMESTAMPTZ DEFAULT now(),
    is_active     BOOLEAN DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_lk_keyword ON loophole_keyword(keyword);

-- ── Записи о найденных лазейках ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_record (
    record_id     BIGSERIAL,
    sha256        TEXT NOT NULL,
    title         TEXT,
    url           TEXT,
    snippet       TEXT,
    domain        TEXT,
    trust_score   NUMERIC(3,2),
    fetched_at    TIMESTAMPTZ DEFAULT now(),
    collected_at  TIMESTAMPTZ DEFAULT now(),
    bank_slug     TEXT,
    keyword       TEXT,
    raw_text      TEXT,
    is_loophole   BOOLEAN,
    verdict_confidence NUMERIC(3,2),
    verdict_reason TEXT,
    verdict_model TEXT,
    classified_at TIMESTAMPTZ,
    status        TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_lr_sha ON loophole_record(sha256);
CREATE INDEX IF NOT EXISTS idx_lr_loophole ON loophole_record(is_loophole) WHERE is_loophole IS TRUE;
CREATE INDEX IF NOT EXISTS idx_lr_bank ON loophole_record(bank_slug);

-- ── Пользовательские workspace ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_workspace (
    workspace_id   BIGSERIAL,
    user_id        TEXT NOT NULL,
    name           TEXT,
    created_at     TIMESTAMPTZ DEFAULT now(),
    last_active_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lw_user ON loophole_workspace(user_id);

-- ── Результаты поиска по workspace ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_result (
    result_id     BIGSERIAL,
    workspace_id  BIGINT,
    query_text    TEXT,
    period_from   DATE,
    period_to     DATE,
    bank_slugs    TEXT,
    records       JSONB,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ
);

-- ── Сообщения чата ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_chat_message (
    message_id    BIGSERIAL,
    workspace_id  BIGINT,
    role          TEXT,
    content       TEXT,
    tool_name     TEXT,
    tool_args     JSONB,
    created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lcm_ws ON loophole_chat_message(workspace_id, created_at);

-- ── Аудит-лог действий пользователя ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_action_log (
    log_id        BIGSERIAL,
    user_id       TEXT,
    workspace_id  BIGINT,
    action        TEXT,
    detail        JSONB,
    ip            TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lal_user ON loophole_action_log(user_id, created_at);
