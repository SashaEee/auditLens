-- 032: межднёвная память новостной ленты дайджеста.
-- Зачем: окно свежести 48 ч означает, что одна и та же новость легально живёт
-- в двух выпусках подряд, а рубричные заголовки ЦБ (одно название каждый день)
-- и недатированные страницы поиска могли публиковаться бесконечно. Правила:
--   * что уже публиковалось в выпуске (picked) в ПРОШЛЫЕ дни, в пул не берём;
--   * что три разных дня попадало в пул и ни разу не отобрано, перестаём таскать.
-- Сегодняшние picked не исключаются: ручной force-refresh пересобирает тот же
-- выпуск и не должен терять собственные утренние новости.
-- NB: знак процента в комментариях миграций не ставить (exec_driver_sql).
CREATE TABLE IF NOT EXISTS digest_news_seen (
    url_hash   TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT,
    title_norm TEXT,
    source     TEXT,
    first_seen DATE NOT NULL DEFAULT current_date,
    last_seen  DATE NOT NULL DEFAULT current_date,
    times_pool INTEGER NOT NULL DEFAULT 1,
    picked     BOOLEAN NOT NULL DEFAULT FALSE,
    picked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS digest_news_seen_last_idx  ON digest_news_seen (last_seen);
CREATE INDEX IF NOT EXISTS digest_news_seen_title_idx ON digest_news_seen (title_norm);
