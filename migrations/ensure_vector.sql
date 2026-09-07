-- ensure_vector.sql — до-создание векторных объектов pgvector.
--
-- Применяется entrypoint'ом на КАЖДЫЙ прогон `migrate` (НЕ через журнал
-- schema_migrations, как и analytics/views.sql), потому что расширение vector
-- ставит суперюзер ОАИТ ОТДЕЛЬНО и, возможно, ПОЗЖЕ первого наката миграций.
-- Идемпотентно (ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS): когда
-- vector ещё нет — тихо пропускает (vector-free фаза); как только суперюзер
-- выполнит CREATE EXTENSION vector — следующий `migrate` до-создаст колонку и индекс.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
    ALTER TABLE document_chunk ADD COLUMN IF NOT EXISTS embedding vector(1024);
    CREATE INDEX IF NOT EXISTS chunk_embedding_hnsw
      ON document_chunk USING hnsw (embedding vector_cosine_ops)
      WITH (m = 16, ef_construction = 64);
    -- «Лазейки»: вектор нужен, чтобы запрос находил запись, даже когда автор
    -- назвал то же самое другими словами. Записей меньше сотни, поэтому
    -- индекса нет намеренно — точный перебор по такой таблице мгновенный, а
    -- HNSW на малых объёмах только портит выдачу под фильтрами.
    ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS embedding vector(1024);
    RAISE NOTICE 'pgvector: document_chunk.embedding, loophole_record.embedding и HNSW-индекс на месте.';
  ELSE
    RAISE NOTICE 'pgvector отсутствует → embedding/HNSW пропущены (vector-free фаза). Суперюзер: CREATE EXTENSION vector, затем повторите `migrate`.';
  END IF;
END $$;
