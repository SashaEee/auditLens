-- app_setting — настройки приложения, которые владелец меняет в интерфейсе без перезапуска
-- контейнера (переменные окружения требуют пересоздания). Первая настройка — кому слать письма
-- об обращениях (inbox_mail) и курсор этой рассылки (inbox_mail_cursor), см. web/inbox_mail.py.
-- Только CREATE ... IF NOT EXISTS: повтор безопасен, существующие таблицы не блокируются.
CREATE TABLE IF NOT EXISTS app_setting (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);
