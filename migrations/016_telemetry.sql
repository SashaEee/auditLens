-- 016: телеметрия использования (дашборд «Пульс», доступ только владельцу)
--   page_view / page_leave(dur_ms)  — фронт (батч через /api/track + sendBeacon)
--   api_request / api_error         — middleware (латентность, статусы, исключения)
--   client_error                    — window.onerror / unhandledrejection
-- Идемпотентно: безопасно накатывать повторно.

CREATE TABLE IF NOT EXISTS usage_event (
    id         bigserial PRIMARY KEY,
    username   text,
    kind       text NOT NULL,
    page       text,                       -- hash-страница или нормализованный /api-путь
    dur_ms     integer,
    status     integer,
    payload    jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS usage_event_ts   ON usage_event (created_at DESC);
CREATE INDEX IF NOT EXISTS usage_event_kind ON usage_event (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_event_user ON usage_event (username, created_at DESC);
