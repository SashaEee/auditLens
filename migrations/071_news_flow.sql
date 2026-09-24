-- Полный поток новостей для «Обзора» (digest/newsflow.py).
-- Раньше пул собирался раз в сутки в 07:00: 40 мест на 13 источников, по 10
-- свежих с источника — обзор видел 5–10 процентов потока, в основном ночные
-- посты. Теперь сбор идёт каждые 20 минут, всё хранится с полным текстом, а
-- отбор оценивает весь поток.
CREATE TABLE IF NOT EXISTS news_item (
    id          bigserial   PRIMARY KEY,
    url_hash    text        NOT NULL UNIQUE,
    url         text        NOT NULL,
    parent_url  text,                    -- пункт, выделенный из сводного поста
    source      text        NOT NULL,
    tag         text,
    cls         text,
    dimension   text,
    ts          timestamptz,             -- время публикации
    first_seen  timestamptz NOT NULL DEFAULT now(),
    title       text,
    body        text,                    -- текст поста или статьи
    body_full   boolean     NOT NULL DEFAULT false,   -- статья подгружена целиком
    image       text,
    -- ступень 1: дешёвая модель по всему потоку
    rel         smallint,                -- 0–10: касается ли розничного банковского бизнеса
    rtype       text,
    s1_at       timestamptz,
    -- склейка: одно событие из разных источников
    emb         vector(1024),
    event_id    bigint,
    -- ступень 2: сильная модель с полным текстом, по событию (на ведущей записи)
    value       smallint,                -- 0–10: повод для проверки аудитора розницы
    s2          jsonb,
    s2_at       timestamptz,
    published_on date
);
CREATE INDEX IF NOT EXISTS news_item_ts_idx     ON news_item (ts DESC);
CREATE INDEX IF NOT EXISTS news_item_event_idx  ON news_item (event_id);
CREATE INDEX IF NOT EXISTS news_item_s1_idx     ON news_item (s1_at) WHERE s1_at IS NULL;
CREATE INDEX IF NOT EXISTS news_item_src_ts_idx ON news_item (source, ts DESC);

CREATE TABLE IF NOT EXISTS news_source_state (
    source      text        PRIMARY KEY,
    last_ok_at  timestamptz,
    last_error  text,
    last_post   bigint,                  -- последний прочитанный пост канала
    items_24h   integer
);

-- Версии выпуска: ручное обновление больше не затирает утренний выпуск
-- бесследно — прежняя версия секции уходит сюда.
CREATE TABLE IF NOT EXISTS daily_digest_archive (
    digest_date  date        NOT NULL,
    section      text        NOT NULL,
    archived_at  timestamptz NOT NULL DEFAULT now(),
    payload      jsonb       NOT NULL,
    status       text,
    generated_at timestamptz,
    PRIMARY KEY (digest_date, section, archived_at)
);
