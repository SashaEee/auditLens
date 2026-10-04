-- «Обратная связь» внизу меню: обращения пользователей к команде AuditLens.
--
-- user_ticket — обращение: тип (идея / ошибка / неверные цифры / не понимаю
-- как), раздел, текст и контекст, который фронт прикладывает сам (адрес с
-- фильтрами, версия, браузер, экран, последние ошибки страницы). Статус ведёт
-- команда во вкладке «Обращения» «Пульса».
-- user_ticket_msg — переписка по обращению: ответы команды, уточнения автора,
-- служебные строки о смене статуса. Непрочитанное считается по user_seen_at /
-- team_seen_at, поэтому отдельной таблицы уведомлений нет.
-- user_ticket_file — снимки экрана (до трёх на обращение). Лежат в базе, а не
-- в рабочей папке: на проде контейнер в неё не пишет.
-- Только CREATE ... IF NOT EXISTS: повтор безопасен.
CREATE TABLE IF NOT EXISTS user_ticket (
    ticket_id     BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL,
    kind          TEXT NOT NULL,                 -- idea | bug | numbers | howto | other
    section       TEXT,                          -- id раздела: reviews, loophole, overview…
    section_label TEXT,                          -- «Аудит отзывов › Жалобы»
    body          TEXT NOT NULL,
    context       JSONB NOT NULL DEFAULT '{}'::jsonb,
    status        TEXT NOT NULL DEFAULT 'new',   -- new | accepted | in_progress | done | wontfix | exists
    confirmed     BOOLEAN,                       -- автор после «Сделано»: работает / нет
    user_seen_at  TIMESTAMPTZ,
    team_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_ticket_user ON user_ticket (username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_ticket_status ON user_ticket (status, created_at DESC);

CREATE TABLE IF NOT EXISTS user_ticket_msg (
    msg_id     BIGSERIAL PRIMARY KEY,
    ticket_id  BIGINT NOT NULL REFERENCES user_ticket (ticket_id) ON DELETE CASCADE,
    author     TEXT NOT NULL,
    role       TEXT NOT NULL,                    -- team | user | system
    body       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_ticket_msg_ticket ON user_ticket_msg (ticket_id, created_at);

CREATE TABLE IF NOT EXISTS user_ticket_file (
    file_id    BIGSERIAL PRIMARY KEY,
    ticket_id  BIGINT NOT NULL REFERENCES user_ticket (ticket_id) ON DELETE CASCADE,
    mime       TEXT NOT NULL,
    data       BYTEA NOT NULL,
    width      INTEGER,
    height     INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_ticket_file_ticket ON user_ticket_file (ticket_id);
