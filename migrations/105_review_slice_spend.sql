-- Траты разметки срезов по дням (rag/review_slices.py): суточный потолок считается по
-- тому, что потрачено сегодня. Раньше — по review_slice.cost_rub строк, созданных
-- сегодня: повтор вчерашней неудачной строки переносил её накопленные траты в
-- сегодняшние, и потолок срабатывал раньше (ревью 04.11.2026).
SET lock_timeout = '3s';
CREATE TABLE IF NOT EXISTS review_slice_spend (
    day    date    NOT NULL,           -- по Москве
    schema text    NOT NULL,
    rub    numeric NOT NULL DEFAULT 0,
    PRIMARY KEY (day, schema)
);
