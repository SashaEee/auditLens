-- Аудит-дело: сохранённые команды (этап 5).
--
-- Команда — сохранённая группа коллег, её ведёт создатель. Подключение к делу
-- «живое»: добавили человека в команду — у него доступ ко всем делам, где она
-- подключена; убрали — доступ пропал (если его не добавили в дело отдельно).
-- Подключить можно только свою команду: иначе чужой создатель команды мог бы
-- провести в ваше дело кого угодно.
--
-- Доступ по-прежнему читается из audit_case_member: строки «через команду»
-- помечены team_id и пересчитываются при каждом изменении (userdata.
-- _sync_case_teams). Личная строка (team_id IS NULL) важнее командной.
-- Только CREATE/ADD ... IF NOT EXISTS: повтор безопасен.
CREATE TABLE IF NOT EXISTS audit_team (
    team_id    BIGSERIAL PRIMARY KEY,
    owner      TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_team_owner ON audit_team (owner);

CREATE TABLE IF NOT EXISTS audit_team_member (
    team_id  BIGINT NOT NULL REFERENCES audit_team (team_id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, username)
);
CREATE INDEX IF NOT EXISTS idx_audit_team_member_user ON audit_team_member (username);

CREATE TABLE IF NOT EXISTS audit_case_team (
    case_id  BIGINT NOT NULL REFERENCES audit_case (case_id) ON DELETE CASCADE,
    team_id  BIGINT NOT NULL REFERENCES audit_team (team_id) ON DELETE CASCADE,
    role     TEXT NOT NULL CHECK (role IN ('editor', 'viewer')),
    added_by TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_case_team_team ON audit_case_team (team_id);

ALTER TABLE audit_case_member ADD COLUMN IF NOT EXISTS team_id BIGINT
    REFERENCES audit_team (team_id) ON DELETE CASCADE;
