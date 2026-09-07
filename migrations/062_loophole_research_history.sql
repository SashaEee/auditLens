-- История исследований сохраняется при удалении; ссылка не даёт прав записи.
ALTER TABLE loophole_workspace ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE loophole_workspace ADD COLUMN IF NOT EXISTS share_token TEXT;
ALTER TABLE loophole_chat_message ADD COLUMN IF NOT EXISTS report_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_loophole_workspace_history
    ON loophole_workspace (user_id, last_active_at DESC, workspace_id DESC)
    WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_loophole_workspace_share
    ON loophole_workspace (share_token) WHERE share_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_loophole_chat_message_order
    ON loophole_chat_message (workspace_id, message_id);
