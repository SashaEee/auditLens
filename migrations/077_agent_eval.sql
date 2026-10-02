-- 077: регрессионный набор ИИ-аналитика (быстрый режим на Hermes).
--
-- Прогон — набор вопросов по всем вкладкам с эталоном, посчитанным в момент
-- прогона теми же функциями, что рисуют вкладки. Нужен, чтобы качество агента
-- мерить числом: после правки навыков и конфига, после самообучения агента
-- (он сам пишет навыки) и при выборе модели. Итог — карточка в «Пульсе».

CREATE TABLE IF NOT EXISTS agent_eval_run (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    engine       text NOT NULL DEFAULT 'hermes',
    model        text,                      -- маршрут модели Hermes или 'default'
    trigger      text,                      -- cli | schedule | admin | gate
    score        numeric,                   -- 0–100: зачёт = 1, частично = 0,5
    n_pass       int,
    n_partial    int,
    n_fail       int,
    median_s     numeric,                   -- медиана времени ответа, с
    cases        jsonb NOT NULL DEFAULT '[]'::jsonb,
    note         text
);

CREATE INDEX IF NOT EXISTS agent_eval_run_started ON agent_eval_run (started_at DESC);
