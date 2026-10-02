-- «Уязвимости»: индексы поиска и порядка «сначала новые».
--
-- Поиск по базе — LOWER(COALESCE(title|snippet, '')) LIKE '%…%': без индекса
-- по всей базе (80 тыс. записей) список, счётчик и сводка занимали ~1 с каждый.
-- Триграммы pg_trgm (доверенное расширение, ставится владельцем базы).
-- «Сначала новые» — по дате публикации, а без неё — по дате сбора.
--
-- CONCURRENTLY: индекс строится без блокировки записи сборщиком; такой скрипт
-- выполняется вне транзакции (psql -f без -1). Повтор безопасен.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_loophole_record_title_trgm
    ON loophole_record USING gin (LOWER(COALESCE(title, '')) gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_loophole_record_snippet_trgm
    ON loophole_record USING gin (LOWER(COALESCE(snippet, '')) gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_loophole_record_published_or_collected
    ON loophole_record ((COALESCE(published_at, collected_at)) DESC, record_id DESC);
