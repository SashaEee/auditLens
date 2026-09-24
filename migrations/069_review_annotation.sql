-- LLM-разметка отзывов по кодификатору (см. rag/review_annotate.py).
-- Одна строка на отзыв и версию кодификатора: смена кодификатора не затирает
-- прежнюю разметку, её можно сравнить и откатить.
CREATE TABLE IF NOT EXISTS review_annotation (
    url            text        NOT NULL,
    schema_version text        NOT NULL,
    -- agree — две модели совпали; arbitrated — решил арбитр; junk — не отзыв;
    -- dup — копия другого отзыва (dup_of); failed — не разметилось, повторить
    status         text        NOT NULL,
    kind           text,
    segment        text,
    product        text,
    channels       text[],
    issue          text,
    issues2        text[],
    esc            text,
    esc_to         text[],
    no_consent     boolean,
    misled         boolean,
    vulnerable     text[],
    amount         numeric,
    event_date     text,
    city           text,
    code_fit       text,
    new_topic      text,
    summary        text,
    quote          text,
    -- true — цитата найдена в тексте дословно; false — модель её исказила и
    -- подобрать близкий фрагмент не удалось (цитата очищена)
    quote_ok       boolean,
    dup_of         text,
    a              jsonb,
    b              jsonb,
    c              jsonb,
    models         text,
    tokens_in      integer,
    tokens_out     integer,
    cost_rub       numeric,
    text_hash      text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (url, schema_version)
);

CREATE INDEX IF NOT EXISTS review_annotation_issue_idx
    ON review_annotation (schema_version, issue);
CREATE INDEX IF NOT EXISTS review_annotation_product_idx
    ON review_annotation (schema_version, product);
CREATE INDEX IF NOT EXISTS review_annotation_status_idx
    ON review_annotation (schema_version, status);
