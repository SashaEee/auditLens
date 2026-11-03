-- 104: «сюжеты» — группы похожих жалоб как постоянная сущность (rag/review_stories.py).
--
-- Было. Группы похожих жалоб в ленте «Аудита отзывов» строились на лету
-- (rag/reviews_work.py, _clusters): жадная «звезда» по последним 2500 жалобам,
-- без id. Завтра та же история — другая группа: нельзя сказать «сюжет вырос»,
-- «два сюжета слились», «это новое», нельзя приобщить сюжет к делу.
--
-- Стало. Пакетный проход по банку (review_stories.run) кластеризует векторы
-- изложений и сопоставляет группы со вчерашними сюжетами по пересечению
-- участников: у сюжета постоянный id, состав и журнал событий. Названия пишет
-- отдельный шаг с моделью (to_name / save_name), до него title пустой.
--
-- NB: знак процента в комментариях миграций не ставить — накатываются через
-- exec_driver_sql, psycopg считает его началом плейсхолдера.

-- Таблицы новые, но ссылки на них и индексы не должны вставать в очередь за
-- чужими блокировками (14.10 ALTER за долгой транзакцией подвесил вкладку).
SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS review_story (
    story_id    bigserial   PRIMARY KEY,
    bank        text        NOT NULL,
    title       text,                          -- название; пишет шаг с моделью, до него NULL
    criterion   text,                          -- одна фраза: по какому признаку жалоба в сюжете
    first_seen  timestamptz,                   -- дата самой ранней жалобы сюжета
    last_seen   timestamptz,                   -- дата самой свежей
    n           int         NOT NULL DEFAULT 0, -- жалоб в сюжете за всё время
    issue_main  text,                          -- главная проблема большинства (код кодификатора)
    -- active — есть в последнем прогоне; quiet — затих (жалобы разошлись или
    -- ушли из окна), может вернуться; merged — влился в другой сюжет;
    -- split — распался на несколько
    status      text        NOT NULL DEFAULT 'active',
    medoid_url  text,                          -- жалоба, ближе всех ко всем остальным
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_story_bank
    ON review_story (bank, status, last_seen DESC);
CREATE INDEX IF NOT EXISTS review_story_unnamed
    ON review_story (n DESC) WHERE title IS NULL AND status = 'active';

-- Участники. Жалоба, ушедшая из окна прогона, остаётся в сюжете историей;
-- жалоба внутри окна — ровно в одном действующем сюжете. У влившихся и
-- распавшихся сюжетов состав замирает на момент закрытия.
CREATE TABLE IF NOT EXISTS review_story_member (
    story_id bigint      NOT NULL REFERENCES review_story (story_id) ON DELETE CASCADE,
    url      text        NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    sim      real,                             -- косинус к медоиду на последнем прогоне
    PRIMARY KEY (story_id, url)
);
CREATE INDEX IF NOT EXISTS review_story_member_url ON review_story_member (url);

-- Журнал: appeared (появился), grew (вырос), merged (слился), split (распался),
-- quiet (затих), returned (вернулся), named (назван). detail — числа и ссылки
-- на другие сюжеты: from / into.
CREATE TABLE IF NOT EXISTS review_story_event (
    event_id bigserial   PRIMARY KEY,
    story_id bigint      NOT NULL REFERENCES review_story (story_id) ON DELETE CASCADE,
    kind     text        NOT NULL,
    at       timestamptz NOT NULL DEFAULT now(),
    detail   jsonb       NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS review_story_event_story ON review_story_event (story_id, at DESC);
CREATE INDEX IF NOT EXISTS review_story_event_at ON review_story_event (at DESC);
