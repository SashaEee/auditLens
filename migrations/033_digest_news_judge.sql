-- 033: ночная LLM-оценка качества новостного выпуска (этап 6).
-- Одна строка на день: судья со строгой рубрикой оценивает КАЖДУЮ
-- опубликованную позицию; junk = score 0-3, borderline = 4-5, relevant = 6-10.
-- Витрина — вкладка «Данные» Пульса владельца: деградацию отбора ловит график,
-- а не глаз. Базлайн аудита 05.08.2026: до переделки мусора была половина
-- публикации, после этапов 1-2 — одна позиция из одиннадцати.
-- NB: знак процента в комментариях миграций не ставить (exec_driver_sql).
CREATE TABLE IF NOT EXISTS digest_news_judge (
    digest_date  DATE PRIMARY KEY,
    n_items      INTEGER NOT NULL,
    junk         INTEGER NOT NULL,
    borderline   INTEGER NOT NULL,
    relevant     INTEGER NOT NULL,
    avg_score    NUMERIC(4, 2),
    detail       JSONB,
    llm_model    TEXT,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
