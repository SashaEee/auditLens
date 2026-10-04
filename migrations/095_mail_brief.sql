-- Утреннее письмо-выпуск (волна 5 аудита 03.10): не больше одного в день на
-- человека. Применять ДО выкладки кода (без индекса «слот дня» не займётся).
-- Откат — DROP INDEX.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_log_brief ON app_mail_log (username, day) WHERE kind = 'brief';
