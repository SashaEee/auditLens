-- Почта пользователей для писем-уведомлений.
--
-- Адрес приходит из системы входа в заголовке X-Authentik-Email (когда nginx
-- его передаёт) и сохраняется при входе. emailed_at у уведомления — отметка, что
-- о нём уже написали письмом: одно событие не приходит на почту дважды, а
-- прочитанное в приложении не попадает ни в письмо, ни в сводку.
-- Только ADD ... IF NOT EXISTS: повтор безопасен.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS email_at TIMESTAMPTZ;
ALTER TABLE app_notice ADD COLUMN IF NOT EXISTS emailed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_app_notice_mail ON app_notice (username)
    WHERE read_at IS NULL AND emailed_at IS NULL;
