-- Migration 011: модуль loophole — агентные задачи, база знаний (pgvector) и парсеры.
-- Идемпотентно, диалект Greenplum 6 (БЕЗ PRIMARY KEY / UNIQUE-конструкций).
-- Уникальность — app-level dedup и индексы. pgvector: тип vector(1024) требует
-- расширения vector (CREATE EXTENSION IF NOT EXISTS vector) — выполняется отдельно
-- администратором БД, здесь только DDL таблиц.

-- ── Агентные задачи (orchestrator) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_agent_task (
    task_id          BIGSERIAL,
    workspace_id     BIGINT,
    query_text       TEXT,
    enriched_query   TEXT,
    phase            TEXT,              -- 'clarify' | 'plan' | 'search' | 'refine' | 'done'
    status           TEXT,              -- 'running' | 'done' | 'error' | 'paused'
    subtasks         JSONB,
    subtask_results  JSONB,
    iterations       INT DEFAULT 0,
    clarify_questions JSONB,
    clarify_answers  JSONB,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lat_workspace ON loophole_agent_task(workspace_id);
CREATE INDEX IF NOT EXISTS idx_lat_status ON loophole_agent_task(status);
CREATE INDEX IF NOT EXISTS idx_lat_phase ON loophole_agent_task(phase);

-- ── База знаний: примеры (few-shot / reference) ─────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_kb_example (
    example_id   BIGSERIAL,
    title        TEXT,
    description  TEXT,
    category     TEXT,
    embedding    vector(1024),
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lkbe_category ON loophole_kb_example(category);

-- ── База знаний: документы (RAG-чанки) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS loophole_kb_doc (
    doc_id       BIGSERIAL,
    source       TEXT,
    content      TEXT,
    embedding    vector(1024),
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lkbd_source ON loophole_kb_doc(source);

-- ── Парсеры пользовательского кода (workspace-scoped) ──────────────────────
CREATE TABLE IF NOT EXISTS loophole_parser (
    parser_id    BIGSERIAL,
    workspace_id BIGINT,
    name         TEXT,
    code_path    TEXT,
    status       TEXT DEFAULT 'created',  -- 'created' | 'running' | 'done' | 'error'
    config       JSONB,
    created_at   TIMESTAMPTZ DEFAULT now(),
    last_run_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lp_workspace ON loophole_parser(workspace_id);
CREATE INDEX IF NOT EXISTS idx_lp_status ON loophole_parser(status);
