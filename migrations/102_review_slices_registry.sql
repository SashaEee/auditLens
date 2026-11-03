-- Срезы «Аудита отзывов» как реестр (rag/review_slices.py SLICES), а не три литерала.
-- 03.11.2026: новые определения «входит / не входит» (версия s7) и новые срезы
-- направлений аудита — самозанятые, дети и молодёжь, участники СВО.
--  • ключ разметки — (url, версия): новая версия не затирает прежнюю до переключения;
--  • review_slice_active — какая версия каждого среза идёт в индекс и во вкладку;
--  • evidence — пункт определения и дословная цитата: «почему жалоба в срезе»;
--  • review_slice_feedback — отметки аудиторов «не относится» (решение — после нашей проверки);
--  • al_product_is знает все срезы реестра (тест сверяет подписи с кодом).

-- review_index и review_slice горячие: не дождались блокировки за 3 с — ошибка, повторить позже.
SET lock_timeout = '3s';

ALTER TABLE review_slice ADD COLUMN IF NOT EXISTS evidence jsonb;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'review_slice_pkey'
                 AND pg_get_constraintdef(oid) = 'PRIMARY KEY (url)') THEN
        ALTER TABLE review_slice DROP CONSTRAINT review_slice_pkey;
        ALTER TABLE review_slice ADD CONSTRAINT review_slice_pkey PRIMARY KEY (url, schema);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS review_slice_url ON review_slice (url);

CREATE TABLE IF NOT EXISTS review_slice_active (
    key         text PRIMARY KEY,
    schema      text NOT NULL,
    switched_at timestamptz NOT NULL DEFAULT now(),
    note        text
);
-- прежние срезы остаются на s6, пока s7 не догнал разметку и не прошёл замер на
-- эталоне; новые есть только в s7
INSERT INTO review_slice_active (key, schema, note) VALUES
    ('payroll', 's6', 'до переключения на s7 по замеру'),
    ('us', 's6', 'до переключения на s7 по замеру'),
    ('field', 's6', 'до переключения на s7 по замеру'),
    ('selfemp', 's7', 'новый срез 03.11.2026'),
    ('youth', 's7', 'новый срез 03.11.2026'),
    ('svo', 's7', 'новый срез 03.11.2026')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS review_slice_feedback (
    id          bigserial PRIMARY KEY,
    url         text NOT NULL,
    key         text NOT NULL,
    username    text NOT NULL,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    decision    text,                    -- NULL — ждёт проверки; excluded | kept
    decided_by  text,
    decided_at  timestamptz,
    UNIQUE (url, key, username)
);
CREATE INDEX IF NOT EXISTS review_slice_feedback_open ON review_slice_feedback (created_at)
    WHERE decision IS NULL;

-- Фильтр продукта: настоящий продукт или срез. Подписи — review_slices.SLICES.
CREATE OR REPLACE FUNCTION al_product_is(product text, slices text[], want text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT product = want
        OR (want = 'Зарплатные проекты' AND 'payroll' = ANY(coalesce(slices, '{}')))
        OR (want = 'УС — банкоматы и терминалы' AND 'us' = ANY(coalesce(slices, '{}')))
        OR (want = 'Выездной сервис — курьеры и выезд' AND 'field' = ANY(coalesce(slices, '{}')))
        OR (want = 'Самозанятые и «Своё дело»' AND 'selfemp' = ANY(coalesce(slices, '{}')))
        OR (want = 'Дети и молодёжь' AND 'youth' = ANY(coalesce(slices, '{}')))
        OR (want = 'Участники СВО' AND 'svo' = ANY(coalesce(slices, '{}')))
$$;
