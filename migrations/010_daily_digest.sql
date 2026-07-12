-- 010: ежедневный дайджест вкладки «Обзор» (утренний брифинг аудитора).
-- Строка = дата × секция: секции генерятся/падают/перегенерируются НЕЗАВИСИМО.
-- История не чистится (365 строк × ~15 КБ/день — копейки) → архив выпусков
-- («что было неделю назад») бесплатно через ?date=.

CREATE TABLE IF NOT EXISTS daily_digest (
    digest_date   date        NOT NULL,           -- «день» выпуска (МСК)
    section       text        NOT NULL,           -- headline|reviews_pulse|reviews_brief|news|tariff_moves|quality_ops|...
    payload       jsonb       NOT NULL,
    status        text        NOT NULL DEFAULT 'ok',
                  -- ok       — секция сгенерирована штатно
                  -- degraded — без LLM-слоя (сырые агрегаты/заголовки)
                  -- stale    — копия прошлого дня (LLM/источник недоступен)
                  -- failed   — не удалось и скопировать нечего
    stale_from    date,                           -- откуда скопировано при status='stale'
    generated_at  timestamptz NOT NULL DEFAULT now(),
    llm_model     text,                           -- NULL для чистых SQL-секций
    tokens_in     int,
    tokens_out    int,
    gen_ms        int,
    error         text,                           -- краткая причина degraded/failed
    PRIMARY KEY (digest_date, section)
);
CREATE INDEX IF NOT EXISTS daily_digest_date ON daily_digest (digest_date DESC);

-- Видимое состояние прогона (бейдж «обновляется» в UI + ручной refresh).
-- НЕ мьютекс: от гонок защищает pg_try_advisory_lock (auto-release при краше).
CREATE TABLE IF NOT EXISTS digest_run (
    digest_date  date PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
    trigger      text NOT NULL,                    -- morning | lazy | manual
    detail       jsonb
);
