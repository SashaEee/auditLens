-- «Пульс»: охват подразделений и траты на модели.
--
-- org_member — штат подразделения, который владелец загружает в «Пульсе» из
-- кадровой выгрузки (xlsx). По нему считается, какая доля сотрудников каждого
-- подразделения пользуется инструментом. ФИО не храним: для логинов без
-- табельного номера — только ключ сопоставления (sha256 нормализованных
-- «имя фамилия»).
--
-- cloud_spend_day / cloud_spend_meta — траты отдела на модели по дням, ключам и
-- моделям из сервиса учёта расходов облака. Храним у себя, чтобы история
-- не зависела от доступности сервиса и считалась без лишних запросов.

SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS org_member (
    tab_no     text PRIMARY KEY,
    status     text,                  -- код статуса из выгрузки (уволенные — ORG_FIRED_STATUSES)
    unit_code  text,                  -- номер подразделения
    unit_hint  text,                  -- вышестоящее подразделение, как в выгрузке
    position   text,
    grade      int,
    name_key   text,                  -- sha256 нормализованных «имя фамилия»
    loaded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS org_member_name ON org_member (name_key);

CREATE TABLE IF NOT EXISTS cloud_spend_day (
    day        date NOT NULL,
    api_key    text NOT NULL,
    model      text NOT NULL,
    cost       numeric(14, 2) NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, api_key, model)
);

CREATE TABLE IF NOT EXISTS cloud_spend_meta (
    day        date PRIMARY KEY,
    total      numeric(14, 2),        -- траты отдела за день
    threshold  numeric(14, 2),        -- дневная норма отдела
    as_of      timestamptz,           -- конец последнего собранного часа
    fetched_at timestamptz NOT NULL DEFAULT now()
);
