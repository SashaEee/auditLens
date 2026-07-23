-- 021: происхождение документов, темы, аудит-дело.
--
-- Три задачи разом, потому что они об одном: сделать базу знаний прослеживаемой.
--
-- 1. document_origin — откуда документ взялся. Сейчас узнать нельзя: аудитор
--    видит в базе документ, но не знает, что тот попал туда из его же отчёта
--    неделю назад. Связь именно N:M и отдельной таблицей, а не колонкой:
--    один документ приходит из нескольких отчётов, и report_id в момент
--    загрузки ещё не существует — отчёт сохраняется в конце потока.
--
-- 2. document.topics — содержательная тема (вклады, комиссии, ипотека).
--    Существующий doc_type — это ФОРМАТ файла (html/pdf), матрица
--    «банк × html» аудитору бесполезна. Темы считает classify_url
--    по пути URL, детерминированно и без модели.
--
-- 3. audit_case — подборка доказательств. Сейчас «дело» живёт в localStorage
--    браузера и умирает вместе с вкладкой.

-- ── 1. Происхождение ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS document_origin (
    origin_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- документа может не быть вовсе: попытку записываем и когда загрузка
    -- сорвалась — это единственный способ отличить «капча» от «не искали»
    document_id    int REFERENCES document(document_id) ON DELETE CASCADE,
    url            text NOT NULL,
    kind           text NOT NULL,   -- report | quick | crawl | manual | refresh
    run_id         text,            -- склейка до появления report_id
    report_id      bigint,          -- проставляется постфактум
    username       text,
    session_id     bigint,
    question       text,            -- вопрос, ради которого документ качали
    fetch_mode     text,            -- http | browser: чтобы перепроверка шла так же
    skipped_reason text,            -- captcha | fetch_failed | low_trust | duplicate
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS document_origin_doc  ON document_origin (document_id);
CREATE INDEX IF NOT EXISTS document_origin_run  ON document_origin (run_id);
CREATE INDEX IF NOT EXISTS document_origin_user ON document_origin (username, created_at DESC);
CREATE INDEX IF NOT EXISTS document_origin_url  ON document_origin (url, created_at DESC);

-- ── 2. Темы документа ───────────────────────────────────────────────────────
-- Без DEFAULT: ADD COLUMN с дефолтом переписывает таблицу, без него — только
-- метаданные. Заполняется отдельным скриптом батчами.
ALTER TABLE document ADD COLUMN IF NOT EXISTS topics text[];

-- ── 3. История ревизий ──────────────────────────────────────────────────────
-- Ревизии уже копятся сами: ingest не делает UPDATE, изменившийся текст даёт
-- новую строку с тем же url (уникальность по url+content_sha256). Индекс
-- нужен, чтобы собрать версии документа за один проход.
CREATE INDEX IF NOT EXISTS document_url_fetched ON document (url, fetched_at DESC);

-- ── 4. Аудит-дело ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_case (
    case_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username   text NOT NULL,
    title      text NOT NULL,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_case_item (
    item_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id  bigint NOT NULL REFERENCES audit_case(case_id) ON DELETE CASCADE,
    kind     text NOT NULL,          -- document | review | offer | report
    ref_id   bigint,
    url      text,
    title    text,
    note     text,                   -- зачем аудитор это приобщил
    added_at timestamptz NOT NULL DEFAULT now()
);

-- Своя таблица шеринга, а не общая с отчётами: у report_share нет
-- дискриминатора сущности, и права там считаются по голому report_id —
-- доступ к отчёту №42 молча открыл бы дело №42.
CREATE TABLE IF NOT EXISTS audit_case_share (
    share_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id     bigint NOT NULL REFERENCES audit_case(case_id) ON DELETE CASCADE,
    owner       text NOT NULL,
    shared_with text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

CREATE INDEX IF NOT EXISTS audit_case_user      ON audit_case (username, updated_at DESC);
CREATE INDEX IF NOT EXISTS audit_case_item_case ON audit_case_item (case_id, added_at);
CREATE INDEX IF NOT EXISTS audit_case_share_to  ON audit_case_share (shared_with)
    WHERE revoked_at IS NULL;
-- один и тот же документ не приобщается в дело дважды
CREATE UNIQUE INDEX IF NOT EXISTS audit_case_item_uniq
    ON audit_case_item (case_id, kind, ref_id) WHERE ref_id IS NOT NULL;
