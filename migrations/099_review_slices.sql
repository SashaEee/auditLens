-- Срезы «Аудита отзывов» вне справочника продуктов — «Зарплатные проекты»,
-- «УС — банкоматы и терминалы», «Выездной сервис — курьеры и выезд» — по отдельной
-- LLM-разметке флагов (rag/review_slices.py), а не по каналам основной разметки:
-- курьер маркетплейса — не выездной сервис банка, POS-терминал магазина — не УС.

-- review_index горячая: ждущий блокировку ALTER ставит в очередь за собой все запросы
-- вкладки (14.10 так она висела 3,5 мин). Не дождались за 3 с — ошибка, повторить позже.
SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS review_slice (
    url        text PRIMARY KEY,
    schema     text NOT NULL,                       -- версия определений срезов
    tags       text[] NOT NULL DEFAULT '{}',        -- payroll | us | field
    status     text NOT NULL,                       -- agree | arbitrated | arb_single | failed
    a          jsonb,                               -- флаги разметчика A
    b          jsonb,                               -- флаги разметчика B
    arb        jsonb,                               -- решение арбитра по спорным
    quote      text,                                -- цитата, подтверждающая срез
    cost_rub   numeric(10, 4) NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_slice_created ON review_slice (created_at);

-- итог в индексе: все запросы вкладки считаются по review_index без join
ALTER TABLE review_index ADD COLUMN IF NOT EXISTS slices text[];

-- Фильтр продукта: настоящий продукт или срез. Названия — review_slices.SLICES.
-- Прежняя 4-аргументная версия (каналы) остаётся, пока код на неё ссылается.
CREATE OR REPLACE FUNCTION al_product_is(product text, slices text[], want text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT product = want
        OR (want = 'Зарплатные проекты' AND 'payroll' = ANY(coalesce(slices, '{}')))
        OR (want = 'УС — банкоматы и терминалы' AND 'us' = ANY(coalesce(slices, '{}')))
        OR (want = 'Выездной сервис — курьеры и выезд' AND 'field' = ANY(coalesce(slices, '{}')))
$$;
