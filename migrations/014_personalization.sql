-- 014_personalization.sql — персонализация AuditLens.
-- Идентичность из Authentik (X-Authentik-Username). Схема auditlens (search_path).
-- Идемпотентно (IF NOT EXISTS). PostgreSQL 17. См. docs/PERSONALIZATION_PLAN.md.

-- ── Пользователь ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_user (
    username        text PRIMARY KEY,                 -- X-Authentik-Username
    display_name    text,                             -- декодированное имя
    timezone        text NOT NULL DEFAULT 'Europe/Moscow',
    prefs           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- ручные настройки
    interests       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {counters, pinned, muted}
    profile_note    text,                             -- LLM-нарратив «чем интересуется»
    profile_note_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now()
);

-- ── История чата ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_session (
    session_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username    text NOT NULL,
    title       text,
    pinned      boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_session_user ON chat_session (username, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_message (
    message_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  bigint NOT NULL,
    role        text NOT NULL,                        -- user | assistant
    content     text NOT NULL,
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,    -- sources/mode/force_deep/report_id
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_message_session ON chat_message (session_id, created_at);

-- ── История отчётов (deep-research) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS report (
    report_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username    text NOT NULL,
    session_id  bigint,
    question    text NOT NULL,
    title       text,
    body        text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,    -- charts/ranking/sources/coverage
    banks       text[] NOT NULL DEFAULT '{}',          -- теги для галереи и ре-ранка
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS report_user ON report (username, created_at DESC);

-- ── Шеринг отчётов ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS report_share (
    share_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id    bigint NOT NULL,
    owner        text NOT NULL,
    shared_with  text,                                -- username; NULL = всем пользователям
    created_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz
);
CREATE INDEX IF NOT EXISTS report_share_to ON report_share (shared_with) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS report_share_report ON report_share (report_id) WHERE revoked_at IS NULL;

-- ── Поведение → профиль интересов ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_event (
    event_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username  text NOT NULL,
    ts        timestamptz NOT NULL DEFAULT now(),
    kind      text NOT NULL,                          -- ai_query | drill | report_open | share | tab
    payload   jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS user_event_user ON user_event (username, ts DESC);

-- ── Персональный дайджест (кэш утренней сводки) ──────────────────────────────
CREATE TABLE IF NOT EXISTS personal_digest (
    username     text NOT NULL,
    local_date   date NOT NULL,                       -- локальная дата пользователя
    payload      jsonb NOT NULL,                      -- greeting, lead, for_you[], focus[]
    generated_at timestamptz NOT NULL DEFAULT now(),
    llm_model    text,
    tokens_in    int,
    tokens_out   int,
    PRIMARY KEY (username, local_date)
);
