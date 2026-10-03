-- Уведомления в приложении: колокольчик рядом с карточкой пользователя внизу меню.
--
-- Раньше человек узнавал, что его добавили в дело, приобщили материалы или
-- поделились отчётом, только если сам открывал нужное место (последний шеринг
-- отчёта — 02.09, и никто его не увидел). Почты на проде нет — сообщаем внутри.
--
-- kind  — что случилось (case_added, case_items, report_shared, ticket, …);
-- link  — куда ведёт клик: «case:12», «report:45», «inbox:7» (NULL — некуда,
--         например «вас убрали из дела»);
-- ref   — данные для заголовка (название дела, роль, номер обращения);
-- count — склейка: пять материалов подряд от одного коллеги — одна строка
--         «5 новых материалов», а не пять.
-- Только CREATE ... IF NOT EXISTS: повтор безопасен.
CREATE TABLE IF NOT EXISTS app_notice (
    notice_id  BIGSERIAL PRIMARY KEY,
    username   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    actor      TEXT,
    link       TEXT,
    ref        JSONB NOT NULL DEFAULT '{}'::jsonb,
    count      INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_app_notice_user ON app_notice (username, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_notice_unread ON app_notice (username) WHERE read_at IS NULL;
