-- Запускается ОДИН РАЗ при первом старте контейнера (initdb).
-- Включает pgvector — обязательное расширение для семантического поиска.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
