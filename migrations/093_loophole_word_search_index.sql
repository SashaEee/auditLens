-- «Аудит уязвимостей»: поиск по словам в заголовке, фрагменте, заголовке
-- модели и сути (аудит 03.10, УЯЗ-03). Выражение совпадает буква в букву с
-- loophole/repository._HAY_SQL — иначе планировщик индекс не возьмёт.
-- CONCURRENTLY: запускать psql -f БЕЗ -1 (вне транзакции); после — проверить
-- pg_index.indisvalid, при INVALID — DROP INDEX CONCURRENTLY и повторить.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_loophole_record_search_hay_trgm
    ON loophole_record USING gin ((REPLACE(REPLACE(LOWER(COALESCE(title, '') || ' ' || COALESCE(snippet, '') || ' ' || COALESCE(headline, '') || ' ' || COALESCE(summary, '')), 'ё', 'е'), 'э', 'е')) gin_trgm_ops);
