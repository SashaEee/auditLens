-- «Пульс» → «Итоги»: разметка запросов пользователей по направлениям аудита и
-- пригодности — даст ли запрос аудиторский результат (web/outcomes.py).
-- Источники — вопросы ИИ-помощнику, чат и поиск «Аудита уязвимостей», названия
-- аудит-дел. Размечает модель раз в сутки ночью; текст копируется сюда, чтобы
-- примеры «плохих» запросов не собирать из четырёх таблиц при каждом открытии.
SET lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS query_label (
    src        text        NOT NULL,          -- ai | vuln_chat | vuln_search | case_title
    ref_id     bigint      NOT NULL,          -- id строки в таблице-источнике
    username   text,
    asked_at   timestamptz NOT NULL,
    text_hash  text        NOT NULL,          -- md5 текста: переименовали дело → переразметка
    question   text        NOT NULL,
    dirs       text[]      NOT NULL DEFAULT '{}',
    quality    text        NOT NULL,          -- ok | followup | reference | template | vague | internal | personal | offtopic | test
    why        text,
    model      text,
    labeled_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (src, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_query_label_asked ON query_label (asked_at);
CREATE INDEX IF NOT EXISTS idx_query_label_hash ON query_label (text_hash);

-- Журнал прогонов разметки: когда была последняя, сколько разметила, сколько
-- ушло токенов — карточка «Итогов» показывает это внизу.
CREATE TABLE IF NOT EXISTS query_label_run (
    run_id      bigserial   PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    trigger     text,
    labeled     int,
    llm_calls   int,
    tokens_in   int,
    tokens_out  int,
    error       text
);
