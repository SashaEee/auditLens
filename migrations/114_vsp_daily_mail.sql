-- Ежедневное письмо раздела «Аудит сети ВСП» (web/vsp_mail.py).
--
-- vsp_mail_item — что и в каком выпуске письма появилось впервые: тяжёлая история, жалоба на долгое
-- ожидание, способ действия. Выпуск собирается раз в рабочий день; каждый подписчик получает записи
-- выпусков после своего прошлого письма, поэтому одна история не приходит дважды и не теряется, если
-- отзыв прочитан моделью позже. day = 2000-01-01 — запись уже была до первого выпуска (в письма не идёт).
--
-- uq_mail_log_vsp_daily — не больше одного такого письма в день на человека («слот дня», как у
-- утренней сводки и письма-выпуска). Применять ДО выкладки кода. Журнал писем небольшой, индекс
-- строится мгновенно; lock_timeout — чтобы ожидание блокировки не задержало запросы пользователей.
-- Только ... IF NOT EXISTS: повтор безопасен. Откат — DROP INDEX / DROP TABLE.
SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS vsp_mail_item (
    kind       TEXT NOT NULL,                 -- story | wait | wave
    rid        TEXT NOT NULL,                 -- id отзыва или ключ способа действия
    day        DATE NOT NULL,                 -- выпуск, в котором запись появилась
    subject    TEXT,                          -- регион — для подписки по регионам
    gosb       TEXT,
    point      TEXT,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, rid)
);
CREATE INDEX IF NOT EXISTS idx_vsp_mail_item_day ON vsp_mail_item (day);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_log_vsp_daily ON app_mail_log (username, day) WHERE kind = 'vsp_daily';
