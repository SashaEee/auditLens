-- Фрагменты базы знаний вне поиска (аудит 03.10, ДАН-04): хвост «Элементы
-- интерфейса» и меню агрегаторов, разобранные старым парсером. Отдельной
-- таблицей, а не колонкой: строки с векторами не переписываются (HNSW и GIN не
-- трогаются), откат — TRUNCATE. Применять ДО выкладки кода: поиск читает её.
CREATE TABLE IF NOT EXISTS document_chunk_excluded (
    chunk_id   BIGINT PRIMARY KEY REFERENCES document_chunk (chunk_id) ON DELETE CASCADE,
    reason     TEXT NOT NULL,              -- ui_tail | nav_template
    set_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
