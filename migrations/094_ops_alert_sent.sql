-- Журнал отправленных эксплуатационных алертов (аудит 03.10, ПЛТ-01): одно
-- письмо в сутки на событие. Без таблицы notifier/alerts.py ничего не шлёт —
-- применять ДО выкладки кода. Откат — DROP TABLE.
CREATE TABLE IF NOT EXISTS ops_alert_sent (
    key  TEXT NOT NULL,
    day  DATE NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key, day)
);
