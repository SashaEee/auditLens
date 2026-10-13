-- Плашка для всех пользователей: «В облаках турбулентность» — перебои у облачных
-- провайдеров (ИИ-помощник, утренний выпуск). Включает и выключает владелец в «Пульсе»
-- без деплоя: такие сбои не предупреждают заранее, и неизвестно, сколько они продлятся.
-- title/body пустые — фронт берёт текст по умолчанию (app.jsx, TURB_COPY).
CREATE TABLE IF NOT EXISTS app_banner (
    banner_id  TEXT PRIMARY KEY,
    active     BOOLEAN NOT NULL DEFAULT false,
    until      TIMESTAMPTZ,                    -- NULL — пока владелец не выключит
    title      TEXT,
    body       TEXT,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO app_banner (banner_id, active, updated_by)
VALUES ('turbulence', true, 'migration')
ON CONFLICT (banner_id) DO NOTHING;
