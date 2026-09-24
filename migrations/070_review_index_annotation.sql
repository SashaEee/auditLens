-- Итог LLM-разметки (review_annotation) в индексе отзывов: все агрегаты вкладки,
-- сигналы и обзор считаются одним запросом по review_index, без join.
-- Заполняет review_annotate.apply_to_index(); product и esc тоже переписываются
-- оттуда — метка площадки и регулярка больше не источник.
ALTER TABLE review_index
    ADD COLUMN IF NOT EXISTS kind    text,        -- complaint|mixed|question|praise|junk|dup; NULL — ещё не размечен
    ADD COLUMN IF NOT EXISTS issue   text,        -- главная проблема (код кодификатора)
    ADD COLUMN IF NOT EXISTS issues2 text[],      -- дополнительные проблемы
    ADD COLUMN IF NOT EXISTS ev_date date,        -- когда начались события претензии (если назвал автор)
    ADD COLUMN IF NOT EXISTS ann_at  timestamptz; -- какая запись разметки перенесена

CREATE INDEX IF NOT EXISTS review_index_bank_issue_idx ON review_index (bank, issue, dt);
CREATE INDEX IF NOT EXISTS review_index_kind_dt_idx ON review_index (dt)
    WHERE kind IN ('complaint', 'mixed');

-- «Москва и область» площадка ведёт отдельным городом — столица в географии
-- раскалывалась надвое. Новые строки нормализует bankiru_fts._city.
UPDATE review_index
   SET city = substring(city FROM '^(.+?) и (?:область|[А-ЯЁ][а-яё]+ская область)$')
 WHERE city ~ ' и (область|[А-ЯЁ][а-яё]+ская область)$';
