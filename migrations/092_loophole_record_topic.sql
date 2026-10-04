-- «Аудит уязвимостей»: отметка «не о банках» (аудит 03.10, УЯЗ-01). Отдельной
-- таблицей, а не колонкой loophole_record: в ту таблицу пишет внешний
-- сборщик, а массовая отметка 85 тыс. строк переписала бы все её индексы
-- (GIN по полному тексту, триграммы). Применять ДО выкладки кода: каталог
-- читает её. Откат — сначала прежний образ приложения, затем DROP TABLE.
CREATE TABLE IF NOT EXISTS loophole_record_topic (
    record_id       BIGINT PRIMARY KEY,
    offtopic_reason TEXT,                   -- no_bank_terms | NULL (о банках)
    rule_version    SMALLINT NOT NULL DEFAULT 1,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
