-- Аудит-дело: участники и роли вместо «открыть всем».
--
-- Раньше дело либо было личным, либо «открытым команде» — на деле всем
-- пользователям AuditLens (запись шеринга без адресата): дело появлялось в
-- списке у каждого, а кто получил доступ, мог всё, кроме переименования и
-- удаления. Теперь владелец добавляет коллег поимённо с ролью:
--   editor — «может добавлять»: приобщает материалы, убирает своё, запускает разбор;
--   viewer — «только смотрит»: видит и выгружает.
-- Владелец по-прежнему audit_case.username. Доступа «всем» больше нет
-- (решение владельца инструмента 03.10.2026): оставшиеся такие записи отзываются.
--
-- deleted_at — мягкое удаление: дело ведут несколько человек, и удаление
-- одним кликом уносило материалы коллег навсегда. 30 дней его можно вернуть.
-- Только CREATE/ADD ... IF NOT EXISTS и идемпотентный перенос: повтор безопасен.
CREATE TABLE IF NOT EXISTS audit_case_member (
    case_id    BIGINT NOT NULL REFERENCES audit_case (case_id) ON DELETE CASCADE,
    username   TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('editor', 'viewer')),
    added_by   TEXT NOT NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, username)
);
CREATE INDEX IF NOT EXISTS idx_audit_case_member_user ON audit_case_member (username);

ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE audit_case ADD COLUMN IF NOT EXISTS deleted_by TEXT;

-- адресные доступы прежней схемы → участники «может добавлять» (так они и работали)
INSERT INTO audit_case_member (case_id, username, role, added_by, added_at)
SELECT s.case_id, s.shared_with, 'editor', s.owner, s.created_at
  FROM audit_case_share s
  JOIN audit_case c ON c.case_id = s.case_id
 WHERE s.shared_with IS NOT NULL AND s.revoked_at IS NULL AND s.shared_with <> c.username
ON CONFLICT (case_id, username) DO NOTHING;

-- доступа «всем пользователям» больше нет
UPDATE audit_case_share SET revoked_at = now()
 WHERE shared_with IS NULL AND revoked_at IS NULL;
