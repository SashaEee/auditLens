-- 076: рабочее место аудитора во вкладке «Отзывы» (волна 4).
--
-- 1. Аудит-дело (021) принимает жалобы: у отзыва нет числового ref_id, и
--    единственность по документу его не защищает от повторного приобщения.
--    Дело можно открыть команде и вести вместе: кто приобщил — видно, убрать
--    может владелец или тот, кто приобщал. Разбор дела моделью хранится при деле.
-- 2. Журнал сигналов: эпизод всплеска — снимок чисел и жалоб, из которых он
--    сложился, и отметка аудитора «подтвердился / ложный». Без него точность
--    сигналов неизвестна.
-- 3. Подписка на сигналы по банку и продукту — для «Для вас».
-- 4. Векторы изложений жалоб — для группировки похожих в ленте. Массив, а не
--    vector: поиск ближайших не нужен, группы считаются в памяти по выборке.

-- ── 1. Аудит-дело ───────────────────────────────────────────────────────────
ALTER TABLE audit_case_item ADD COLUMN IF NOT EXISTS added_by text;
ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS analysis text;
ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS analysis_at timestamptz;
ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS analysis_items int;

CREATE UNIQUE INDEX IF NOT EXISTS audit_case_item_url_uniq
    ON audit_case_item (case_id, kind, url) WHERE ref_id IS NULL AND url IS NOT NULL;

-- ── 2. Журнал сигналов ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signal_journal (
    signal_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bank         text NOT NULL,
    product      text NOT NULL DEFAULT '',   -- '' — все продукты
    issue        text NOT NULL,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    week_end     date,                        -- неделя пика
    level        text,
    stats        jsonb NOT NULL DEFAULT '{}', -- числа пика: неделя, норма, ×, q, рынок
    urls         text[] NOT NULL DEFAULT '{}',-- снимок жалоб сигнала
    verdict      text CHECK (verdict IN ('confirmed', 'false')),
    verdict_by   text,
    verdict_at   timestamptz,
    verdict_note text
);
CREATE INDEX IF NOT EXISTS signal_journal_open
    ON signal_journal (bank, product, issue, last_seen DESC);
CREATE INDEX IF NOT EXISTS signal_journal_seen ON signal_journal (last_seen DESC);

-- ── 3. Подписки ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS review_subscription (
    username   text NOT NULL,
    bank       text NOT NULL,
    product    text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (username, bank, product)
);

-- ── 4. Векторы изложений ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS review_summary_vec (
    url        text PRIMARY KEY,
    vec        real[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
