-- 028: полнотекстовое зеркало корпуса отзывов — вторая нога гибридного поиска.
--
-- Зачем. Поиск по отзывам был чисто векторным, и на редких токенах он слеп:
-- «нарушения ПДС» возвращал 0 релевантных из 10, хотя в корпусе 127 таких
-- отзывов по одному только Сберу. Эмбеддинг размывает аббревиатуры. Лечится
-- тем же приёмом, что и база знаний (см. 020_knowledge_fts.sql): второй,
-- словесный поиск, и слияние двух выдач ранговой суммой.
--
-- Почему ЗЕРКАЛО, а не индекс на месте. Корпус живёт в СОСЕДНЕЙ базе `bankiru`,
-- её наполняет чужой процесс, и прав на запись у нас там нет:
-- has_schema_privilege(current_user,'bankiru','CREATE') = false. Считать
-- to_tsvector на лету измерено — 8 секунд по одному банку, это не поиск.
-- Поэтому tsvector считается один раз и хранится у нас.
--
-- Тела отзывов НЕ дублируем: это ещё ~450 МБ, а они и так доступны в `bankiru`
-- по первичному ключу. Зеркало отдаёт review_id, тексты добираются оттуда для
-- полутора десятков строк, которые реально показываются.
--
-- Дедуп по url. Краулер заливал одну жалобу тысячами копий (уникальных url
-- 169 364 из 411 916 строк), и весь тракт отзывов дедуплицирует по url, беря
-- самую свежую строку. Зеркало обязано вести себя так же, иначе полнотекстовая
-- нога вернёт дубли там, где векторная их уже отсеяла. Отсюда url как PRIMARY KEY.

CREATE TABLE IF NOT EXISTS bankiru_review_fts (
    url        TEXT PRIMARY KEY,           -- ключ дедупа, он же ссылка на отзыв
    review_id  BIGINT NOT NULL,            -- bankiru.reviews.id, за телом идём по нему
    bank       TEXT   NOT NULL,            -- каноническое имя банка как в bankiru
    product    TEXT,                       -- метка направления banki.ru
    dt         TIMESTAMP,                  -- datePublished, naive — как в источнике
    city       TEXT,                       -- location до скобки, как в витрине
    tsv        TSVECTOR NOT NULL
);

CREATE INDEX IF NOT EXISTS bankiru_review_fts_tsv_gin
    ON bankiru_review_fts USING GIN (tsv);

-- поиск всегда идёт с выбранным банком, свежесть — второй по частоте фильтр
CREATE INDEX IF NOT EXISTS bankiru_review_fts_bank_dt
    ON bankiru_review_fts (bank, dt DESC);

-- срез по городу используется и в ленте, и в drill-in по аномалии
CREATE INDEX IF NOT EXISTS bankiru_review_fts_city
    ON bankiru_review_fts (city) WHERE city IS NOT NULL;

-- Водяной знак инкрементальной синхронизации. Отдельная таблица, а не rag_cache:
-- rag_cache живёт с TTL и чистится, а потеря водяного знака означает повторный
-- полный бэкфилл на 400 тысяч строк.
CREATE TABLE IF NOT EXISTS bankiru_fts_state (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
