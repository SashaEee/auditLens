-- Почта, которую человек указал сам, и журнал писем.
--
-- Пока система входа не передаёт почту, адрес вводят в колокольчике
-- («Что присылать» → «На почту») и подтверждают кодом из письма. Без
-- подтверждения на адрес не уходит ничего, кроме самого кода.
--
-- app_user.email_source — откуда адрес: 'user' (указал и подтвердил сам),
-- 'sso' (из системы входа), 'off' (сам отключил — из системы входа не
-- возвращаем). Адрес, указанный вручную, система входа не перезаписывает.
--
-- app_email_verify — код подтверждения (хранится только хэш), срок, попытки.
-- app_mail_log — каждое письмо: лимиты на коды, «не чаще раза в 15 минут»,
-- утренняя сводка — раз в день (уникальный индекс по дню).
-- Только ... IF NOT EXISTS: повтор безопасен.
ALTER TABLE app_user ADD COLUMN IF NOT EXISTS email_source TEXT;

CREATE TABLE IF NOT EXISTS app_email_verify (
    username   TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    tries      INT NOT NULL DEFAULT 0,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_mail_log (
    id       BIGSERIAL PRIMARY KEY,
    username TEXT,
    kind     TEXT NOT NULL,                 -- verify | welcome | instant | digest | test
    to_addr  TEXT NOT NULL,
    n_items  INT NOT NULL DEFAULT 0,
    ok       BOOLEAN NOT NULL DEFAULT true,
    error    TEXT,
    day      DATE NOT NULL DEFAULT ((now() AT TIME ZONE 'Europe/Moscow')::date),
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mail_log_user ON app_mail_log (username, kind, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_mail_log_addr ON app_mail_log (to_addr, kind, sent_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_log_digest ON app_mail_log (username, day) WHERE kind = 'digest';
