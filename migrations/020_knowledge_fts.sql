-- 020: полнотекстовый поиск по фрагментам — вторая нога гибридного поиска.
--
-- Вектор (BGE-M3) хорошо ловит смысл: «сколько стоит обслуживание» находит
-- «плата за ведение счёта». Но аудитор ищет и буквально: номер предписания,
-- «ПСК», «п. 4.2», точную формулировку из договора. На таких запросах вектор
-- промахивается — эмбеддинг размывает редкие токены. Полнотекст ищет ровно то,
-- что написано. Результаты двух поисков сливаются ранговой суммой (RRF).

-- CAST в regconfig обязателен: без него выбирается перегрузка to_tsvector(text,text),
-- она STABLE, а generated-колонка требует IMMUTABLE — Postgres откажет.
ALTER TABLE document_chunk
    ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS
        (to_tsvector(CAST('russian' AS regconfig), coalesce(text, ''))) STORED;

CREATE INDEX IF NOT EXISTS document_chunk_tsv_gin ON document_chunk USING GIN (tsv);

-- заголовок документа тоже участвует в поиске: «тарифы Газпромбанк» должно
-- находить документ по названию, даже если в теле фрагмента этих слов нет
ALTER TABLE document
    ADD COLUMN IF NOT EXISTS title_tsv tsvector
    GENERATED ALWAYS AS
        (to_tsvector(CAST('russian' AS regconfig), coalesce(title, ''))) STORED;

CREATE INDEX IF NOT EXISTS document_title_tsv_gin ON document USING GIN (title_tsv);

-- фильтры витрины: банк, тип, свежесть — по ним же строятся счётчики фасетов
CREATE INDEX IF NOT EXISTS document_bank_fetched ON document (bank_id, fetched_at DESC);
